"""Report generator (T22 / FR-12): REPORT.md as a pure function of the raw and analysis files.

No wall-clock time is used: the same inputs always give the same bytes. Every per-validator claim is
followed by an evidence table of signatures, slots and positions that `itb verify` can re-check.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from .config import ALPENGLOW_ACTIVATION

EXPLORER = "https://solscan.io/tx/"


def _run_date(run_dir: Path) -> str:
    n = run_dir.name  # YYYYMMDDTHHMMSSZ
    return f"{n[0:4]}-{n[4:6]}-{n[6:8]}"


def _read(p: Path) -> list[dict]:
    with open(p) as f:
        return list(csv.DictReader(f))


def _fmt(v, nd=2):
    return "n/a" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def _runs(data_dir: Path) -> tuple[list[Path], list[Path]]:
    allr = sorted(p for p in (data_dir / "raw").glob("2*") if (p / "landings.csv").exists())
    cut = ALPENGLOW_ACTIVATION
    return [p for p in allr if _run_date(p) >= cut], [p for p in allr if _run_date(p) < cut]


def build_report(data_dir: Path, report_dir: Path) -> str:
    data_dir, report_dir = Path(data_dir), Path(report_dir)
    runs, pre = _runs(data_dir)
    verdicts = {v["validator"]: v for v in json.loads((data_dir / "analysis/verdicts.json").read_text())} \
        if (data_dir / "analysis/verdicts.json").exists() else {}
    noise = json.loads((data_dir / "analysis/noise.json").read_text()) \
        if (data_dir / "analysis/noise.json").exists() else {}

    probes, lands, envs = [], {}, {}
    for rd in runs:
        pr = _read(rd / "probes.csv")
        ld = {r["signature"]: r for r in _read(rd / "landings.csv")}
        envs[rd.name] = json.loads((rd / "env.json").read_text()) if (rd / "env.json").exists() else {}
        for p in pr:
            probes.append({**p, "_run_dir": rd.name})
            lands[p["signature"]] = ld.get(p["signature"], {})
    fam: dict[str, tuple] = {}
    for e in envs.values():
        for t in e.get("targets_snapshot", []):
            fam[t["identity"]] = (t["client_family"], t["version"])
    by_val: dict[str, list[dict]] = defaultdict(list)
    for p in probes:
        by_val[p["leader"]].append(p)

    L: list[str] = []
    dates = sorted({_run_date(r) for r in runs})
    L += ["# Inside the Block: report", "",
          f"Measurements: {dates[0]} to {dates[-1]} (all after Alpenglow activation, {ALPENGLOW_ACTIVATION})."
          if dates else "No post-Alpenglow measurement data yet: this report is empty by design.",
          "", "Regenerate with `itb analyze && itb report`. Every claim below is followed by the signatures "
          "and slots it rests on; `docker compose run verify` re-checks them against the chain.", ""]

    # --- summary
    L += ["## Summary", "", "| Validator | Client (at measurement) | Runs | Probes | Landed in window | Verdict | Confidence |",
          "|---|---|---|---|---|---|---|"]
    for v, ps in sorted(by_val.items()):
        out = Counter(lands[p["signature"]].get("outcome", "dropped") for p in ps)
        ver = verdicts.get(v, {})
        f = fam.get(v, ("?", "?"))
        L.append(f"| `{v[:8]}…` | {f[0]} {f[1]} | {len({p['run_id'] for p in ps})} | {len(ps)} | "
                 f"{out.get('landed', 0)} ({100 * out.get('landed', 0) // max(1, len(ps))}%) | "
                 f"**{ver.get('verdict', 'not analysed')}** | {_fmt(ver.get('confidence'))} |")
    L.append("")
    if (report_dir / "figures/summary_effects.png").exists():
        L += ["![effect summary](figures/summary_effects.png)", ""]

    # --- measurement log
    L += ["## Measurement log", "", "| Run | Date (UTC) | Provider | Clock offset (s) | Live | Probes |",
          "|---|---|---|---|---|---|"]
    for rd in runs:
        e = envs[rd.name]
        n = sum(1 for p in probes if p["_run_dir"] == rd.name)
        L.append(f"| `{rd.name}` | {e.get('date_utc', _run_date(rd))} | {e.get('rpc_provider_host', 'n/a')} | "
                 f"{_fmt(e.get('clock_offset_s'), 3)} | {e.get('live', 'n/a')} | {n} |")
    L.append("")

    # --- per validator
    L += ["## Findings per validator", ""]
    for v, ps in sorted(by_val.items()):
        ver = verdicts.get(v, {})
        f = fam.get(v, ("?", "?"))
        L += [f"### `{v}`", "", f"Client at measurement: **{f[0]}** {f[1]}. Verdict: **{ver.get('verdict', 'not analysed')}** "
              f"(confidence {_fmt(ver.get('confidence'))}; {ver.get('n_landed_probes', 0)} probes used).", ""]
        if ver.get("tests"):
            L += ["| Hypothesis | Effect | p | p (Holm) | Runs agreeing | Passed |", "|---|---|---|---|---|---|"]
            for h, t in ver["tests"].items():
                L.append(f"| {h} | {_fmt(t['effect'])} | {_fmt(t['p'], 4)} | {_fmt(t.get('p_holm'), 4)} | "
                         f"{_fmt(t['run_agreement'])} | {'yes' if t['passed'] else 'no'} |")
            L.append("")
        nv = noise.get("validators", {}).get(v, {})
        if nv:
            L += ["Noise vs rule:", ""]
            for h, s in nv.items():
                st = s["stability"]
                line = (f"- {h}: effect {_fmt(s['effect'])} = {_fmt(s['effect_over_null_sd'], 1)}x the shuffle-noise SD "
                        f"({_fmt(s['null_sd'], 3)}); between-run SD {_fmt(st.get('sd'), 3)} over {st['n_runs']} runs")
                if s.get("path"):
                    pe = s["path"]
                    line += f"; path effect {pe['paths'][0]} vs {pe['paths'][1]}: diff {_fmt(pe['difference'])}, p {_fmt(pe['p'], 4)}"
                L.append(line)
            L.append("")
        ctx = noise.get("competition", {}).get(v)
        if ctx:
            L += [f"Competing traffic ({ctx['n_blocks']} blocks): context tau {_fmt(ctx['context_tau_mean'])} "
                  f"(sd {_fmt(ctx['context_tau_sd'])}). Supplementary only.", ""]
        fig = report_dir / f"figures/{v[:8]}_position.png"
        if fig.exists():
            L += [f"![positions {v[:8]}](figures/{fig.name})", ""]
        L += ["<details><summary>Evidence: every probe (signature, slot, position)</summary>", "",
              "| Probe | Signature | Slot | Tx index | Rank in batch | Outcome |", "|---|---|---|---|---|---|"]
        for p in sorted(ps, key=lambda p: (p["_run_dir"], p["probe_id"])):
            l = lands[p["signature"]]
            sig = p["signature"]
            L.append(f"| `{p['probe_id']}` | [`{sig[:12]}…`]({EXPLORER}{sig}) | {l.get('slot') or ''} | "
                     f"{l.get('tx_index_in_block') or ''} | {l.get('rank_in_batch') or ''} | {l.get('outcome', 'dropped')} |")
        L += ["", "</details>", ""]

    # --- what went wrong
    L += ["## What went wrong", ""]
    manual = report_dir / "what_went_wrong_manual.md"
    if manual.exists():
        L += [ln for ln in manual.read_text().splitlines() if not ln.startswith("<!--")] + [""]
    auto = []
    for rd in pre:
        auto.append(f"- Run `{rd.name}` is dated before Alpenglow ({ALPENGLOW_ACTIVATION}) and is excluded.")
    tot = Counter(lands[p["signature"]].get("outcome", "dropped") for p in probes)
    for k, why in (("dropped", "never landed in any collected block"),
                   ("landed_other_leader", "landed in another leader's slot (excluded from ordering tests)"),
                   ("landed_outside_window", "landed outside the 4-slot window (excluded)")):
        if tot.get(k):
            auto.append(f"- {tot[k]} of {len(probes)} probes {why}.")
    failed = sum(1 for p in probes if lands[p["signature"]].get("status") == "failed")
    if failed:
        auto.append(f"- {failed} probes landed but failed on chain; check `err` in `landings.csv`.")
    for v in by_val:
        if verdicts.get(v, {}).get("verdict") == "insufficient data":
            auto.append(f"- `{v[:8]}…`: insufficient data (fewer than 16 probes landed in window).")
    for rd in runs:
        off = envs[rd.name].get("clock_offset_s")
        if off is not None and abs(off) > 0.1:
            auto.append(f"- Run `{rd.name}`: local clock was {off:.3f} s off NTP.")
        for ident in envs[rd.name].get("client_version_changes", []):
            auto.append(f"- Run `{rd.name}`: `{ident[:8]}…` changed client/version between target selection and measurement.")
        if not envs[rd.name]:
            auto.append(f"- Run `{rd.name}`: no env.json recorded.")
        log = rd / "send_log.jsonl"
        if log.exists():
            refused = sum(1 for ln in log.read_text().splitlines() if '"event": "refused"' in ln)
            if refused:
                auto.append(f"- Run `{rd.name}`: {refused} batch(es) refused by the spend cap.")
    none = "- No data-derived problems detected." if runs else "- No measurements yet, so nothing to check."
    L += (auto or [none]) + [""]

    L += ["## Limits", "", "See README, *What this design does NOT separate*: arrival-time jitter, provider "
          "forwarding path, Jito block-engine vs banking stage, entry boundaries, small samples.", ""]
    return "\n".join(L)


def write_report(data_dir: Path, report_dir: Path) -> Path:
    out = Path(report_dir) / "REPORT.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(data_dir, report_dir))
    return out
