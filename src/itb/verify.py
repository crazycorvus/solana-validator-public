"""Verification helper (T23 / FR-13): re-check every landing claim directly against the chain.

Trusts nothing in `data/raw/<run>/blocks`: for each landed probe it re-fetches the block (signatures
only, finalized) and asserts the slot, the index in the block and the leader. Dropped probes are
re-checked with getSignatureStatuses. Rank-in-batch is recomputed from the verified positions, and
the committed verdicts are re-derived from landings.csv and compared byte-for-byte.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .collect import SKIPPED_CODES
from .rpc import Rpc, RpcError

SIG_OPTS = {"transactionDetails": "signatures", "rewards": False, "commitment": "finalized",
            "maxSupportedTransactionVersion": 1}


def _block_sigs(rpc: Rpc, slot: int, cache: dict) -> list[str] | None:
    if slot not in cache:
        try:
            cache[slot] = rpc.call("getBlock", [slot, SIG_OPTS])["signatures"]
        except RpcError as e:
            if e.code in SKIPPED_CODES:
                cache[slot] = None
            else:
                raise
    return cache[slot]


def verify_run(rpc: Rpc, run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    with open(run_dir / "landings.csv") as f:
        rows = list(csv.DictReader(f))
    problems: list[dict] = []
    cache: dict[int, list[str] | None] = {}
    leaders: dict[int, str] = {}
    checked = {"landed": 0, "dropped": 0}

    def bad(r, why):
        problems.append({"signature": r["signature"], "probe_id": r["probe_id"], "problem": why})

    verified_pos: dict[str, tuple[int, int]] = {}
    for r in rows:
        if r["outcome"] == "dropped":
            continue
        checked["landed"] += 1
        slot, idx = int(r["slot"]), int(r["tx_index_in_block"])
        sigs = _block_sigs(rpc, slot, cache)
        if sigs is None:
            bad(r, f"slot {slot} is skipped on chain")
            continue
        if idx >= len(sigs) or sigs[idx] != r["signature"]:
            where = sigs.index(r["signature"]) if r["signature"] in sigs else None
            bad(r, f"slot {slot}: claimed index {idx}, chain has it at {where}")
            continue
        verified_pos[r["signature"]] = (slot, idx)
        if slot not in leaders:
            leaders[slot] = rpc.call("getSlotLeaders", [slot, 1])[0]
        if leaders[slot] != r["leader"]:
            bad(r, f"slot {slot}: leader on chain is {leaders[slot]}, claimed {r['leader']}")

    dropped = [r for r in rows if r["outcome"] == "dropped"]
    for i in range(0, len(dropped), 200):
        chunk = dropped[i:i + 200]
        st = rpc.call("getSignatureStatuses", [[r["signature"] for r in chunk],
                                               {"searchTransactionHistory": True}])["value"]
        for r, s in zip(chunk, st):
            checked["dropped"] += 1
            if s is not None:
                bad(r, f"claimed dropped but it landed in slot {s['slot']}")

    # rank_in_batch must equal order of verified (slot, index) among landed probes of that batch
    by_batch: dict[str, list[dict]] = {}
    for r in rows:
        if r["outcome"] == "landed" and r["signature"] in verified_pos:
            by_batch.setdefault(r["batch_id"] + "|" + r["probe_id"].rsplit(":", 2)[0], []).append(r)
    for rs in by_batch.values():
        for rank, r in enumerate(sorted(rs, key=lambda r: verified_pos[r["signature"]])):
            if str(rank) != r["rank_in_batch"]:
                bad(r, f"rank_in_batch claimed {r['rank_in_batch']}, recomputed {rank}")

    report = {"run": run_dir.name, "checked": checked, "problems": problems, "ok": not problems}
    (run_dir / "verify_report.json").write_text(json.dumps(report, indent=1))
    return report


def verify_verdicts(run_dirs: list[Path], verdicts_path: Path, seed: int, n_perm: int = 10_000) -> dict:
    """Re-derive verdicts from landings.csv; they must equal the committed file exactly."""
    from .infer import analyze
    tmp = Path(verdicts_path).with_suffix(".rederived.json")
    again = analyze(run_dirs, tmp, seed, n_perm)
    same = json.loads(Path(verdicts_path).read_text()) == json.loads(json.dumps(again))
    tmp.unlink(missing_ok=True)
    return {"verdicts_reproduced": same}
