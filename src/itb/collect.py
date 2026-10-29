"""Block collector (T12 / FR-08) and landing mapper (T13 / FR-09)."""
from __future__ import annotations

import csv
import gzip
import json
import struct
import time
from pathlib import Path

from .rpc import Rpc, RpcError

GETBLOCK_OPTS = {"encoding": "json", "transactionDetails": "full", "rewards": True,
                 "maxSupportedTransactionVersion": 1, "commitment": "finalized"}
SKIPPED_CODES = (-32007, -32009)  # slot skipped / missing in long-term storage
NOT_YET_CODE = -32004             # block not available yet (not finalized)


def fetch_block(rpc: Rpc, slot: int, wait_s: float = 90, poll_s: float = 2.0) -> dict:
    """finalized getBlock; polls until the block exists. Skipped slots are stored as such."""
    t_end = time.time() + wait_s
    while True:
        try:
            b = rpc.call("getBlock", [slot, GETBLOCK_OPTS])
            return b if b is not None else {"skipped": True, "slot": slot}
        except RpcError as e:
            if e.code in SKIPPED_CODES:
                return {"skipped": True, "slot": slot}
            if e.code == NOT_YET_CODE and time.time() < t_end:
                time.sleep(poll_s)
                continue
            raise


def collect_slots(rpc: Rpc, slots: list[int], run_dir: Path, cache_dir: Path | None = None,
                  wait_s: float = 90) -> dict[int, str]:
    """Fetch full finalized blocks into `cache_dir/blocks` (git-ignored working copy) and write
    `run_dir/leaders.json` (slot -> leader identity). `compact_blocks` then builds the committed copy."""
    cache = Path(cache_dir or run_dir) / "blocks"
    cache.mkdir(parents=True, exist_ok=True)
    run_dir = Path(run_dir)
    slots = sorted(set(slots))
    if not slots:
        return {}
    for s in slots:
        p = cache / f"{s}.json"
        if p.exists():  # finalized blocks are immutable
            continue
        p.write_text(json.dumps({"slot": s, **fetch_block(rpc, s, wait_s)}))
    lmap: dict[str, str] = {}
    start = prev = slots[0]
    for s in slots[1:] + [None]:  # one call per contiguous cluster: windows can be hours apart
        if s is not None and s - prev <= 1 and s - start < 4000:
            prev = s
            continue
        for i, who in enumerate(rpc.call("getSlotLeaders", [start, prev - start + 1])):
            lmap[str(start + i)] = who
        start = prev = s if s is not None else start
    lp = run_dir / "leaders.json"
    if lp.exists():
        lmap = {**json.loads(lp.read_text()), **lmap}
    lp.write_text(json.dumps(lmap))
    return {int(k): v for k, v in lmap.items()}


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def compute_budget(tx: dict) -> tuple[int | None, int | None]:
    """(cu_price micro-lamports, cu_limit) requested by a tx, from its ComputeBudget instructions."""
    msg = tx["transaction"]["message"]
    keys = msg["accountKeys"]
    price = limit = None
    for ix in msg["instructions"]:
        if keys[ix["programIdIndex"]] != COMPUTE_BUDGET:
            continue
        d = _b58decode(ix["data"])
        if d[:1] == b"\x03" and len(d) >= 9:
            price = struct.unpack("<Q", d[1:9])[0]
        elif d[:1] == b"\x02" and len(d) >= 5:
            limit = struct.unpack("<I", d[1:5])[0]
    return price, limit


def compact_block(b: dict) -> dict:
    """Judge-useful subset: block position + per-tx signature, error, fee, CU used, requested price/limit."""
    if b.get("skipped"):
        return b
    txs = []
    for t in b.get("transactions", []):
        meta = t.get("meta") or {}
        price, limit = compute_budget(t)
        keys = t["transaction"]["message"]["accountKeys"]
        vote = any(keys[ix["programIdIndex"]] == VOTE_PROGRAM
                   for ix in t["transaction"]["message"]["instructions"])
        txs.append({"sig": t["transaction"]["signatures"][0], "vote": vote, "err": meta.get("err"),
                    "fee": meta.get("fee"), "cu_used": meta.get("computeUnitsConsumed"),
                    "cu_price": price, "cu_limit": limit})
    return {"slot": b["slot"], "compact": True, "blockhash": b.get("blockhash"),
            "parentSlot": b.get("parentSlot"), "blockTime": b.get("blockTime"),
            "blockHeight": b.get("blockHeight"), "transactions": txs}


