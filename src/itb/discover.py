"""Validator discovery (FR-01 / T04): getClusterNodes + getVoteAccounts (+ validators.app)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from .rpc import Rpc


# Exact validators.app `software_client` values -> family. Substring matching misfiles
# HarmonicAgave/HarmonicFiredancer/FireBAM as plain Agave/Firedancer, so it is exact on purpose.
APP_CLIENT_FAMILY = {
    "agave": "agave", "jitolabs": "jito-agave", "agavebam": "agave-bam",
    "firedancer": "firedancer", "firebam": "firedancer-bam", "paladin": "paladin",
    "harmonicagave": "harmonic-agave", "harmonicfiredancer": "harmonic-firedancer",
}


def classify_client(version: str | None, app_client: str | None = None) -> str:
    """Client family. `getClusterNodes` alone cannot tell Jito/BAM from Agave (same version
    string), so validators.app's software_client is authoritative when known. Without it we
    only claim what the version string supports and mark the rest unverified."""
    a = (app_client or "").strip().lower()
    if a in APP_CLIENT_FAMILY:
        return APP_CLIENT_FAMILY[a]
    if a and not a.startswith("unknown"):
        return "other:" + re.sub(r"[^a-z0-9]+", "-", a)  # e.g. rakurai, raiku: kept separate
    v = version or ""
    if v.startswith("0."):  # Firedancer/Frankendancer use 0.x.y numbering
        return "firedancer-family-unverified"
    return "unverified"


def fetch_validators_app(token: str | None) -> dict[str, dict]:
    if not token:
        return {}
    r = httpx.get("https://www.validators.app/api/v1/validators/mainnet.json",
                  headers={"Token": token}, params={"limit": 1000, "order": "stake"}, timeout=60)
    r.raise_for_status()
    return {v["account"]: v for v in r.json() if v.get("account")}


def discover(rpc: Rpc, out_dir: Path, token: str | None = None, limit: int = 500) -> list[dict]:
    nodes = {n["pubkey"]: n for n in rpc.call("getClusterNodes")}
    votes = rpc.call("getVoteAccounts")["current"]
    app = fetch_validators_app(token)
    ts = int(time.time())
    rows = []
    for v in sorted(votes, key=lambda x: -x["activatedStake"])[:limit]:
        ident = v["nodePubkey"]
        node = nodes.get(ident, {})
        ver = node.get("version")
        client = (app.get(ident) or {}).get("software_client")
        rows.append({"identity": ident, "vote_pubkey": v["votePubkey"], "stake": v["activatedStake"],
                     "version": ver, "client_family": classify_client(ver, client),
                     "jito": (app.get(ident) or {}).get("jito"),
                     "software_client": client,
                     "source": "getClusterNodes+getVoteAccounts" + ("+validators.app" if client else ""),
                     "snapshot_ts": ts})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "validators.json").write_text(json.dumps(rows, indent=1))
    return rows
