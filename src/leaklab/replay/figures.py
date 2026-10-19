"""The two write-up figures, drawn from the exported bundles and nothing else.

  1. timeline_figure   three lanes (SharePoint, Azure AI Search, Revocation layer): one tick per read
                       of the flipped pair(s); green = served, red = denied, grey = no answer (error
                       or not indexed, never red); a small red marker under a served tick that the
                       oracle says was a leak; a thin row for the control pair; a vertical line at
                       t=0, "change confirmed"; the first denial of each lane as a dotted line.
                       The time axis is symlog (linear within +/- 30 s, logarithmic beyond: the
                       three lags differ by two orders of magnitude) or linear.
  2. first_deny_figure every replicate's first_deny_s as its own dot, per scenario and system, on a
                       log scale; DID_NOT_CONVERGE is drawn at the poll cap with a distinct marker.

No number on a figure is typed in: positions come from the bundle events and summaries, and the
captions are built from the bundles (n per cell, config labels, run and generation).
"""

import json
import textwrap
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

LANES = ("setup0", "setup_a_basic", "setup_b")
COLORS = {"served": "#2e9e5b", "denied": "#d6453d", "unscored": "#9aa0a6"}
SYSTEM_COLORS = {"setup0": "#4c78a8", "setup_a_basic": "#e8a33d", "setup_b": "#2e9e5b"}
TICKS_S = [1, 10, 60, 600, 3600]
TICK_LABELS = ["1 s", "10 s", "1 min", "10 min", "1 h"]


def load_bundles(folder: Path) -> list[dict[str, Any]]:
    """The scenario_run bundles of an export folder (agent_trial bundles are not drawn here)."""
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    loaded = [
        json.loads((folder / e["bundle"]).read_text(encoding="utf-8")) for e in index["bundles"]
    ]
    return [b for b in loaded if b.get("kind", "scenario_run") == "scenario_run"]


def representative_name(folder: Path, scenario: str) -> str | None:
    """File stem of the replicate the export ranked closest to the per-system medians, if it ranked."""
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    for e in index["bundles"]:
        if e.get("scenario") == scenario and e.get("representative"):
            return str(e["bundle"]).removesuffix(".json")
    return None


def bundle_name(b: dict[str, Any]) -> str:
    return f"{b['run']['run_id']}__{b['scenario']['id']}__r{b['scenario']['replicate']}"


def summary_of(b: dict[str, Any], system: str) -> dict[str, Any] | None:
    return next((s for s in b["summary"] if s["system"] == system), None)


def best_demo_bundle(
    bundles: Sequence[dict[str, Any]], scenario: str = "S1-entra"
) -> dict[str, Any] | None:
    """The bundle of `scenario` with the longest SharePoint lag among those where Setup B is clean:
    converged, a first denial, no leak exposure, no lockout, no read error (ties: faster Setup B)."""
    best: tuple[float, float, dict[str, Any]] | None = None
    for b in bundles:
        if b["scenario"]["id"] != scenario:
            continue
        sp, rl = summary_of(b, "setup0"), summary_of(b, "setup_b")
        if not sp or not rl or sp["first_deny_s"] is None or rl["first_deny_s"] is None:
            continue
        if (
            rl["status"] != "CONVERGED"
            or rl["leak_exposure_s"] != 0
            or rl["lockouts"] != 0
            or rl["n_errors"] != 0
        ):
            continue
        key = (float(sp["first_deny_s"]), -float(rl["first_deny_s"]))
        if best is None or key > best[:2]:
            best = (*key, b)
    return best[2] if best else None


def _caption(bundles: Sequence[dict[str, Any]], b: dict[str, Any]) -> str:
    same = [x for x in bundles if x["scenario"]["id"] == b["scenario"]["id"]]
    systems = {s["id"]: s for s in b["run"]["systems"]}
    labels = " | ".join(
        f"{systems[s]['label']}: {systems[s]['config_label']}" for s in LANES if s in systems
    )
    return (
        f"{b['scenario']['id']}, replicate {b['scenario']['replicate']} of {len(same)} exported "
        f"(run {b['run']['run_id']}, tenant generation {b['run']['generation']}). One test tenant; "
        f"{len(b['run']['personas'])} test personas; tenant admin out of scope; one read every "
        f"{b['run']['read_interval_s']:g} s. {labels}"
    )


def _lane_rows(b: dict[str, Any], system: str) -> list[tuple[str, str]]:
    """The lane's rows, top to bottom: each flipped pair, then the control pair (persona, canary)."""
    pairs = b["scenario"]["tracked_pairs"]
    order = {"flipped": 0, "control": 1}
    return [
        (p["persona"], p["canary"])
        for p in sorted(pairs, key=lambda p: order.get(p["role"], 2))
        if p["role"] in order
    ]


