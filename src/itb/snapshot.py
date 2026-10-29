"""Client identity AT MEASUREMENT TIME (task: validators are "identifiable by version string or
validators.app data at the time of measurement"). Re-read at the start of every run, not copied from
the earlier target selection, and any drift is flagged."""
from __future__ import annotations

from .discover import classify_client, fetch_validators_app
from .rpc import Rpc


def snapshot_targets(rpc: Rpc, targets: list[dict], token: str | None) -> list[dict]:
    try:
        nodes = {n["pubkey"]: n for n in rpc.call("getClusterNodes")}
    except Exception:
        nodes = {}
    try:
        app = fetch_validators_app(token) if token else {}
    except Exception:
        app = {}
    out = []
    for t in targets:
        ver = (nodes.get(t["identity"]) or {}).get("version")
        a = app.get(t["identity"]) or {}
        client = a.get("software_client")
        if client:
            fam, src = classify_client(ver, client), "getClusterNodes+validators.app"
        elif ver == t.get("version"):  # version unchanged and no second source: keep the selected family
            fam, src = t.get("client_family"), "getClusterNodes (version unchanged)"
        else:
            fam, src = "unverified", "getClusterNodes (version changed, no validators.app)"
        out.append({"identity": t["identity"], "client_family": fam, "version": ver,
                    "software_client": client, "jito": a.get("jito"), "source": src,
                    "selected_family": t.get("client_family"), "selected_version": t.get("version"),
                    "changed": fam != t.get("client_family") or ver != t.get("version")})
    return out
