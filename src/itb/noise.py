"""Noise analysis (T19): how much of what we see is delivery noise, path effect or run-to-run
instability, versus rule. Also summarises competing traffic in the same blocks (FR-14)."""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

from .collect import _load, excluded_signatures
from .infer import Batch, _pooled, _stat_for, _types_for, shuffled, tau_b


def landing_stats(run_dirs: list[Path]) -> dict:
    """Delivery noise: outcome counts per (validator, path) and per batch type."""
    out: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for rd in run_dirs:
        with open(rd / "probes.csv") as f:
            probes = {r["signature"]: r for r in csv.DictReader(f)}
        excl, _ = excluded_signatures(rd)
        with open(rd / "landings.csv") as f:
            for l in csv.DictReader(f):
                if l["signature"] in excl:  # our own funding failure, not delivery behaviour
                    continue
                p = probes[l["signature"]]
                key = f"{p['leader']}|{p['path']}"
                out[key]["_all"][l["outcome"]] += 1
                out[key][p["batch_type"]][l["outcome"]] += 1
    res = {}
    for key, by in out.items():
        res[key] = {bt: {**dict(c), "n": sum(c.values()),
                         "landing_rate": c.get("landed", 0) / sum(c.values())} for bt, c in by.items()}
    return res


def run_stability(batches: list[Batch], h: str) -> dict:
    """Per-run pooled effect for hypothesis h, and how much it moves between runs."""
    stat = _stat_for(h)
    use = [b for b in batches if b.batch_type in _types_for(h)]
    by_run: dict[str, list[Batch]] = defaultdict(list)
    for b in use:
        by_run[b.run_id].append(b)
    per = {r: _pooled(bs, stat) for r, bs in by_run.items()}
    vals = [v for v in per.values() if v is not None]
    if len(vals) < 2:
        return {"per_run": per, "n_runs": len(vals), "sd": None, "sign_consistent": None}
    return {"per_run": per, "n_runs": len(vals), "mean": statistics.mean(vals),
            "sd": statistics.stdev(vals), "range": [min(vals), max(vals)],
            "sign_consistent": len({(v > 0) - (v < 0) for v in vals}) == 1}


def null_sd(batches: list[Batch], h: str, seed: str, n_perm: int = 2000) -> float | None:
    """SD of the pooled statistic when positions are shuffled: the 'pure noise' yardstick."""
    stat = _stat_for(h)
    use = [b for b in batches if b.batch_type in _types_for(h)]
    rng = random.Random(seed)
    vals = []
    for _ in range(n_perm):
        sh = []
        for b in use:
            sh.append(shuffled(b, rng))
        v = _pooled(sh, stat)
        if v is not None:
            vals.append(v)
    return statistics.pstdev(vals) if len(vals) > 1 else None


def path_effect(batches: list[Batch], h: str, seed: str, n_perm: int = 5000) -> dict | None:
    """Does the delivery path change what we measure? Compares the pooled effect of hypothesis h
    between two paths; p from permuting path labels across batches within each batch type."""
    stat = _stat_for(h)
    use = [b for b in batches if b.batch_type in _types_for(h) and b.path not in ("", "mixed", "dry")]
    paths = sorted({b.path for b in use})
    if len(paths) != 2:
        return None
    a, b_ = paths

    def diff(assign):
        ea = _pooled([x for x, p in zip(use, assign) if p == a], stat)
        eb = _pooled([x for x, p in zip(use, assign) if p == b_], stat)
        return None if ea is None or eb is None else ea - eb
    obs_assign = [x.path for x in use]
    obs = diff(obs_assign)
    if obs is None:
        return None
    rng = random.Random(seed)
    strata: dict[str, list[int]] = defaultdict(list)
    for i, x in enumerate(use):
        strata[x.batch_type].append(i)
    hit = 0
    for _ in range(n_perm):
        assign = obs_assign[:]
        for idxs in strata.values():
            labs = [obs_assign[i] for i in idxs]
            rng.shuffle(labs)
            for i, l in zip(idxs, labs):
                assign[i] = l
        d = diff(assign)
        if d is not None and abs(d) >= abs(obs) - 1e-12:
            hit += 1
    ea = _pooled([x for x in use if x.path == a], stat)
    eb = _pooled([x for x in use if x.path == b_], stat)
    return {"hypothesis": h, "paths": [a, b_], "effect": {a: ea, b_: eb}, "difference": obs,
            "p": (1 + hit) / (1 + n_perm), "n_batches": {a: sum(x.path == a for x in use),
                                                           b_: sum(x.path == b_ for x in use)}}


