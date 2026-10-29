"""`itb <stage>`: discover | plan | send | collect | analyze | report | verify.

Default is dry-run. Live sending needs BOTH --live and --cap-sol.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import ConfigError, load_settings
from .matrix import build_window_plan, estimate_cost

STAGES = ["approve", "select", "track", "discover", "plan", "send", "collect", "analyze", "report", "verify"]


def _run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def cmd_discover(a, s):
    from .discover import discover
    from .rpc import Rpc
    rows = discover(Rpc(s.rpc_url, s.rpc_url_fallback), s.data_dir / "raw" / "discovery",
                    s.validators_app_token)
    fams = sorted({r["client_family"] for r in rows})
    print(json.dumps({"validators": len(rows), "families": fams}))


def cmd_select(a, s):
    from .rpc import Rpc
    from .schedule import LeaderSchedule
    from .select import select_targets, write_targets
    rpc = Rpc(s.rpc_url, s.rpc_url_fallback)
    rows = json.loads((s.data_dir / "raw" / "discovery" / "validators.json").read_text())
    sched = LeaderSchedule.fetch(rpc)
    chosen, log = select_targets(rows, sched, rpc.call("getSlot"), n=a.n)
    print("\n".join(log))
    write_targets(s.targets_file, chosen)
    print(f"wrote {len(chosen)} targets to {s.targets_file}")


def cmd_track(a, s):
    import asyncio
    from .schedule import track
    r = asyncio.run(track(s.rpc_url, a.seconds))
    out = s.data_dir / "raw" / "tracking"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"track_{_run_id()}.json").write_text(json.dumps(r, indent=1))
    print(json.dumps({k: v for k, v in r.items() if k != "samples"}))
    return 0 if (r["hit_rate"] or 0) >= 0.95 else 1


def cmd_send(a, s):
    """Dry-run: plan every batch on real upcoming windows, replay the window procedure to see whether we
    would have made it into the windows, and rehearse a dispatch. Nothing is sent, zero lamports."""
    import asyncio
    import time as _t

    from solders.hash import Hash
    from .dryrun import format_report, run_window_dryrun
    from .env import write_env
    from .rpc import Rpc
    from .schedule import LeaderSchedule
    from .sender import DryTransport, Sender, payer_pool, prepare
    from .snapshot import snapshot_targets
    if s.live:
        from .live import run_live
        return run_live(s, a.resume, a.only, a.max_batches)
    if not s.targets:
        print("no targets: run `itb select` first", file=sys.stderr)
        return 2
    rpc = Rpc(s.rpc_url, s.rpc_url_fallback)
    run_id = _run_id()
    out = s.data_dir / "raw" / run_id
    out.mkdir(parents=True, exist_ok=True)
    snap = snapshot_targets(rpc, s.targets, s.validators_app_token)
    write_env(out, s.rpc_url, s.targets, live=False, snapshot=snap)
    for t in snap:
        if t["changed"]:
            print(f"NOTE: {t['identity'][:8]} changed since selection: {t['selected_family']} {t['selected_version']}"
                  f" -> {t['client_family']} {t['version']}")
    lead_ms = s.experiment.get("send_lead_ms", -50)
    seed = s.experiment.get("seed", 0)
    sched = LeaderSchedule.fetch(rpc)
    target_ids = {t["identity"] for t in s.targets}

    rep, watcher = asyncio.run(run_window_dryrun(
        s.rpc_url, a.watch, lead_ms,
        targets={sl for sl, who in sched.slot_leader.items() if who in target_ids and sl % 4 == 0}))
    (out / "window_dryrun.json").write_text(json.dumps(rep, indent=1))
    print(format_report(rep))

    slot_s = watcher.slot_seconds()
    cur = rpc.call("getSlot")
    plan = []
    for tgt in s.targets:
        batches = build_window_plan(f"{run_id}-{tgt['identity'][:6]}", seed, slot_s)
        wins = sched.windows(tgt["identity"], cur + round(150 / slot_s), limit=len(batches))
        for b, w in zip(batches, wins):
            start = watcher.predict(w[0])
            plan.append({"validator": tgt["identity"], "client_family": tgt["client_family"],
                         "batch": b.batch_id, "type": b.batch_type, "window": w, "n_tx": len(b.probes),
                         "predicted_window_start": start,
                         "send_at": start - lead_ms / 1000, "in_s": round(start - _t.time()),
                         "worst_case_lamports": b.worst_case_lamports})
    (out / "send_plan.json").write_text(json.dumps(plan, indent=1))
    print(f"planned {len(plan)} batches over {len(s.targets)} validators; spend 0 lamports -> {out}/send_plan.json")

    # rehearsal: build + dispatch one batch with a real blockhash, null transport, near-future t0
    bh = Hash.from_string(rpc.call("getLatestBlockhash")["value"]["blockhash"])
    b = build_window_plan("rehearsal", seed, slot_s)[0]
    t_prep = _t.perf_counter()
    prep = prepare(b, payer_pool(None, len(b.probes)), bh)
    prep_ms = (_t.perf_counter() - t_prep) * 1000
    sender = Sender(DryTransport(), "rehearsal-" + run_id, out, live=False)
    rows = asyncio.run(sender.send_batch(prep, _t.time() + 1.0))
    sts = [r["send_ts_us"] for r in rows]
    rep = {"batch_txs": len(prep), "prepare_ms": round(prep_ms, 2),
           "dispatch_spread_us": max(sts) - min(sts), "lamports_spent": 0, "network_sends": 0}
    (out / "rehearsal.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep))


def cmd_collect(a, s):
    from .collect import collect_run, collect_slots, slots_for_run
    from .rpc import Rpc
    raw = s.data_dir / "raw"
    if a.slots:
        lo, hi = (int(x) for x in a.slots.split(":"))
        run_dir = raw / (a.run or "adhoc")
        run_dir.mkdir(parents=True, exist_ok=True)
        collect_slots(Rpc(s.rpc_url, s.rpc_url_fallback), list(range(lo, hi + 1)), run_dir,
                      s.data_dir / "cache" / run_dir.name)
        print(f"stored {hi - lo + 1} full blocks in {s.data_dir / 'cache' / run_dir.name}/blocks (git-ignored)")
        return
    runs = sorted(p for p in raw.glob("2*") if (p / "probes.csv").exists())
    run_dir = raw / a.run if a.run else (runs[-1] if runs else None)
    if run_dir is None:
        print("no run with probes.csv yet (nothing was sent); use --slots LO:HI to fetch blocks", file=sys.stderr)
        return 1
    if not slots_for_run(run_dir / "probes.csv"):
        print(f"{run_dir.name}: probes.csv has no target windows (dry-run rehearsal); nothing to collect")
        return 0
    from collections import Counter
    print(json.dumps(collect_run(Rpc(s.rpc_url, s.rpc_url_fallback), run_dir, s.data_dir / "cache" / run_dir.name)))


def cmd_analyze(a, s):
    from .infer import analyze
    raw = s.data_dir / "raw"
    runs = sorted(p for p in raw.glob("2*") if (p / "landings.csv").exists())
    if not runs:
        print("no run with landings.csv yet: nothing to analyse (dry-run). Run collect after a live run.")
        return 0
    seed = s.experiment.get("seed", 0)
    res = analyze(runs, s.data_dir / "analysis" / "verdicts.json", seed)
    from .infer import load_batches
    from .noise import noise_report
    from .figures import make_figures
    batches = load_batches(runs)
    figs = make_figures(batches, res, Path("report/figures"))
    print(f"wrote {len(figs)} figures to report/figures")
    (s.data_dir / "analysis" / "noise.json").write_text(
        json.dumps(noise_report(load_batches(runs), runs, seed), indent=1))
    for r in res:
        print(f"{r['validator'][:8]} landed={r['n_landed_probes']:3} -> {r['verdict']} (conf {r['confidence']})")


def cmd_verify(a, s):
    from .rpc import Rpc
    from .verify import verify_run, verify_verdicts
    raw = s.data_dir / "raw"
    runs = sorted(p for p in raw.glob("2*") if (p / "landings.csv").exists())
    if a.run:
        runs = [raw / a.run]
    if not runs:
        print("no run with landings.csv to verify", file=sys.stderr)
        return 1
    rpc = Rpc(s.rpc_url, s.rpc_url_fallback)
    ok = True
    for rd in runs:
        rep = verify_run(rpc, rd)
        ok &= rep["ok"]
        print(f"{rd.name}: checked {rep['checked']} -> {'OK' if rep['ok'] else str(len(rep['problems'])) + ' PROBLEMS'}")
        for p in rep["problems"][:10]:
            print("  ", p["probe_id"], p["problem"])
    vp = s.data_dir / "analysis" / "verdicts.json"
    if vp.exists():
        r = verify_verdicts(runs, vp, s.experiment.get("seed", 0))
        ok &= r["verdicts_reproduced"]
        print("verdicts re-derived identically:", r["verdicts_reproduced"])
    print("VERIFY", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def cmd_report(a, s):
    from .report import write_report
    out = write_report(s.data_dir, Path("report"))
    print(f"wrote {out}")


def cmd_approve(a):
    """The T16 human gate: a person reviews cap, wallet need and targets, then types APPROVE."""
    from .approval import APPROVAL_FILE, write_approval
    from .config import LAMPORTS_PER_SOL
    from .live import FUND_EACH, N_PAYERS, wallet_need
    s = load_settings()
    cap = int(round((a.cap_sol or 0) * LAMPORTS_PER_SOL))
    if cap <= 0 or not s.targets or not a.by:
        print("usage: itb approve --cap-sol 0.05 --by 'Your Name'  (needs targets: run `itb select`)", file=sys.stderr)
        return 2
    est = estimate_cost(s.experiment.get("runs_per_validator", 3), len(s.targets), s.experiment.get("seed", 0))
    print(f"Targets ({len(s.targets)}):")
    for t in s.targets:
        print(f"  {t['identity'][:8]}  {t['client_family']:16} {t['version']}")
    print(f"Fee cap: {cap} lamports ({cap / LAMPORTS_PER_SOL} SOL); worst-case plan {est['total_lamports']} lamports, {est['txs']} txs")
    print(f"Payer deposits (returned): {N_PAYERS * FUND_EACH} lamports. Wallet must hold >= {wallet_need(cap)} lamports.")
    if not sys.stdin.isatty():
        print("not an interactive terminal: a person must run this", file=sys.stderr)
        return 2
    if input("Type APPROVE to sign off live sending with exactly this cap and target list: ").strip() != "APPROVE":
        print("not approved")
        return 1
    write_approval(APPROVAL_FILE, a.by, cap, s.targets, N_PAYERS * FUND_EACH)
    print(f"wrote {APPROVAL_FILE}")


def cmd_plan(a, s):
    from .orchestrator import build_schedule
    from .rpc import Rpc
    from .schedule import LeaderSchedule
    n_val = max(len(s.targets), 6)
    runs = s.experiment.get("runs_per_validator", 3)
    est = estimate_cost(runs, n_val, s.experiment.get("seed", 0))
    print(json.dumps(est))
    cap_hint = 50_000_000
    if est["total_lamports"] > cap_hint:
        print("WARNING: worst-case plan exceeds 0.05 SOL; shrink the matrix", file=sys.stderr)
        return 1
    for b in build_window_plan("preview", s.experiment.get("seed", 0)):
        print(f"{b.batch_id:5} {b.batch_type:14} n={len(b.probes):2} tests={','.join(b.tests)} "
              f"worst_case={b.worst_case_lamports} lamports")
    if s.targets:  # can the whole study be scheduled? (needs the network for the leader schedule)
        try:
            rpc = Rpc(s.rpc_url, s.rpc_url_fallback)
            slot_s = s.experiment.get("slot_s", 0.214)
            items, skipped = build_schedule(
                s.targets, LeaderSchedule.fetch(rpc), rpc.call("getSlot"), stamp="plan",
                seed=s.experiment.get("seed", 0), runs=runs, min_ahead_slots=round(150 / slot_s),
                min_gap_s=s.experiment.get("min_gap_s", 15),
                horizon_slots=round(s.experiment.get("horizon_s", 43_200) / slot_s), slot_s=slot_s)
            span = (items[-1].window[0] - items[0].window[0]) * slot_s / 3600 if items else 0
            print(f"schedule: {len(items)} batches over {len(s.targets)} validators in {span:.1f} h, "
                  f"{len(skipped)} could not be placed; worst-case spend of these batches "
                  f"{sum(i.batch.worst_case_lamports for i in items)} lamports")
        except Exception as e:  # offline: the cost estimate above still stands
            print(f"(schedule feasibility skipped: {type(e).__name__})")


def cmd_todo(name):
    def f(a, s):
        mode = "LIVE" if s.live else "dry-run"
        print(f"[{name}] ({mode}) not implemented yet; see docs/ task list")
    return f


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="itb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="stage")
    for st in STAGES:
        sp = sub.add_parser(st, help=f"{st} stage")
        sp.add_argument("--live", action="store_true", help="really send txs (needs --cap-sol)")
        if st == "select":
            sp.add_argument("--n", type=int, default=6, help="number of targets (more than 6 = backups)")
        if st == "approve":
            sp.add_argument("--by", help="name of the person signing off")
        if st == "send":
            sp.add_argument("--watch", type=float, default=60,
                            help="dry-run: seconds of real windows to replay for the would-we-hit report (0 = skip)")
            sp.add_argument("--resume", help="run stamp to resume (batches already started are never resent)")
            sp.add_argument("--only", help="live: only the validator whose identity starts with this")
            sp.add_argument("--max-batches", type=int, help="live: only the first N scheduled batches")
        if st == "verify":
            sp.add_argument("--run")
        if st == "collect":
            sp.add_argument("--run")
            sp.add_argument("--slots", help="LO:HI absolute slot range, fetch blocks only")
        if st == "track":
            sp.add_argument("--seconds", type=float, default=120)
        sp.add_argument("--cap-sol", type=float, help="hard spend cap in SOL (live only)")
    a = p.parse_args(argv)
    if not a.stage:
        p.print_help()
        return 0
    if a.stage == "approve":
        return cmd_approve(a) or 0
    try:
        s = load_settings(live=a.live, cap_sol=a.cap_sol)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    handler = {"discover": cmd_discover, "plan": cmd_plan, "send": cmd_send, "collect": cmd_collect, "analyze": cmd_analyze, "verify": cmd_verify, "report": cmd_report, "select": cmd_select, "track": cmd_track}.get(a.stage, cmd_todo(a.stage))
    return handler(a, s) or 0


if __name__ == "__main__":
    sys.exit(main())