def compact_blocks(cache_dir: Path, run_dir: Path, probe_sigs: set[str], keep_full: bool = False) -> dict:
    """Committed copy of every collected slot, gzipped. By default ALL slots are COMPACT (signature, error,
    fee, CU used, requested price/limit): that is every transaction of the block in order, enough to verify
    positions, and a judge can re-fetch the full block with getBlock. `keep_full` additionally keeps the full
    getBlock response for slots that contain one of our probes (about 0.7 MB each, hundreds of MB for a study)."""
    out = Path(run_dir) / "blocks"
    out.mkdir(parents=True, exist_ok=True)
    n_full = n_compact = 0
    for p in sorted((Path(cache_dir) / "blocks").glob("*.json")):
        b = json.loads(p.read_text())
        has = keep_full and (not b.get("skipped")) and any(
            t["transaction"]["signatures"][0] in probe_sigs for t in b.get("transactions", []))
        body = p.read_bytes() if has else json.dumps(compact_block(b), separators=(",", ":")).encode()
        (out / f"{p.stem}.json.gz").write_bytes(gzip.compress(body, 9, mtime=0))  # mtime=0: reproducible bytes
        (out / f"{p.stem}.json").unlink(missing_ok=True)
        n_full += has
        n_compact += not has
    return {"full": n_full, "compact": n_compact}


def _load(p: Path) -> dict:
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_text())


def slots_for_run(probes_csv: Path, neighbours: int = 1) -> list[int]:
    out = set()
    with open(probes_csv) as f:
        for r in csv.DictReader(f):
            if r.get("window_start") not in (None, "", "None"):
                w = int(r["window_start"])
                out.update(range(w - neighbours, w + 4 + neighbours))
    return sorted(out)


def block_index(blocks_dir: Path) -> dict[str, tuple[int, int, dict]]:
    """signature -> (slot, tx_index_in_block, {"err": ...}); reads full (.json.gz/.json) or compact blocks."""
    idx: dict[str, tuple[int, int, dict]] = {}
    for p in sorted(Path(blocks_dir).glob("*.json*")):
        b = _load(p)
        if b.get("skipped"):
            continue
        for i, t in enumerate(b.get("transactions", [])):
            if b.get("compact"):
                idx[t["sig"]] = (b["slot"], i, {"err": t["err"]})
            else:
                idx[t["transaction"]["signatures"][0]] = (b["slot"], i, t.get("meta") or {})
    return idx


FIELDS = ["signature", "probe_id", "batch_id", "slot", "leader", "expected_leader", "tx_index_in_block",
          "entry_index", "index_in_entry", "status", "err", "outcome", "rank_in_batch"]


