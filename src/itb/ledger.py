"""Persisted spend ledger with a hard cap, checked before every send (FR-06, NFR-03)."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .config import HARD_CEILING_LAMPORTS


class CapExceeded(RuntimeError):
    pass


class SpendLedger:
    """Reserve-then-send: `reserve` is called with the worst-case fee BEFORE sending, so a crash
    after reserve over-counts (safe) rather than under-counts. The cap only ever ratchets down
    across restarts, so a restart with a bigger --cap-sol cannot raise it past the first value
    unless the ledger file is deliberately deleted."""

    def __init__(self, path: Path, cap_lamports: int):
        if cap_lamports > HARD_CEILING_LAMPORTS:
            raise CapExceeded("cap above hard ceiling")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(".lock")
        with self._locked():
            st = self._read()
            st["cap_lamports"] = min(st["cap_lamports"], cap_lamports) if st["cap_lamports"] else cap_lamports
            self._write(st)

    # -- persistence -------------------------------------------------------
    def _locked(self):
        outer = self

        class _Ctx:
            def __enter__(self_):
                self_.f = open(outer._lock_path, "w")
                fcntl.flock(self_.f, fcntl.LOCK_EX)

            def __exit__(self_, *a):
                fcntl.flock(self_.f, fcntl.LOCK_UN)
                self_.f.close()

        return _Ctx()

    def _read(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"cap_lamports": 0, "spent_lamports": 0, "runs": {}}

    def _write(self, st: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    # -- api ---------------------------------------------------------------
    @property
    def spent(self) -> int:
        return self._read()["spent_lamports"]

    @property
    def cap(self) -> int:
        return self._read()["cap_lamports"]

    def remaining(self) -> int:
        st = self._read()
        return st["cap_lamports"] - st["spent_lamports"]

    def reserve(self, lamports: int, run_id: str) -> int:
        """Atomically add `lamports` to spend or raise CapExceeded. Returns new cumulative spend."""
        if lamports < 0:
            raise ValueError("negative spend")
        with self._locked():
            st = self._read()
            if st["spent_lamports"] + lamports > st["cap_lamports"]:
                raise CapExceeded(
                    f"refusing send: spent {st['spent_lamports']} + {lamports} > cap {st['cap_lamports']}")
            st["spent_lamports"] += lamports
            st["runs"][run_id] = st["runs"].get(run_id, 0) + lamports
            self._write(st)
            return st["spent_lamports"]
