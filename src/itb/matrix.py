"""Experiment matrix generator (T09): one batch type per leader window, seeded and budget-checked."""
from __future__ import annotations

import random
from dataclasses import dataclass

from .probes import ProbeSpec, fee_lamports

# Measured on Agave 4.3.2 (simulateTransaction): the memo ix alone consumes ~19.4k CU and the whole probe
# ~19.8k; a 10k limit made every probe fail with ProgramFailedToComplete. 30k leaves headroom for the
# longer live memo ids.
MIN_CU_LIMIT = 30_000
DEFAULT_CU_LIMIT = 30_000


@dataclass
class Batch:
    batch_id: str
    batch_type: str
    probes: list[ProbeSpec]
    tests: list[str]

    @property
    def worst_case_lamports(self) -> int:
        return sum(fee_lamports(p.cu_price, p.cu_limit) for p in self.probes)


def _mk(run, bid, btype, idx, price, limit, key, order, payer, offset=0):
    return ProbeSpec(f"probe:{run}:{bid}:{idx}", run, bid, btype, idx, price, limit, key, order,
                     payer, offset)


def fee_ladder(run: str, bid: str, rng: random.Random, levels=(0, 100, 500, 1_000, 2_000, 5_000, 10_000, 20_000),
               reps=1, limit=DEFAULT_CU_LIMIT) -> Batch:
    prices = [p for p in levels for _ in range(reps)]
    order = list(range(len(prices)))
    rng.shuffle(order)  # send order independent of fee
    ps = [_mk(run, bid, "fee_ladder", i, prices[i], limit, f"u{i}", order[i], i) for i in range(len(prices))]
    return Batch(bid, "fee_ladder", ps, ["H1", "H3"])


def reward_per_cu(run: str, bid: str, rng: random.Random,
                  limits=(30_000, 45_000, 65_000, 95_000, 135_000, 200_000),
                  prices=(1_000, 2_000, 3_000, 4_000, 5_000, 6_000)) -> Batch:
    """H1 vs H2 discriminator. Price rises with the CU limit, but (priority fee + base_fee/2) / limit
    FALLS with it (the base-fee term dominates at these sizes). A price-sorter and a reward-per-CU
    sorter therefore produce opposite orders; see infer.reward_per_cu."""
    order = list(range(len(limits)))
    rng.shuffle(order)
    ps = [_mk(run, bid, "reward_per_cu", i, prices[i], lim, f"u{i}", order[i], i)
          for i, lim in enumerate(limits)]
    return Batch(bid, "reward_per_cu", ps, ["H1", "H2"])


def reverse_order(run: str, bid: str, rng: random.Random, n=8, base=1_000, step=1_000,
                  limit=DEFAULT_CU_LIMIT) -> Batch:
    """Fee ascending in send order, then (in the paired batch) descending: separates H1 from H3."""
    asc = bid.endswith("a")
    ps = [_mk(run, bid, "reverse_order", i, base + step * (i if asc else n - 1 - i), limit, f"u{i}", i, i)
          for i in range(n)]
    return Batch(bid, "reverse_order", ps, ["H1", "H3"])


def conflict_set(run: str, bid: str, rng: random.Random, n=8, price=1_000, limit=DEFAULT_CU_LIMIT) -> Batch:
    """First half share one writable account, second half all disjoint; identical fees."""
    ps = []
    order = list(range(n)); rng.shuffle(order)
    for i in range(n):
        key = "shared" if i < n // 2 else f"u{i}"
        ps.append(_mk(run, bid, "conflict_set", i, price, limit, key, order[i], i))
    return Batch(bid, "conflict_set", ps, ["H4"])


def timing_spread(run: str, bid: str, rng: random.Random, n=8, price=1_000, limit=DEFAULT_CU_LIMIT,
                  window_ms=1_200) -> Batch:
    """Identical txs sent at evenly spread offsets across the first 1.2 s of the 4-slot (1.6 s) window, so
    the last probe still lands inside the window even with ~300 ms of delivery latency."""
    ps = [_mk(run, bid, "timing_spread", i, price, limit, f"u{i}", i, i, offset=i * window_ms // n)
          for i in range(n)]
    return Batch(bid, "timing_spread", ps, ["H5"])


def control(run: str, bid: str, rng: random.Random, n=8, price=1_000, limit=DEFAULT_CU_LIMIT) -> Batch:
    order = list(range(n)); rng.shuffle(order)
    ps = [_mk(run, bid, "control", i, price, limit, f"u{i}", order[i], i) for i in range(n)]
    return Batch(bid, "control", ps, ["H6"])


def build_window_plan(run_id: str, seed: int, slot_s: float = 0.4) -> list[Batch]:
    """`slot_s` is the MEASURED slot time: b5 spreads its probes over 60 % of the 4-slot window (about 515 ms at
    mainnet's 0.214 s/slot on 2026-10-28; the old 1.2 s spread was longer than the whole window).
    One run = six batch kinds (reverse split in a/b) against ONE validator, one batch per window."""
    rng = random.Random(f"{seed}:{run_id}")
    return [
        fee_ladder(run_id, "b1", rng),
        reward_per_cu(run_id, "b2", rng),
        reverse_order(run_id, "b3a", rng),
        reverse_order(run_id, "b3b", rng),
        conflict_set(run_id, "b4", rng),
        timing_spread(run_id, "b5", rng, window_ms=int(0.6 * 4 * slot_s * 1000)),
        control(run_id, "b6", rng),
    ]


def estimate_cost(runs: int, validators: int, seed: int = 0) -> dict:
    per_run = sum(b.worst_case_lamports for b in build_window_plan("est", seed))
    total = per_run * runs * validators
    return {"per_run_lamports": per_run, "runs": runs, "validators": validators,
            "total_lamports": total, "total_sol": total / 1e9,
            "txs": sum(len(b.probes) for b in build_window_plan("est", seed)) * runs * validators}