def map_landings(probes_csv: Path, run_dir: Path) -> list[dict]:
    """One row per probe. outcome: landed | landed_other_leader | landed_outside_window | dropped.
    entry_index/index_in_entry stay empty: getBlock returns no entry boundaries (see DESIGN.md)."""
    leaders = {int(k): v for k, v in json.loads((run_dir / "leaders.json").read_text()).items()}
    idx = block_index(run_dir / "blocks")
    rows = []
    with open(probes_csv) as f:
        probes = list(csv.DictReader(f))
    for pr in probes:
        sig = pr["signature"]
        win = int(pr["window_start"]) if pr["window_start"] not in ("", "None") else None
        row = {k: "" for k in FIELDS}
        row.update(signature=sig, probe_id=pr["probe_id"], batch_id=pr["batch_id"],
                   expected_leader=pr["leader"])
        if sig not in idx:
            row["outcome"] = "dropped"
        else:
            slot, ti, meta = idx[sig]
            row.update(slot=slot, leader=leaders.get(slot, ""), tx_index_in_block=ti,
                       status="ok" if meta.get("err") is None else "failed",
                       err=json.dumps(meta["err"]) if meta.get("err") is not None else "")
            if row["leader"] != pr["leader"]:
                row["outcome"] = "landed_other_leader"
            elif win is not None and not (win <= slot < win + 4):
                row["outcome"] = "landed_outside_window"
            else:
                row["outcome"] = "landed"
        rows.append(row)
    # rank among OUR probes of the same batch that landed in the same slot-ordered block stream
    # one ranking per (run, batch): a run folder holds many validators and runs with the same batch ids
    by_batch: dict[tuple[str, str], list[dict]] = {}
    for r, pr in zip(rows, probes):
        if r["outcome"] == "landed":
            by_batch.setdefault((pr.get("run_id") or pr["probe_id"].rsplit(":", 2)[0], r["batch_id"]), []).append(r)
    for rs in by_batch.values():
        for rank, r in enumerate(sorted(rs, key=lambda r: (r["slot"], r["tx_index_in_block"]))):
            r["rank_in_batch"] = rank
    with open(run_dir / "landings.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def collect_run(rpc: Rpc, run_dir: Path, cache_dir: Path, wait_s: float = 90) -> dict:
    """Collect the target windows (+/-1 slot), then find where every probe NOT seen there actually landed
    (getSignatureStatuses) and collect those slots too. Without this a probe that landed late or early
    is mislabelled `dropped`; with it, only probes the chain has never seen are dropped."""
    from collections import Counter
    run_dir = Path(run_dir)
    probes = run_dir / "probes.csv"
    slots = slots_for_run(probes)
    if not slots:
        return {}
    collect_slots(rpc, slots, run_dir, cache_dir, wait_s)
    with open(probes) as f:
        sigs = [r["signature"] for r in csv.DictReader(f)]
    for _ in range(2):
        compact_blocks(cache_dir, run_dir, set(sigs))
        rows = map_landings(probes, run_dir)
        missing = [r["signature"] for r in rows if r["outcome"] == "dropped"]
        extra: set[int] = set()
        for i in range(0, len(missing), 256):
            st = rpc.call("getSignatureStatuses", [missing[i:i + 256], {"searchTransactionHistory": True}])["value"]
            extra |= {s["slot"] for s in st if s}
        extra -= set(slots)
        if not extra:
            break
        slots = sorted(set(slots) | extra)
        collect_slots(rpc, sorted(extra), run_dir, cache_dir, wait_s)
    return dict(Counter(r["outcome"] for r in rows))


def block_fees(blocks_dir: Path) -> dict[str, int]:
    """signature -> fee actually charged, from full (meta.fee) or compact (fee) committed blocks."""
    out: dict[str, int] = {}
    for p in sorted(Path(blocks_dir).glob("*.json*")):
        b = _load(p)
        if b.get("skipped"):
            continue
        for t in b.get("transactions", []):
            if b.get("compact"):
                out[t["sig"]] = t.get("fee") or 0
            else:
                out[t["transaction"]["signatures"][0]] = (t.get("meta") or {}).get("fee") or 0
    return out


def excluded_signatures(run_dir: Path) -> tuple[set[str], str]:
    """Probes excluded from delivery statistics because OUR infrastructure (not a validator) failed them,
    with the reason. Read from `exclusions.json` (written by `itb send --redo`)."""
    import csv
    p = Path(run_dir) / "exclusions.json"
    if not p.exists():
        return set(), ""
    doc = json.loads(p.read_text())
    keys = set(doc.get("batches", []))
    with open(Path(run_dir) / "probes.csv") as f:
        sigs = {r["signature"] for r in csv.DictReader(f) if f"{r['run_id']}:{r['batch_id']}" in keys}
    return sigs, doc.get("reason", "")
