"""`itb send --live`: the capped, approved, resumable mainnet run (T17)."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from .approval import APPROVAL_FILE, ApprovalError, check_approval
from .config import ConfigError, Settings, load_keypair
from .env import write_env
from .fund import RENT_EXEMPT_ZERO_DATA, fund_payers, rent_min, sweep
from .probes import fee_lamports
from .ledger import SpendLedger
from .matrix import estimate_cost
from .orchestrator import Orchestrator, build_schedule, limit_items
from .rpc import Rpc
from .schedule import LeaderSchedule, SlotWatcher
from .sender import RpcTransport, Sender, payer_pool

N_PAYERS = 8
FUND_EACH = RENT_EXEMPT_ZERO_DATA  # rent floor only; fee budgets are added per payer from the schedule
MARGIN = 50_000


def wallet_need(cap_lamports: int) -> int:
    """What the wallet must hold: the fee cap (it is deposited in the payers, who pay the fees) plus the
    recoverable rent floor of every payer plus a margin. Everything not spent is swept back."""
    return cap_lamports + N_PAYERS * FUND_EACH + MARGIN


def payer_budgets(items, n_payers: int = N_PAYERS) -> list[int]:
    """Worst-case fees each payer will pay over the whole schedule. A payer's deposit must be at least
    rent floor + this, because a fee payer can never spend below the rent floor (found the hard way: with a
    50 000 fee budget every payer ran dry after ~57 transactions and ~75 % of the first study never landed)."""
    budget = [0] * n_payers
    for it in items:
        for pr in it.batch.probes:
            budget[pr.payer_index] += fee_lamports(pr.cu_price, pr.cu_limit)
    return budget


def run_live(s: Settings, resume: str | None = None, only: str | None = None,
             max_batches: int | None = None, redo: str | None = None) -> int:
    try:
        check_approval(APPROVAL_FILE, s.cap_lamports, s.targets)
        master = load_keypair(s.keypair_path or "")
    except (ApprovalError, ConfigError) as e:
        print(f"refusing to go live: {e}", file=sys.stderr)
        return 2
    rpc = Rpc(s.rpc_url, s.rpc_url_fallback)
    bal = rpc.call("getBalance", [str(master.pubkey())])["value"]
    if bal < wallet_need(s.cap_lamports):
        print(f"wallet has {bal} lamports, needs {wallet_need(s.cap_lamports)} "
              f"(fee cap {s.cap_lamports} + rent floor of {N_PAYERS} payers {N_PAYERS * FUND_EACH}; unspent is returned)", file=sys.stderr)
        return 2
    seed = s.experiment.get("seed", 0)
    runs = s.experiment.get("runs_per_validator", 3)
    est = estimate_cost(runs, len(s.targets), seed)
    if est["total_lamports"] > s.cap_lamports:
        print(f"worst-case plan {est['total_lamports']} lamports exceeds cap {s.cap_lamports}", file=sys.stderr)
        return 2

    stamp = resume or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = s.data_dir / "raw" / stamp
    ids_stamp = redo or stamp  # redo re-sends a finished run's missing batches under that run's own run ids
    done: set[str] = set()
    if redo:
        from .orchestrator import batch_outcomes
        out_old = batch_outcomes(s.data_dir / "raw" / redo)
        # a batch is only done if ALL its probes reached the chain: a partial batch lost its highest-index
        # probes (those whose payer ran dry first), which would bias the ordering tests
        done = {k for k, (n, tot) in out_old.items() if n == tot}
        missing = sorted(k for k, (n, tot) in out_old.items() if n < tot)
        (s.data_dir / "raw" / redo / "exclusions.json").write_text(json.dumps({
            "reason": "payer fee deposits were too small (rent floor not accounted for): these batches were fully or "
                      "partly rejected by the leader for lack of fee funds, not by validator behaviour; re-sent in run " + stamp,
            "batches": missing}, indent=1))
        print(f"redo of {redo}: {len(done)} batches complete on chain, {len(missing)} to re-send")
    from .snapshot import snapshot_targets
    snap = snapshot_targets(rpc, s.targets, s.validators_app_token)
    for t in snap:
        if t["changed"]:
            print(f"NOTE: {t['identity'][:8]} changed since selection: {t['selected_family']} {t['selected_version']} -> {t['client_family']} {t['version']}")
    write_env(run_dir, s.rpc_url, s.targets, live=True, snapshot=snap)
    ledger = SpendLedger(s.data_dir / "ledger" / "spend_ledger.json", s.cap_lamports)
    sched = LeaderSchedule.fetch(rpc)
    payers = payer_pool(master, N_PAYERS)
    transport = RpcTransport(s.rpc_url)
    sender = Sender(transport, stamp, run_dir, live=True, ledger=ledger)

    async def main():
        w = SlotWatcher(s.rpc_url)
        task = asyncio.create_task(w.run())
        try:
            for _ in range(180):  # a stable clock fit needs >= 100 slots over >= 30 s
                if w.ready():
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("slot clock never became ready")
            # everything below is sized from the MEASURED slot time (0.214 s on 2026-10-28, not 0.4 s)
            slot_s = w.slot_seconds()
            print(f"measured slot time: {slot_s * 1000:.0f} ms")
            items, skipped = build_schedule(
                s.targets, sched, rpc.call("getSlot"), stamp=ids_stamp, seed=seed, runs=runs,
                min_ahead_slots=round(s.experiment.get("min_ahead_s", 150) / slot_s),
                min_gap_s=s.experiment.get("min_gap_s", 15),
                horizon_slots=round(s.experiment.get("horizon_s", 43_200) / slot_s), slot_s=slot_s)
            if redo:
                items = [i for i in items if i.key not in done]
            items = limit_items(items, only, max_batches)
            (run_dir / "schedule.json").write_text(json.dumps(
                [{"key": i.key, "validator": i.validator, "family": i.family, "window": i.window}
                 for i in items], indent=1))
            print(f"{len(items)} batches over {len(s.targets)} validators; skipped: {len(skipped)}")
            for sk in skipped:
                print("  skipped", sk)
            if not items:
                return None, {"sent": 0, "stopped": "empty schedule"}
            floor = rent_min(rpc)
            budget = payer_budgets(items)
            deposits = [floor + b + MARGIN // N_PAYERS for b in budget]
            print(f"funding {N_PAYERS} payers: rent floor {floor} + fee budgets {budget}")
            fund_payers(rpc, master, payers, deposits)
            for _ in range(60):  # do not start before every deposit is visible
                if all(rpc.call("getBalance", [str(p.pubkey())])["value"] >= d for p, d in zip(payers, deposits)):
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("payer deposits did not arrive")
            await transport.warm(16)
            orch = Orchestrator(rpc=rpc, sender=sender, sched=sched, predict=w.predict, payers=payers,
                                run_dir=run_dir, lead_ms=s.experiment.get("send_lead_ms", -50))
            stats = await orch.run(items, observed=w.observed)
            return orch, stats
        finally:
            task.cancel()
            await sender.close()

    orch = None
    try:
        orch, stats = asyncio.run(main())
        print("run finished:", stats)
    finally:
        try:
            print("swept back", sweep(rpc, master, payers), "lamports of payer deposits")
        except Exception as e:  # never hide a failed run behind a failed sweep
            print(f"WARNING: sweep failed ({type(e).__name__}); payers can be re-derived from the key", file=sys.stderr)
    if orch:
        print("landings:", orch.finish(s.data_dir / "cache" / stamp))
        print("next: itb analyze && itb verify && itb report")
    return 0
