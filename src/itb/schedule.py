"""Leader schedule, slot clock and window predictor (T06 / FR-03)."""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field

import websockets

from .rpc import Rpc

SLOTS_PER_WINDOW = 4
SLOT_MS_NOMINAL = 400.0  # nominal; the clock fits the real rate from observations


def ws_url(rpc_url: str) -> str:
    return rpc_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)


@dataclass
class LeaderSchedule:
    epoch: int
    first_slot: int
    slot_leader: dict[int, str]  # absolute slot -> identity

    @classmethod
    def fetch(cls, rpc: Rpc) -> "LeaderSchedule":
        info = rpc.call("getEpochInfo")
        first = info["absoluteSlot"] - info["slotIndex"]
        raw = rpc.call("getLeaderSchedule")  # identity -> [slot offsets in epoch]
        m = {first + off: ident for ident, offs in raw.items() for off in offs}
        return cls(info["epoch"], first, m)

    def windows(self, identity: str, after_slot: int, limit: int | None = None) -> list[list[int]]:
        """Complete 4-slot windows (aligned, same leader) for `identity` starting after `after_slot`."""
        out, seen = [], set()
        for slot in sorted(s for s, i in self.slot_leader.items() if i == identity and s > after_slot):
            base = slot - slot % SLOTS_PER_WINDOW
            if base in seen or base <= after_slot:
                continue
            seen.add(base)
            w = list(range(base, base + SLOTS_PER_WINDOW))
            if all(self.slot_leader.get(x) == identity for x in w):
                out.append(w)
                if limit and len(out) >= limit:
                    break
        return out


@dataclass
class SlotClock:
    """Maps slot -> wall time with a robust linear fit over recent slot notifications."""
    obs: list[tuple[int, float]] = field(default_factory=list)
    keep: int = 200

    def add(self, slot: int, ts: float) -> None:
        if not self.obs or slot > self.obs[-1][0]:
            self.obs.append((slot, ts))
            del self.obs[:-self.keep]

    def rate(self) -> float | None:
        """Seconds per slot, least-squares over all observations (endpoints alone are too noisy)."""
        if len(self.obs) < 10:
            return None
        xs = [s for s, _ in self.obs]
        ys = [t for _, t in self.obs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else None

    def predict(self, slot: int) -> float | None:
        """Endpoint-span rate + median intercept. Measured on mainnet 2026-10-28 (330 s, 323 windows) this was
        slightly better than a least-squares slope (median |err| 400 vs 467 ms) and on localnet far better
        (+-12 ms vs +100 ms), so `rate()` (OLS) is only used to report the slot time."""
        if len(self.obs) < 10:
            return None
        xs = [s for s, _ in self.obs]
        span = self.obs[-1][1] - self.obs[0][1]
        rate = span / (xs[-1] - xs[0]) if xs[-1] > xs[0] else SLOT_MS_NOMINAL / 1000
        s0 = xs[-1]
        icpt = statistics.median(ts - (s - s0) * rate for s, ts in self.obs)
        return icpt + (slot - s0) * rate


async def track(url: str, seconds: float, lead_s: float = 10.0, tol_s: float = 0.4,
                windows: dict[int, str] | None = None) -> dict:
    """Subscribe to slotSubscribe for `seconds`; ~`lead_s` before each upcoming window start,
    freeze a prediction, then compare with the observed arrival of the window's first slot.
    A hit = |predicted - observed| <= tol_s (one nominal slot)."""
    clock = SlotClock()
    pending: dict[int, float] = {}  # window base slot -> frozen predicted ts
    bias: list[float] = []  # past signed errors of window-start slots: first slot of a window
    #                         arrives later than the fitted line (leader hand-off), so we learn it
    results = []
    t_end = time.time() + seconds
    async with websockets.connect(ws_url(url), ping_interval=20) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}))
        while time.time() < t_end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if msg.get("method") != "slotNotification":
                continue
            now = time.time()
            slot = msg["params"]["result"]["slot"]
            clock.add(slot, now)
            # freeze a prediction for the next window start that is ~lead_s ahead
            target = slot + round(lead_s / (SLOT_MS_NOMINAL / 1000))
            base = target - target % SLOTS_PER_WINDOW
            if base not in pending:
                p = clock.predict(base)
                if p is not None:
                    pending[base] = p + (statistics.median(bias[-30:]) if len(bias) >= 5 else 0.0)
            if slot in pending and slot % SLOTS_PER_WINDOW == 0:
                pred = pending.pop(slot)
                raw = now - pred
                err = raw
                bias.append(raw + (statistics.median(bias[-30:]) if len(bias) >= 5 else 0.0))
                results.append({"window_start": slot, "err_ms": round(err * 1000, 1),
                                "hit": abs(err) <= tol_s})
            # drop stale predictions whose slot was skipped
            for b in [b for b in pending if b < slot]:
                del pending[b]
    hits = sum(r["hit"] for r in results)
    return {"windows": len(results), "hits": hits,
            "hit_rate": hits / len(results) if results else None,
            "median_abs_err_ms": statistics.median(abs(r["err_ms"]) for r in results) if results else None,
            "samples": results}


class SlotWatcher:
    """Long-running slot clock for the orchestrator: keeps a SlotClock fresh from slotSubscribe,
    reconnects on drop, remembers when each slot was first seen, and learns the first-slot lag
    of window starts (leader hand-off) from its own past predictions."""

    def __init__(self, url: str, ws: str | None = None):
        self.url, self.clock, self.seen, self.lags = url, SlotClock(), {}, []
        self.ws = ws or ws_url(url)
        self.listeners: list = []  # callables (slot, now) run after the clock has absorbed each notification

    def ready(self) -> bool:
        """Enough history for a stable fit: >= 100 slots spanning >= 30 s (mainnet slots are ~0.2 s now,
        so the old 25-observation rule fitted on ~5 s and was 777 ms off on the first live window)."""
        o = self.clock.obs
        return len(o) >= 100 and o[-1][1] - o[0][1] >= 30

    def slot_seconds(self) -> float:
        return self.clock.rate() or SLOT_MS_NOMINAL / 1000

    def predict(self, slot: int) -> float | None:
        p = self.clock.predict(slot)
        if p is None:
            return None
        return p + (statistics.median(self.lags[-30:]) if len(self.lags) >= 5 else 0.0)

    def observed(self, slot: int) -> float | None:
        return self.seen.get(slot)

    def ingest(self, slot: int, now: float) -> None:
        """Absorb one slot notification (split out of `run` so it can be driven by tests and replays)."""
        if slot % SLOTS_PER_WINDOW == 0 and self.ready():
            pred = self.clock.predict(slot)
            if pred is not None:
                self.lags.append(now - pred)
        self.clock.add(slot, now)
        self.seen[slot] = now
        for fn in self.listeners:
            fn(slot, now)
        if len(self.seen) > 4000:
            for k in sorted(self.seen)[:1000]:
                del self.seen[k]

    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(self.ws, ping_interval=20) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}))
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("method") != "slotNotification":
                            continue
                        self.ingest(m["params"]["result"]["slot"], time.time())
            except (OSError, websockets.ConnectionClosed):
                await asyncio.sleep(1)
