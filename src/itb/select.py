"""Target selector (T05 / FR-02): >=6 validators over >=3 client groups with windows in the next 24h."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from .rpc import Rpc
from .schedule import LeaderSchedule

SLOTS_PER_24H = 216_000
# Plain Agave is the documented baseline, so it must be in; the rest are the big-stake forks.
PRIORITY = ["agave", "jito-agave", "agave-bam", "firedancer", "firedancer-bam", "harmonic-agave",
            "harmonic-firedancer", "paladin"]


def select_targets(rows: list[dict], sched: LeaderSchedule, current_slot: int, *, n=6, min_families=3,
                   min_windows=3, per_family_cap=2) -> tuple[list[dict], list[str]]:
    """Greedy: highest-stake validators first, but round-robin across client groups so that no
    family fills the list. Only validators with >= min_windows full 4-slot windows in 24h qualify
    (3 runs need 3 distinct windows; more is better for scheduling slack)."""
    log: list[str] = []
    horizon = current_slot + SLOTS_PER_24H
    by_fam: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda x: -x["stake"]):
        ws = [w for w in sched.windows(r["identity"], current_slot) if w[0] <= horizon]
        if len(ws) < min_windows:
            log.append(f"skip {r['identity'][:8]} ({r['client_family']} {r['version']}): {len(ws)} windows < {min_windows}")
            continue
        by_fam.setdefault(r["client_family"], []).append({**r, "windows_24h": len(ws)})
    chosen: list[dict] = []
    # `unknown` is not a family we can claim in the report
    fams = sorted((f for f in by_fam if f != "unverified"),
                  key=lambda f: (PRIORITY.index(f) if f in PRIORITY else len(PRIORITY), -by_fam[f][0]["stake"]))
    for rnd in range(max(per_family_cap, 5)):  # later rounds only fill a short list
        for f in fams:
            if len(chosen) < n and rnd < len(by_fam[f]) and (rnd < per_family_cap or len(chosen) < n):
                v = by_fam[f][rnd]
                chosen.append({"identity": v["identity"], "vote_pubkey": v["vote_pubkey"],
                               "client_family": f, "version": v["version"], "stake": v["stake"],
                               "windows_24h": v["windows_24h"],
                               "rationale": f"#{rnd + 1} by stake in {f} with {v['windows_24h']} full windows in 24h"})
    if len({c["client_family"] for c in chosen}) < min_families or len(chosen) < n:
        log.append(f"WARNING: got {len(chosen)} targets over {len({c['client_family'] for c in chosen})} groups; "
                   f"need {n}/{min_families}. Add validators.app token or relax per_family_cap.")
    for c in chosen:
        log.append(f"pick {c['identity'][:8]} {c['client_family']} {c['version']}: {c['rationale']}")
    return chosen, log


def write_targets(path: Path, chosen: list[dict]) -> None:
    doc = yaml.safe_load(path.read_text()) if path.exists() else {}
    doc["targets"] = chosen
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
