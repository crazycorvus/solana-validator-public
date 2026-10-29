"""Sender (T10 / FR-05, FR-06, FR-07): fire pre-signed probe batches at a leader window.

Dry-run is the default and is structural, not a flag check on each call: it uses `DryTransport`,
which has no network code at all, and `Sender` refuses to pair a live transport with live=False.
Live sends reserve the batch's worst-case fee in the spend ledger BEFORE the window opens.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import Transaction

from .ledger import CapExceeded, SpendLedger
from .matrix import Batch
from .probes import ProbeSpec, build_tx, fee_lamports, signature_of, spec_row


def now_us() -> int:
    return time.time_ns() // 1000


def payer_pool(master: Keypair | None, n: int, run_seed: str = "dry") -> list[Keypair]:
    """One fee payer per probe (a shared payer would write-lock-serialise the batch, see probes.py).
    Derived deterministically from the master key so no extra secret files exist and the pool can
    be re-derived to sweep leftover lamports. Dry-run uses throwaway seeds with no real funds."""
    base = bytes(master)[:32] if master else run_seed.encode()
    return [Keypair.from_seed(hashlib.sha256(base + i.to_bytes(4, "big")).digest()) for i in range(n)]


@dataclass
class Prepared:
    spec: ProbeSpec
    tx: Transaction
    signature: str
    raw: bytes


def prepare(batch: Batch, payers: list[Keypair], blockhash: Hash) -> list[Prepared]:
    out = []
    for spec in batch.probes:
        tx = build_tx(spec, payers[spec.payer_index], blockhash)
        out.append(Prepared(spec, tx, signature_of(tx), bytes(tx)))
    return out


class Transport(Protocol):
    path: str
    live: bool

    async def send(self, raw: bytes) -> dict: ...
    async def close(self) -> None: ...


class DryTransport:
    """Records what WOULD be sent. Contains no network code."""
    path = "dry"
    live = False

    async def send(self, raw: bytes) -> dict:
        return {"dry": True, "bytes": len(raw)}

    async def close(self) -> None:
        pass


class RpcTransport:
    """sendTransaction through an RPC provider (no preflight, no provider-side retry)."""
    path = "rpc"
    live = True

    def __init__(self, url: str, pool: int = 64):
        self.url = url
        self.client = httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=pool,
                                                                         max_keepalive_connections=pool))

    async def warm(self, n: int) -> None:
        """Open connections ahead of the window so TLS handshakes are not inside it."""
        body = {"jsonrpc": "2.0", "id": 0, "method": "getHealth"}
        await asyncio.gather(*[self.client.post(self.url, json=body) for _ in range(n)])

    async def send(self, raw: bytes) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(raw).decode(),
                           {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]}
        r = await self.client.post(self.url, json=body)
        return r.json()

    async def close(self) -> None:
        await self.client.aclose()


class Sender:
    def __init__(self, transport: Transport, run_id: str, out_dir: Path, *, live: bool,
                 ledger: SpendLedger | None = None):
        if transport.live and not live:
            raise RuntimeError("live transport used without live=True")
        if live and ledger is None:
            raise RuntimeError("live mode requires a spend ledger")
        self.t, self.run_id, self.live, self.ledger = transport, run_id, live, ledger
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self._log = open(self.out / "send_log.jsonl", "a")

    def log(self, **kw) -> None:
        self._log.write(json.dumps({"ts_us": now_us(), "run_id": self.run_id, **kw}) + "\n")
        self._log.flush()

    async def _one(self, p: Prepared, t0: float) -> dict:
        off = p.spec.send_offset_ms / 1000
        delay = t0 + off - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        ts = now_us()
        try:
            resp = await self.t.send(p.raw)
            err = resp.get("error") if isinstance(resp, dict) else None
        except Exception as e:  # network failure is data, not a crash
            resp, err = None, f"{type(e).__name__}: {e}"
        self.log(event="send", probe_id=p.spec.probe_id, signature=p.signature, path=self.t.path,
                 send_ts_us=ts, ack_us=now_us(), live=self.live, error=err,
                 lamports=fee_lamports(p.spec.cu_price, p.spec.cu_limit))
        return {**spec_row(p.spec, p.signature, ts, self.t.path), "error": err}

    async def send_batch(self, prepared: list[Prepared], t0: float, *, leader: str | None = None,
                         window: list[int] | None = None) -> list[dict]:
        """Fire `prepared` in send_order starting at wall-clock `t0`. Returns probes.csv rows."""
        order = sorted(prepared, key=lambda p: p.spec.send_order)
        total = sum(fee_lamports(p.spec.cu_price, p.spec.cu_limit) for p in order)
        if self.live:
            try:
                self.ledger.reserve(total, self.run_id)  # before the window, not per tx
            except CapExceeded as e:
                self.log(event="refused", reason=str(e), batch=order[0].spec.batch_id)
                raise
        self.log(event="batch_start", batch=order[0].spec.batch_id, n=len(order), t0=t0,
                 leader=leader, window=window, reserved_lamports=total if self.live else 0)
        # tasks are created in send order; each runs up to its first await in that order
        rows = await asyncio.gather(*[asyncio.ensure_future(self._one(p, t0)) for p in order])
        sts = [r["send_ts_us"] for r in rows]
        self.log(event="batch_done", batch=order[0].spec.batch_id,
                 dispatch_spread_us=max(sts) - min(sts) if sts else 0)
        self.write_probes(rows, leader, window)
        return rows

    def write_probes(self, rows, leader, window) -> None:
        path = self.out / "probes.csv"
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["leader", "window_start"])
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({**r, "leader": leader, "window_start": window[0] if window else None})

    async def close(self) -> None:
        await self.t.close()
        self._log.close()