def timeline_figure(
    bundle: dict[str, Any],
    all_bundles: Sequence[dict[str, Any]],
    out_stem: Path,
    axis: str = "symlog",
) -> list[Path]:
    """Three lanes of ticks for one bundle; writes <out_stem>.svg and .png."""
    systems = {s["id"]: s for s in bundle["run"]["systems"]}
    lanes = [s for s in LANES if s in systems]
    role = {(p["persona"], p["canary"]): p["role"] for p in bundle["scenario"]["tracked_pairs"]}
    reads = [e for e in bundle["events"] if e["type"] == "read"]
    xs = [e["t_rel_s"] for e in reads]
    fig, axes = plt.subplots(len(lanes), 1, figsize=(11, 1.9 * len(lanes) + 1.6), sharex=True)
    axes_list: list[Axes] = list(axes) if len(lanes) > 1 else [axes]
    for ax, system in zip(axes_list, lanes, strict=True):
        rows = _lane_rows(bundle, system)
        y_of = {pair: float(len(rows) - i) for i, pair in enumerate(rows)}
        for e in (r for r in reads if r["system"] == system):
            pair = (e["persona"], e["canary"])
            if pair not in y_of:
                continue
            control = role[pair] == "control"
            kind = (
                "served"
                if e["outcome"] == "served"
                else "denied"
                if e["outcome"] == "denied"
                else "unscored"
            )
            ax.plot(
                e["t_rel_s"], y_of[pair], marker="|", color=COLORS[kind],
                markersize=5 if control else 13, markeredgewidth=0.9 if control else 1.3,
                alpha=0.6 if control else 1.0, linestyle="none",
            )  # fmt: skip
            if e["verdict"] == "leak" and not control:
                ax.plot(
                    e["t_rel_s"],
                    y_of[pair] - 0.42,
                    marker="v",
                    color=COLORS["denied"],
                    markersize=3.5,
                    linestyle="none",
                )
        ax.axvline(0.0, color="#202124", linewidth=1.1)
        first = next(
            (
                p
                for p in bundle["events"]
                if p["type"] == "phase" and p["name"] == "first_deny" and p.get("system") == system
            ),
            None,
        )
        if first is not None:
            ax.axvline(first["t_rel_s"], color=COLORS["denied"], linestyle=":", linewidth=1.2)
        s = summary_of(bundle, system)
        note = (
            f"first denied {s['first_deny_s']:.1f} s after the change" if s and s["first_deny_s"] is not None
            else f"no first denial ({s['status']})" if s else ""
        )  # fmt: skip
        ax.set_ylim(0.4, len(rows) + 0.7)
        ax.set_yticks([])
        ax.set_ylabel(
            systems[system]["label"],
            rotation=0,
            ha="right",
            va="center",
            fontsize=10,
            fontweight="bold",
        )
        ax.text(
            0.995,
            0.93,
            note,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8.5,
            color="#3c4043",
        )
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
    axes_list[0].annotate("change confirmed", xy=(0.0, 1.0), xycoords=("data", "axes fraction"), xytext=(4, 6),
                          textcoords="offset points", fontsize=9, color="#202124")  # fmt: skip
    if axis == "symlog":
        axes_list[-1].set_xscale("symlog", linthresh=30.0, linscale=1.0)
    axes_list[-1].set_xlim(min(xs) - 5, max(xs) * 1.04 if max(xs) > 0 else 1)
    axes_list[-1].set_xlabel(
        "seconds since the change was confirmed"
        + (" (linear within +-30 s, logarithmic beyond)" if axis == "symlog" else "")
    )
    handles = [
        Line2D([], [], marker="|", color=COLORS["served"], linestyle="none", markersize=10, label="served"),
        Line2D([], [], marker="|", color=COLORS["denied"], linestyle="none", markersize=10, label="denied"),
        Line2D([], [], marker="|", color=COLORS["unscored"], linestyle="none", markersize=10, label="no answer (error / not indexed)"),
        Line2D([], [], marker="v", color=COLORS["denied"], linestyle="none", markersize=5, label="served although access was revoked (leak)"),
        Line2D([], [], color=COLORS["denied"], linestyle=":", label="first denial"),
    ]  # fmt: skip
    fig.legend(
        handles=handles,
        loc="upper center",
        ncol=5,
        fontsize=8,
        frameon=False,
        bbox_to_anchor=(0.5, 0.995),
    )
    fig.text(
        0.01,
        0.005,
        "\n".join(textwrap.wrap(_caption(all_bundles, bundle), 175)),
        fontsize=7.3,
        va="bottom",
        color="#3c4043",
    )
    fig.subplots_adjust(left=0.13, right=0.985, top=0.9, bottom=0.2 + 0.02 * len(lanes))
    return _save(fig, out_stem)


