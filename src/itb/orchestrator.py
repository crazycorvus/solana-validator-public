"""Live run orchestrator (T17): walks the schedule, one batch per leader window, at-most-once.

Safety properties, all enforced here and covered by tests:
  * a batch is marked started BEFORE it is sent, so a crash/restart can never send it twice;
  * a batch that would start late is skipped, not sent late (it would land outside its window);
  * the ledger reserves the whole batch before it fires, a cap breach stops the run;
  * payers are swept back in `finally`.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from solders.hash import Hash
from solders.keypair import Keypair

from .collect import collect_slots, compact_blocks, map_landings
from .ledger import CapExceeded
from .matrix import Batch, build_window_plan
from .schedule import LeaderSchedule
from .sender import Sender, payer_pool, prepare

SLOT_S = 0.4


@dataclass
class Item:
    validator: str
    family: str
    run_id: str
    batch: Batch
    window: list[int]

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.batch.batch_id}"


def build_schedule(targets: list[dict], sched: LeaderSchedule, now_slot: int, *, stamp: str, seed: int,
                   runs: int, min_ahead_slots: int, min_gap_s: float, horizon_slots: int,
                   slot_s: float = SLOT_S) -> tuple[list[Item], list[str]]:
    """Distinct windows for every (validator, run, batch). Each run starts at a different quantile of the
    horizon (windows of different runs are far apart); items are >= min_gap_s apart globally so sends,
    blockhash refresh and the shared payer pool never overlap. Returns (items, skipped reasons)."""
    taken: list[int] = []  # window start slots already used
    gap = max(1, round(min_gap_s / slot_s))
    items: list[Item] = []
    skipped: list[str] = []
    for tgt in targets:
        v = tgt["identity"]
        wins = [w for w in sched.windows(v, now_slot + min_ahead_slots) if w[0] <= now_slot + horizon_slots]
        for r in range(runs):
            run_id = f"{stamp}-{v[:6]}-r{r + 1}"
            batches = build_window_plan(run_id, seed, slot_s)
            i = (len(wins) * r) // runs
            for b in batches:
                while i < len(wins) and any(abs(wins[i][0] - t) < gap for t in taken):
                    i += 1
                if i >= len(wins):
                    skipped.append(f"{v[:8]} run {r + 1} batch {b.batch_id}: no free window")
                    continue
                taken.append(wins[i][0])
                items.append(Item(v, tgt.get("client_family", "?"), run_id, b, wins[i]))
                i += 1
    items.sort(key=lambda it: it.window[0])
    return items, skipped


def batch_outcomes(run_dir: Path) -> dict[str, tuple[int, int]]:
    """'run_id:batch' -> (probes that reached the chain in any slot, probes sent) for a finished run."""
    import csv
    with open(Path(run_dir) / "probes.csv") as f:
        probes = {r["signature"]: r for r in csv.DictReader(f)}
    out: dict[str, list[int]] = {}
    with open(Path(run_dir) / "landings.csv") as f:
        for r in csv.DictReader(f):
            p = probes[r["signature"]]
            c = out.setdefault(f"{p['run_id']}:{p['batch_id']}", [0, 0])
            c[0] += r["outcome"] != "dropped"
            c[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def limit_items(items: list[Item], only: str | None, max_batches: int | None) -> list[Item]:
    """Narrow a schedule for a small test: one validator (identity prefix) and/or the first N batches in time."""
    if only:
        items = [i for i in items if i.validator.startswith(only)]
    return items[:max_batches] if max_batches else items


class Orchestrator:
    def __init__(self, *, rpc, sender: Sender, sched: LeaderSchedule, predict: Callable[[int], float | None],
                 payers: list[Keypair], run_dir: Path, lead_ms: int = 300, prep_s: float = 25.0,
                 deadline: float | None = None, settle_s: float = 25.0, min_lead_s: float = 0.2, time_fn=time.time,
                 on_sent: Callable[[Item, list[dict]], None] | None = None):
        self.rpc, self.sender, self.sched, self.predict, self.payers = rpc, sender, sched, predict, payers
        self.run_dir, self.lead_ms, self.prep_s, self.deadline = Path(run_dir), lead_ms, prep_s, deadline
        self.settle_s, self.now, self.on_sent = settle_s, time_fn, on_sent
        self.min_lead_s = min_lead_s
        self.state_path = self.run_dir / "orchestrator_state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"started": []}
        self.windows_log: list[dict] = []
        self.stats = {"sent": 0, "skipped_late": 0, "skipped_done": 0, "stopped": None}

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    async def run_item(self, it: Item) -> None:
        if it.key in self.state["started"]:
            self.stats["skipped_done"] += 1
            return
        start = self.predict(it.window[0])
        if start is None:
            self.sender.log(event="item_skipped", key=it.key, reason="no slot clock")
            self.stats["skipped_late"] += 1
            return
        t0 = start - self.lead_ms / 1000
        wait = t0 - self.prep_s - self.now()
        if wait > 0:
            await asyncio.sleep(wait)
        if self.deadline and self.now() > self.deadline:
            self.stats["stopped"] = "deadline"
            raise StopAsyncIteration
        # re-predict with the freshest clock, then prepare against a fresh blockhash
        start = self.predict(it.window[0]) or start
        t0 = start - self.lead_ms / 1000
        if self.sched.slot_leader.get(it.window[0]) != it.validator:
            self.sender.log(event="item_skipped", key=it.key, reason="leader schedule changed")
            return
        bh = Hash.from_string(self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
        prep = prepare(it.batch, self.payers, bh)
        if t0 - self.now() < self.min_lead_s:  # not enough time left to fire at the right moment: do not send late
            self.sender.log(event="item_skipped", key=it.key, reason="late", behind_s=self.now() - t0)
            self.stats["skipped_late"] += 1
            return
        self.state["started"].append(it.key)  # at-most-once: recorded before any send
        self._save()
        try:
            rows = await self.sender.send_batch(prep, t0, leader=it.validator, window=it.window)
        except CapExceeded:
            self.stats["stopped"] = "cap"
            raise StopAsyncIteration
        self.windows_log.append({"run_id": it.run_id, "batch": it.batch.batch_id, "validator": it.validator,
                                 "slots": it.window, "predicted_ts": start})
        self.stats["sent"] += 1
        if self.on_sent:
            self.on_sent(it, rows)

    async def run(self, items: list[Item], observed: Callable[[int], float | None] = lambda s: None) -> dict:
        try:
            for it in items:
                try:
                    await self.run_item(it)
                except StopAsyncIteration:
                    break
        finally:
            for w in self.windows_log:
                w["observed_ts"] = observed(w["slots"][0])
            (self.run_dir / "leader_windows.json").write_text(json.dumps(self.windows_log, indent=1))
        return self.stats

    def finish(self, cache_dir: Path) -> dict:
        """After the last window: wait for finality, then collect, compact and map (looking up where
        probes outside the target windows landed)."""
        time.sleep(self.settle_s)
        if not self.windows_log:
            return {"collected": 0}
        from .collect import collect_run
        return collect_run(self.rpc, self.run_dir, cache_dir)