def competition_context(run_dirs: list[Path], max_txs: int = 250, seed: int = 0) -> dict:
    """Competing (non-vote, non-probe) traffic in the blocks that contain our probes. The context tau
    (price vs block position among competitors, n in the hundreds) is a large-sample hint about the
    leader's rule that does not depend on our probes at all; it is supplementary, not primary."""
    per_leader: dict[str, list[dict]] = defaultdict(list)
    for rd in run_dirs:
        leaders = {int(k): v for k, v in json.loads((rd / "leaders.json").read_text()).items()}
        with open(rd / "probes.csv") as f:
            ours = {r["signature"] for r in csv.DictReader(f)}
        with open(rd / "landings.csv") as f:
            our_slots = {int(r["slot"]) for r in csv.DictReader(f) if r["slot"]}
        for p in sorted((rd / "blocks").glob("*.json*")):
            b = _load(p)
            if b.get("skipped") or b["slot"] not in our_slots:
                continue
            if not b.get("compact"):
                from .collect import compact_block
                b = compact_block(b)
            others = [(i, t) for i, t in enumerate(b["transactions"])
                      if not t.get("vote") and t["sig"] not in ours]
            if len(others) < 20:
                continue
            prices = [t["cu_price"] or 0 for _, t in others]
            sample = others
            if len(others) > max_txs:
                rng = random.Random(f"{seed}:{b['slot']}")
                sample = sorted(rng.sample(others, max_txs), key=lambda x: x[0])
            tau = tau_b([t["cu_price"] or 0 for _, t in sample], [i for i, _ in sample])  # one block = one slot
            ps = sorted(prices)
            q = lambda f: ps[min(len(ps) - 1, int(f * len(ps)))]  # noqa: E731
            per_leader[leaders.get(b["slot"], "?")].append({
                "slot": b["slot"], "n_competing": len(others), "share_priority_fee": sum(x > 0 for x in prices) / len(prices),
                "price_p50": q(0.5), "price_p90": q(0.9), "price_p99": q(0.99), "context_tau": tau})
    out = {}
    for who, rows in per_leader.items():
        taus = [r["context_tau"] for r in rows if r["context_tau"] is not None]
        out[who] = {"blocks": rows, "n_blocks": len(rows),
                    "context_tau_mean": statistics.mean(taus) if taus else None,
                    "context_tau_sd": statistics.stdev(taus) if len(taus) > 1 else None}
    return out


def noise_report(batches_by_val: dict[str, list[Batch]], run_dirs: list[Path], seed: int = 0,
                 n_perm: int = 2000) -> dict:
    rep: dict = {"delivery": landing_stats(run_dirs), "competition": competition_context(run_dirs, seed=seed),
                 "validators": {}}
    for v, bs in batches_by_val.items():
        sec: dict = {}
        for h in ("H1", "H2", "H3"):
            stab = run_stability(bs, h)
            nsd = null_sd(bs, h, f"{seed}:{v}:{h}", n_perm)
            eff = _pooled([b for b in bs if b.batch_type in _types_for(h)], _stat_for(h))
            sec[h] = {"stability": stab, "null_sd": nsd, "effect": eff,
                      "effect_over_null_sd": (eff / nsd) if eff is not None and nsd else None,
                      "path": path_effect(bs, h, f"{seed}:{v}:{h}:path", n_perm)}
        rep["validators"][v] = sec
    return rep
