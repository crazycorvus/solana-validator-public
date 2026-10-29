"""Inference engine (T18 / FR-10): tests H1-H6 from docs/DESIGN.md per validator.

Pure Python (n per batch is <= 8) and fully deterministic: every permutation test is seeded.
Position = rank among OUR probes of one batch (`rank_in_batch`), so competing traffic is factored out.
"""
from __future__ import annotations

import csv
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

BASE_FEE = 5_000
TAU_MIN, P_MAX, RUN_AGREE_MIN, TYPE_FLOOR = 0.6, 0.01, 2 / 3, 0.3
EFFECT_H4, NOISE_TAU, MIN_LANDED, MIN_PAIRS = 0.5, 0.3, 16, 30
# which batch types feed which hypothesis (see DESIGN.md)
H123_TYPES = ("fee_ladder", "reward_per_cu", "reverse_order")


def reward_per_cu(cu_price: int, cu_limit: int) -> float:
    """H2 score: (priority fee + base_fee/2) / requested CU limit, in lamports per CU."""
    return (cu_price * cu_limit / 1e6 + BASE_FEE / 2) / cu_limit


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class Batch:
    run_id: str
    batch_id: str
    batch_type: str
    x: dict[str, list[float]]           # variable name -> value per probe
    pos: list[int]                      # rank among our landed probes
    shared: list[bool] = field(default_factory=list)
    path: str = ""
    group: list[int] | None = None      # slot of each probe; order is only compared inside a slot


def _pairs(n: int, grp: list[int] | None):
    """Index pairs that may be compared. Intra-block order is only meaningful between probes that landed
    in the SAME slot: across slots the order is simply arrival time (slot 1 precedes slot 2 whatever the
    leader's rule is), which would otherwise masquerade as FIFO or hide a fee rule."""
    return [(i, j) for i in range(n) for j in range(i + 1, n) if grp is None or grp[i] == grp[j]]


def n_pairs(batches: list["Batch"]) -> int:
    return sum(len(_pairs(len(b.pos), b.group)) for b in batches)


def tau_b(score: list[float], pos: list[int], grp: list[int] | None = None) -> float | None:
    """Kendall tau-b between score (higher = predicted earlier) and earliness (-position) over same-slot
    pairs. +1 = the score perfectly predicts order; None when there is no information."""
    pr = _pairs(len(score), grp)
    n0 = len(pr)
    ties_x = sum(1 for i, j in pr if score[i] == score[j])
    if n0 - ties_x == 0:
        return None
    s = sum(_sign(score[i] - score[j]) * _sign(pos[j] - pos[i]) for i, j in pr)
    return s / math.sqrt((n0 - ties_x) * n0)


def pairwise_consistency(score, pos, grp=None) -> float | None:
    """Share of same-slot pairs with different scores ordered the way the score predicts."""
    ok = tot = 0
    for i, j in _pairs(len(score), grp):
        if score[i] != score[j]:
            tot += 1
            ok += _sign(score[i] - score[j]) == _sign(pos[j] - pos[i])
    return ok / tot if tot else None


def spearman(x, pos) -> float | None:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    a, b = ranks(list(x)), ranks(list(pos))
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((p - ma) * (q - mb) for p, q in zip(a, b))
    den = math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - mb) ** 2 for q in b))
    return num / den if den else None


def rank_biserial(shared: list[bool], pos: list[int], grp: list[int] | None = None) -> float | None:
    """+1: shared-account probes always landed AFTER disjoint ones (same-slot pairs); -1: always before."""
    u = n = 0.0
    for i, j in _pairs(len(pos), grp):
        if shared[i] == shared[j]:
            continue
        a, b = (i, j) if shared[i] else (j, i)
        n += 1
        u += (pos[a] > pos[b]) + 0.5 * (pos[a] == pos[b])
    return 2 * u / n - 1 if n else None


def shuffled(b: "Batch", rng: random.Random) -> "Batch":
    """Null copy: positions permuted within each slot group (all probes if no groups)."""
    p = b.pos[:]
    idx_by_g: dict = defaultdict(list)
    for i, g in enumerate(b.group or [0] * len(p)):
        idx_by_g[g].append(i)
    for idxs in idx_by_g.values():
        vals = [p[i] for i in idxs]
        rng.shuffle(vals)
        for i, v in zip(idxs, vals):
            p[i] = v
    return Batch(b.run_id, b.batch_id, b.batch_type, b.x, p, b.shared, b.path, b.group)


