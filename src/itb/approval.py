"""The T16 human gate. Live sending is impossible without a sign-off file written by a person
(`itb approve`), bound to the exact target list and to a cap at or below what they approved."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .config import HARD_CEILING_LAMPORTS

APPROVAL_FILE = Path("config/live_approval.json")


class ApprovalError(RuntimeError):
    pass


def targets_digest(targets: list[dict]) -> str:
    return hashlib.sha256("\n".join(sorted(t["identity"] for t in targets)).encode()).hexdigest()


def write_approval(path: Path, by: str, cap_lamports: int, targets: list[dict],
                   payer_funding_lamports: int) -> dict:
    if cap_lamports > HARD_CEILING_LAMPORTS:
        raise ApprovalError("cap above hard ceiling")
    doc = {"approved_by": by, "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "cap_lamports": cap_lamports, "payer_funding_lamports": payer_funding_lamports,
           "targets_sha256": targets_digest(targets), "n_targets": len(targets)}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=1))
    return doc


def check_approval(path: Path, cap_lamports: int, targets: list[dict]) -> dict:
    p = Path(path)
    if not p.exists():
        raise ApprovalError(f"no sign-off at {p}: a human must run `itb approve` first (task T16)")
    doc = json.loads(p.read_text())
    if cap_lamports > doc["cap_lamports"]:
        raise ApprovalError(f"--cap-sol exceeds the approved cap ({doc['cap_lamports']} lamports)")
    if doc["targets_sha256"] != targets_digest(targets):
        raise ApprovalError("targets changed since sign-off: re-run `itb approve`")
    return doc