def first_deny_figure(bundles: Sequence[dict[str, Any]], out_stem: Path) -> list[Path]:
    """Every replicate's first_deny_s as its own dot, per scenario x system, log scale."""
    scenarios = list(dict.fromkeys(b["scenario"]["id"] for b in bundles))
    cells: dict[tuple[str, str], list[tuple[int, float, bool]]] = defaultdict(list)
    cap = 0.0
    for b in bundles:
        cap = max(cap, float(b["run"]["poll_cap_s"]))
        for s in b["summary"]:
            if s["status"] == "DID_NOT_CONVERGE":
                cells[(b["scenario"]["id"], s["system"])].append(
                    (b["scenario"]["replicate"], float(b["run"]["poll_cap_s"]), True)
                )
            elif s["first_deny_s"] is not None:
                cells[(b["scenario"]["id"], s["system"])].append(
                    (b["scenario"]["replicate"], float(s["first_deny_s"]), False)
                )
    scenarios = [sc for sc in scenarios if any(k[0] == sc for k in cells)]
    fig, ax = plt.subplots(figsize=(max(8.0, 2.6 * len(scenarios) + 2), 5.6))
    width = 0.26
    clipped = 0
    for si, scenario in enumerate(scenarios):
        for li, system in enumerate(LANES):
            pts = sorted(cells.get((scenario, system), []))
            for k, (_rep, value, capped) in enumerate(pts):
                if value <= 0.5:
                    value, clipped = 0.5, clipped + 1
                x = si + (li - 1) * width + (k - (len(pts) - 1) / 2) * 0.035
                ax.plot(x, value, linestyle="none", marker="X" if capped else "o", markersize=9 if capped else 7,
                        color=SYSTEM_COLORS[system], markeredgecolor="#202124" if capped else "white", markeredgewidth=1.0)  # fmt: skip
    ax.set_yscale("log")
    ax.set_yticks([t for t in TICKS_S if t <= cap * 1.5])
    ax.set_yticklabels([lab for t, lab in zip(TICKS_S, TICK_LABELS, strict=True) if t <= cap * 1.5])
    ax.set_ylim(0.8 if not clipped else 0.4, cap * 1.6)
    ax.axhline(cap, color="#9aa0a6", linestyle="--", linewidth=0.9)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(scenarios, fontsize=9)
    ax.set_ylabel("time until the revoked user was first denied (log scale)")
    ax.grid(axis="y", which="major", color="#e5e7eb")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    names = {s["id"]: s for b in bundles for s in b["run"]["systems"]}
    handles = [
        Line2D(
            [], [], marker="o", color=SYSTEM_COLORS[s], linestyle="none", label=names[s]["label"]
        )
        for s in LANES
        if s in names
    ] + [
        Line2D(
            [],
            [],
            marker="X",
            color="#5f6368",
            markeredgecolor="#202124",
            linestyle="none",
            label="did not converge: drawn at the poll cap",
        )
    ]
    ax.legend(handles=handles, loc="upper left", fontsize=8.5, frameon=False)
    n_cells = {
        sc: sorted({len(cells.get((sc, s), [])) for s in LANES if cells.get((sc, s))})
        for sc in scenarios
    }
    ns = "; ".join(
        f"{sc}: n = {'-'.join(map(str, v)) if len(v) > 1 else v[0]} per system"
        for sc, v in n_cells.items()
    )
    configs = " | ".join(
        f"{names[s]['label']}: {names[s]['config_label']}" for s in LANES if s in names
    )
    note = f" {clipped} value(s) below 0.5 s are drawn at 0.5 s." if clipped else ""
    caption = (
        f"Every dot is one replicate ({ns}). One test tenant; the poll cap was {cap:g} s; reads every "
        f"{bundles[0]['run']['read_interval_s']:g} s; tenant admin out of scope.{note} {configs}"
    )
    fig.text(
        0.01,
        0.005,
        "\n".join(textwrap.wrap(caption, 150)),
        fontsize=7.3,
        va="bottom",
        color="#3c4043",
    )
    fig.subplots_adjust(left=0.09, right=0.985, top=0.97, bottom=0.27)
    return _save(fig, out_stem)


def _save(fig: Any, out_stem: Path) -> list[Path]:
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    paths = [out_stem.with_suffix(".svg"), out_stem.with_suffix(".png")]
    fig.savefig(paths[0])
    fig.savefig(paths[1], dpi=160)
    plt.close(fig)
    return paths
