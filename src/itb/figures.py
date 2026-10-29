"""Charts (T20): probe position vs fee and vs send order per validator, plus an effect summary.

Deterministic: fixed style, no timestamps in the PNG, runs coloured in a fixed order. Colours are the
first three slots of the validated categorical palette (they pass the all-pairs checks in light mode,
so scatter is legal up to three runs; more runs fall back to one neutral colour). Light mode only:
these are static PNGs for the report.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .infer import Batch  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2dd"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # blue, orange, aqua
NEUTRAL = "#8a8984"
THRESH = 0.6  # decision threshold from docs/DESIGN.md

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.axisbelow": True, "svg.hashsalt": "itb",
})


def _save(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, metadata={"Software": None}, bbox_inches="tight")
    plt.close(fig)


def _colors(batches: list[Batch]) -> dict[str, str]:
    runs = sorted({b.run_id for b in batches})
    return {r: (SERIES[i] if len(runs) <= len(SERIES) else NEUTRAL) for i, r in enumerate(runs)}


def _norm(pos: list[int]) -> list[float]:
    n = len(pos)
    return [p / (n - 1) if n > 1 else 0.0 for p in pos]


def _panel(ax, batches, types, xkey, xlabel, colors, symlog=False, invert=False):
    for b in batches:
        if b.batch_type not in types:
            continue
        x = [-v for v in b.x[xkey]] if invert else b.x[xkey]
        ax.scatter(x, _norm(b.pos), s=44, color=colors[b.run_id], edgecolor=SURFACE, linewidth=1.2,
                   zorder=3)
    if symlog:
        ax.set_xscale("symlog", linthresh=500)
    ax.set_xlabel(xlabel)
    ax.set_ylim(1.06, -0.06)  # first position at the top
    ax.set_yticks([0, 0.5, 1])
    ax.set_yticklabels(["first", "middle", "last"])


def validator_figure(label: str, batches: list[Batch], verdict: str, out: Path) -> Path:
    colors = _colors(batches)
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.9), sharey=True)
    _panel(axes[0], batches, ("fee_ladder", "reverse_order"), "cu_price",
           "Priority fee (micro-lamports per CU)", colors, symlog=True)
    _panel(axes[1], batches, ("fee_ladder", "control"), "send_order",
           "Send order (0 = sent first)", colors)
    axes[0].set_ylabel("Position among our probes in the block")
    axes[0].set_title("Does a higher fee land earlier?", loc="left", fontsize=10, color=INK)
    axes[1].set_title("Does sending first land earlier?", loc="left", fontsize=10, color=INK)
    runs = sorted(colors)
    if len(runs) > 1:  # legend outside the plot area: identity is never colour alone
        hs = [axes[1].scatter([], [], s=44, color=colors[r], label=f"run {i + 1}") for i, r in enumerate(runs)]
        fig.legend(handles=hs, frameon=False, loc="upper right", ncol=len(hs), fontsize=9,
                   labelcolor=INK2, scatterpoints=1, bbox_to_anchor=(0.99, 1.02))
    fig.suptitle(f"{label[:8]}…  verdict: {verdict}", x=0.01, y=1.02, ha="left", fontsize=11, color=INK)
    path = Path(out) / f"{label[:8]}_position.png"
    _save(fig, path)
    return path


def summary_figure(verdicts: list[dict], out: Path) -> Path | None:
    rows = [v for v in verdicts if v.get("tests")]
    if not rows:
        return None
    hyps = ["H1", "H2", "H3"]
    names = {"H1": "fee per CU", "H2": "reward per CU", "H3": "arrival order"}
    fig, axes = plt.subplots(1, 3, figsize=(10, 0.55 * len(rows) + 1.6), sharey=True)
    for ax, h in zip(axes, hyps):
        ys = list(range(len(rows)))
        eff = [v["tests"][h]["effect"] or 0.0 for v in rows]
        ax.axvline(THRESH, color=INK2, linewidth=1, linestyle=(0, (4, 3)), zorder=1)
        ax.axvline(0, color=GRID, linewidth=1.2, zorder=1)
        ax.scatter(eff, ys, s=60, color=SERIES[0], edgecolor=SURFACE, linewidth=1.2, zorder=3)
        ax.set_xlim(-1.05, 1.05)
        ax.set_title(names[h], loc="left", fontsize=10, color=INK)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(range(len(rows)))
    axes[0].set_yticklabels([f"{v['validator'][:8]}  {v['verdict']}" for v in rows], fontsize=9)
    axes[0].invert_yaxis()
    th = axes[2].plot([], [], color=INK2, linewidth=1, linestyle=(0, (4, 3)), label="decision threshold (τ = 0.6)")
    fig.legend(handles=th, frameon=False, loc="upper right", fontsize=9, labelcolor=INK2,
               bbox_to_anchor=(0.99, 1.0))
    fig.supxlabel("Kendall τ between the rule's score and landing order (1 = rule fully explains order)",
                  fontsize=9, color=INK2, y=-0.04)
    fig.suptitle("How well each rule explains the order of our probes", x=0.01, y=1.03, ha="left", fontsize=11)
    path = Path(out) / "summary_effects.png"
    _save(fig, path)
    return path


def make_figures(batches_by_val: dict[str, list[Batch]], verdicts: list[dict], out: Path) -> list[Path]:
    by = {v["validator"]: v["verdict"] for v in verdicts}
    paths = [validator_figure(v, bs, by.get(v, "n/a"), out) for v, bs in sorted(batches_by_val.items())]
    s = summary_figure(verdicts, out)
    return paths + ([s] if s else [])
