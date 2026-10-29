"""env.json (section 7 of the spec): what a run was measured with. Never stores the RPC key."""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

NTP_EPOCH_OFFSET = 2_208_988_800


def sntp_offset(server: str = "pool.ntp.org", timeout: float = 3.0) -> float | None:
    """Local clock minus NTP time, in seconds (positive = our clock is ahead). None if unreachable.
    Single-packet SNTP: good to a few ms, which is what matters next to ~400 ms slots."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            pkt = b"\x1b" + 47 * b"\0"
            t0 = time.time()
            s.sendto(pkt, (server, 123))
            data, _ = s.recvfrom(48)
            t3 = time.time()
        sec, frac = struct.unpack("!II", data[40:48])
        server_t = sec - NTP_EPOCH_OFFSET + frac / 2**32
        return (t0 + t3) / 2 - server_t
    except OSError:
        return None


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    except Exception:
        return "unknown"


def rpc_host(url: str) -> str:
    """Provider host only: API keys live in the path/query and must never be recorded."""
    return urlparse(url).hostname or "unknown"


def write_env(run_dir: Path, rpc_url: str, targets: list[dict] | None = None, live: bool = False,
              snapshot: list[dict] | None = None) -> dict:
    env = {"date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_sha": git_sha(),
           "image_digest": None, "rpc_provider_host": rpc_host(rpc_url),
           "clock_offset_s": sntp_offset(), "live": live,
           # client identity at measurement time when `snapshot` is given (itb.snapshot), else the selection
           "targets_snapshot": snapshot if snapshot is not None else
           [{k: t.get(k) for k in ("identity", "client_family", "version")} for t in (targets or [])],
           "client_version_changes": [t["identity"] for t in (snapshot or []) if t.get("changed")]}
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "env.json").write_text(json.dumps(env, indent=1))
    return env