def _pooled(batches: list[Batch], stat) -> float | None:
    vals = [v for v in (stat(b) for b in batches) if v is not None]
    return sum(vals) / len(vals) if vals else None


def perm_p(batches: list[Batch], stat, rng: random.Random, n_perm: int, two_sided=False) -> float | None:
    """Permutation p-value for the pooled statistic: shuffle positions inside each batch."""
    obs = _pooled(batches, stat)
    if obs is None:
        return None
    hit = 0
    for _ in range(n_perm):
        perm = [shuffled(b, rng) for b in batches]
        v = _pooled(perm, stat)
        if v is not None and ((abs(v) >= abs(obs) - 1e-12) if two_sided else (v >= obs - 1e-12)):
            hit += 1
    return (1 + hit) / (1 + n_perm)


def _stat_for(h: str):
    return {
        "H1": lambda b: tau_b(b.x["cu_price"], b.pos, b.group),
        "H2": lambda b: tau_b(b.x["reward_per_cu"], b.pos, b.group),
        "H3": lambda b: tau_b([-v for v in b.x["send_order"]], b.pos, b.group),
        "H4": lambda b: rank_biserial(b.shared, b.pos, b.group),
        "H5": lambda b: spearman(b.x["send_offset_ms"], b.pos),
    }[h]


def _types_for(h: str) -> tuple[str, ...]:
    return {"H1": H123_TYPES, "H2": H123_TYPES, "H3": H123_TYPES, "H4": ("conflict_set",),
            "H5": ("timing_spread",)}[h]


def holm(ps: dict[str, float | None]) -> dict[str, float | None]:
    items = sorted(((k, v) for k, v in ps.items() if v is not None), key=lambda kv: kv[1])
    out, prev, m = {k: None for k in ps}, 0.0, len(items)
    for i, (k, v) in enumerate(items):
        prev = max(prev, min(1.0, (m - i) * v))
        out[k] = prev
    return out


def test_hypothesis(h: str, batches: list[Batch], rng: random.Random, n_perm: int) -> dict:
    use = [b for b in batches if b.batch_type in _types_for(h)]
    stat = _stat_for(h)
    eff = _pooled(use, stat)
    two = h == "H4"
    p = perm_p(use, stat, rng, n_perm, two_sided=two)
    by_run: dict[str, list[Batch]] = defaultdict(list)
    for b in use:
        by_run[b.run_id].append(b)
    per_run = {r: _pooled(bs, stat) for r, bs in by_run.items()}
    thr = EFFECT_H4 if two else TAU_MIN
    agree_vals = [v for v in per_run.values() if v is not None]
    ok = [(abs(v) if two else v) >= thr for v in agree_vals]
    if two and ok:  # same sign across agreeing runs
        ok = [o and _sign(v) == _sign(eff or 0) for o, v in zip(ok, agree_vals)]
    by_type: dict[str, list[Batch]] = defaultdict(list)
    for b in use:
        by_type[b.batch_type].append(b)
    type_means = {t: _pooled(bs, stat) for t, bs in by_type.items()}
    tvals = [v for v in type_means.values() if v is not None]
    floor_ok = all((abs(v) if two else v) >= TYPE_FLOOR for v in tvals) if tvals and not two else True
    cons = [c for c in (pairwise_consistency(_score(h, b), b.pos, b.group) for b in use if h in ("H1", "H2", "H3")) if c is not None]
    agree = sum(ok) / len(ok) if ok else None
    passed = bool(eff is not None and p is not None and (abs(eff) if two else eff) >= thr and p < P_MAX
                  and agree is not None and agree >= RUN_AGREE_MIN and floor_ok)
    return {"effect": eff, "p": p, "runs": per_run, "run_agreement": agree, "by_type": type_means,
            "pairwise_consistency": sum(cons) / len(cons) if cons else None, "n_batches": len(use),
            "passed": passed}


def _score(h, b):
    return {"H1": b.x["cu_price"], "H2": b.x["reward_per_cu"], "H3": [-v for v in b.x["send_order"]]}[h]


