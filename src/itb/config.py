"""Config loading and secret handling. Keys come from a file path or env only."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from solders.keypair import Keypair

LAMPORTS_PER_SOL = 1_000_000_000
HARD_CEILING_LAMPORTS = 60_000_000  # 0.06 SOL: never exceeded regardless of flags (T16)
ALPENGLOW_ACTIVATION = "2026-10-21"


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    rpc_url: str
    rpc_url_fallback: str | None = None
    validators_app_token: str | None = None
    data_dir: Path = Path("data")
    targets_file: Path = Path("config/targets.yaml")
    live: bool = False
    cap_lamports: int = 0
    keypair_path: str | None = None
    experiment: dict = field(default_factory=dict)
    targets: list[dict] = field(default_factory=list)


def load_keypair(path_or_json: str) -> Keypair:
    """Load a keypair from a JSON-array file path or an inline JSON array. Never logs the secret."""
    raw = path_or_json.strip()
    try:
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_bytes(bytes(json.loads(Path(raw).read_text())))
    except FileNotFoundError:
        raise ConfigError("SOLANA_KEYPAIR points to a file that does not exist") from None
    except Exception as e:  # do not echo contents
        raise ConfigError(f"could not parse keypair ({type(e).__name__})") from None


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"\'')
    return out


def load_settings(*, live: bool = False, cap_sol: float | None = None,
                  env: dict[str, str] | None = None) -> Settings:
    if env is None:
        env = {**_dotenv(Path(".env")), **os.environ}
    if live and cap_sol is None:
        raise ConfigError("live mode requires --cap-sol")
    if cap_sol is not None and not live:
        raise ConfigError("--cap-sol only makes sense together with --live")
    cap = int(round((cap_sol or 0) * LAMPORTS_PER_SOL))
    if cap > HARD_CEILING_LAMPORTS:
        raise ConfigError(f"cap {cap} lamports exceeds hard ceiling {HARD_CEILING_LAMPORTS}")
    if live and cap <= 0:
        raise ConfigError("--cap-sol must be positive")

    kp = env.get("SOLANA_KEYPAIR")
    if live and not kp:
        raise ConfigError("live mode requires SOLANA_KEYPAIR (file path or JSON array) in env")

    s = Settings(
        rpc_url=env.get("ITB_RPC_URL", "https://api.mainnet-beta.solana.com"),
        rpc_url_fallback=env.get("ITB_RPC_URL_FALLBACK") or None,
        validators_app_token=env.get("VALIDATORS_APP_TOKEN") or None,
        data_dir=Path(env.get("ITB_DATA_DIR", "data")),
        targets_file=Path(env.get("ITB_TARGETS", "config/targets.yaml")),
        live=live, cap_lamports=cap, keypair_path=kp,
    )
    if s.targets_file.exists():
        doc = yaml.safe_load(s.targets_file.read_text()) or {}
        s.experiment = doc.get("experiment", {})
        s.targets = doc.get("targets") or []
    return s
