"""Mainnet dry-run (FR-07): real leader schedule and real windows, nothing sent.

Answers the question the dry-run exists for: "would we have made it into the window?" It replays the
exact procedure the live run uses (freeze a window-start prediction ~25 s ahead, fire at prediction minus
`lead`), then compares with when the window really started on the slot feed, for a grid of leads.
`PATH_LATENCY_S` is the provider round trip to the leader, calibrated from the live tests of 2026-10-28/29:
a send 284 ms before the observed window start landed in slot +0, sends 641 ms and 827 ms after landed in
slots +4/+5 (about one slot of extra delay on top of the elapsed time).
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from .schedule import SLOTS_PER_WINDOW, SlotWatcher

PATH_LATENCY_S = 0.28
LEAD_GRID_MS = list(range(-200, 701, 100))


@dataclass
class Sample:
    slot: int
    predicted: float
    observed: float
    slot_s: float
    target: bool


class WindowDryRun:
    def __init__(self, watcher: SlotWatcher, prep_s: float = 25.0, targets: set[int] | None = None):
        self.w, self.prep_s, self.targets = watcher, prep_s, targets or set()
        self.frozen: dict[int, float] = {}
        self.samples: list[Sample] = []

    def on_slot(self, slot: int, now: float) -> None:
        if not self.w.ready():
            return
        ss = self.w.slot_seconds()
        base = slot + round(self.prep_s / ss)
        base -= base % SLOTS_PER_WINDOW
        if base not in self.frozen:
            p = self.w.predict(base)
            if p is not None:
                self.frozen[base] = p
        if slot % SLOTS_PER_WINDOW == 0 and slot in self.frozen:
            self.samples.append(Sample(slot, self.frozen.pop(slot), now, ss, slot in self.targets))
        for b in [b for b in self.frozen if b < slot]:
            del self.frozen[b]

    def report(self, lead_ms_now: int, latency_s: float = PATH_LATENCY_S) -> dict:
        S = self.samples
        if not S:
            return {"windows": 0}
        err = [x.observed - x.predicted for x in S]

        def hit_rate(lead_ms: int, sub: list[Sample]) -> float | None:
            if not sub:
                return None
            ok = 0
            for x in sub:
                arrival = x.predicted - lead_ms / 1000 + latency_s
                ok += x.observed <= arrival < x.observed + SLOTS_PER_WINDOW * x.slot_s
            return ok / len(sub)
        grid = {str(l): hit_rate(l, S) for l in LEAD_GRID_MS}
        best = max(LEAD_GRID_MS, key=lambda l: (grid[str(l)], -abs(l)))
        a = sorted(abs(e) * 1000 for e in err)
        return {"windows": len(S), "slot_ms": round(statistics.median(x.slot_s for x in S) * 1000, 1),
                "window_ms": round(SLOTS_PER_WINDOW * statistics.median(x.slot_s for x in S) * 1000),
                "median_abs_err_ms": round(statistics.median(a)), "p90_abs_err_ms": round(a[int(0.9 * len(a))]),
                "mean_signed_err_ms": round(1000 * statistics.mean(err)),
                "assumed_path_latency_ms": round(latency_s * 1000),
                "hit_rate_by_lead_ms": grid, "configured_lead_ms": lead_ms_now,
                "hit_rate_at_configured_lead": hit_rate(lead_ms_now, S), "best_lead_ms": best,
                "hit_rate_at_best_lead": grid[str(best)],
                "target_windows": sum(x.target for x in S),
                "caveat": "ground truth is the same slot feed (noisy by design); the real check is where probes land"}


async def run_window_dryrun(url: str, seconds: float, lead_ms: int, targets: set[int] | None = None,
                            warm_timeout: float = 180) -> tuple[dict, SlotWatcher]:
    w = SlotWatcher(url)
    d = WindowDryRun(w, targets=targets)
    w.listeners.append(d.on_slot)
    task = asyncio.create_task(w.run())
    try:
        t0 = time.time()
        while not w.ready():
            if time.time() - t0 > warm_timeout:
                raise RuntimeError("slot clock never became ready")
            await asyncio.sleep(0.5)
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
    return d.report(lead_ms), w


def format_report(r: dict) -> str:
    if not r.get("windows"):
        return "window dry-run: no windows observed"
    g = r["hit_rate_by_lead_ms"]
    rows = "  ".join(f"{l:+d}:{100 * g[str(l)]:.0f}%" for l in LEAD_GRID_MS)
    return (f"window dry-run over {r['windows']} real windows ({r['window_ms']} ms each, slot {r['slot_ms']} ms)\n"
            f"  clock error: median {r['median_abs_err_ms']} ms, p90 {r['p90_abs_err_ms']} ms, "
            f"mean signed {r['mean_signed_err_ms']} ms\n"
            f"  would-hit rate by lead (send = predicted - lead; path {r['assumed_path_latency_ms']} ms): {rows}\n"
            f"  configured lead {r['configured_lead_ms']:+d} ms -> {100 * r['hit_rate_at_configured_lead']:.0f}% ; "
            f"best {r['best_lead_ms']:+d} ms -> {100 * r['hit_rate_at_best_lead']:.0f}%")