def verdict_for(batches: list[Batch], seed: int = 0, n_perm: int = 10_000, label: str = "") -> dict:
    landed = sum(len(b.pos) for b in batches)
    base = {"validator": label, "n_landed_probes": landed, "n_batches": len(batches)}
    base["n_same_slot_pairs"] = n_pairs(batches)
    if landed < MIN_LANDED or base["n_same_slot_pairs"] < MIN_PAIRS:
        return {**base, "verdict": "insufficient data", "confidence": None, "tests": {}}
    rng = random.Random(f"{seed}:{label}")
    tests = {h: test_hypothesis(h, batches, rng, n_perm) for h in ("H1", "H2", "H3", "H4", "H5")}
    adj = holm({h: t["p"] for h, t in tests.items()})
    for h, t in tests.items():
        t["p_holm"] = adj[h]
    primary = [h for h in ("H1", "H2", "H3") if tests[h]["passed"]]
    # b5 has identical fees, so H5's statistic is really "do equal-fee txs land in arrival order":
    # entry/micro-block clustering cannot be tested without entry boundaries (not in getBlock).
    mod_name = {"H4": "H4(conflict effect)", "H5": "arrival-order tie-break"}
    mods = [mod_name[h] for h in ("H4", "H5") if tests[h]["passed"]]
    ctl = [b for b in batches if b.batch_type == "control"]
    ctl_tau = {k: _pooled(ctl, lambda b, k=k: tau_b([-v for v in b.x[k]] if k == "send_order" else b.x[k], b.pos, b.group))
               for k in ("send_order",)}
    quiet = all(v is None or abs(v) < NOISE_TAU for v in ctl_tau.values())
    weak = any(t["effect"] is not None and NOISE_TAU <= abs(t["effect"]) < TAU_MIN
               for h, t in tests.items() if h in ("H1", "H2", "H3"))
    if len(primary) == 1:
        v, conf = primary[0], tests[primary[0]]["run_agreement"]
        if mods:
            v += " + " + " + ".join(mods)
    elif len(primary) > 1:
        v, conf = "ambiguous: " + "/".join(primary), None
    elif mods:
        v, conf = " + ".join(mods) + " only", min(t["run_agreement"] or 0 for h, t in tests.items() if mod_name[h] in mods)
    elif quiet and not weak:
        v, conf = "H6/noise", None
    else:
        v, conf = "mixed", None
    return {**base, "verdict": v, "confidence": conf, "control_tau_vs_send_order": ctl_tau, "tests": tests}


# ---------------------------------------------------------------- loading raw data
def load_batches(run_dirs: list[Path]) -> dict[str, list[Batch]]:
    """validator (expected leader) -> batches, from probes.csv + landings.csv. Only probes that
    landed in their target leader's window are used; the rest are counted by the caller."""
    out: dict[str, list[Batch]] = defaultdict(list)
    for rd in run_dirs:
        with open(rd / "probes.csv") as f:
            probes = {r["signature"]: r for r in csv.DictReader(f)}
        with open(rd / "landings.csv") as f:
            lands = [r for r in csv.DictReader(f) if r["outcome"] == "landed"]
        groups: dict[tuple, list[tuple[dict, dict]]] = defaultdict(list)
        for l in lands:
            p = probes[l["signature"]]
            groups[(p["leader"], p["run_id"], p["batch_id"], p["batch_type"])].append((p, l))
        for (leader, run_id, bid, btype), rows in groups.items():
            rows.sort(key=lambda pl: int(pl[1]["rank_in_batch"]))
            pr = [p for p, _ in rows]
            fl = lambda k: [float(p[k]) for p in pr]  # noqa: E731
            x = {"cu_price": fl("cu_price"), "send_order": fl("send_order"),
                 "send_offset_ms": fl("send_offset_ms"),
                 "reward_per_cu": [reward_per_cu(int(p["cu_price"]), int(p["cu_limit"])) for p in pr]}
            paths = {p["path"] for p in pr}
            out[leader].append(Batch(run_id, bid, btype, x, list(range(len(pr))),
                                     [p["accounts_key"] == "shared" for p in pr],
                                     paths.pop() if len(paths) == 1 else "mixed",
                                     [int(l["slot"]) for _, l in rows]))
    return out


def analyze(run_dirs: list[Path], out_path: Path, seed: int = 0, n_perm: int = 10_000) -> list[dict]:
    by_val = load_batches(run_dirs)
    res = [verdict_for(bs, seed, n_perm, label=v) for v, bs in sorted(by_val.items())]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(res, indent=1))
    return res
