"""Fill the write-up and the video script: every number comes from report.json, the replay bundles, the
tenant spec or meta.yaml; none is typed into the template.

TOKENS  {{path}} or {{path|dur}}   (inside a Markdown table the pipe may be escaped: {{path\\|dur}})
  <scenario>.<system>.<metric>.<stat>   metric: first_deny_s | leak_exposure_s | time_to_conformance_s
                                        stat: median | min | max  (report.json cells)
                                              this_bundle         (the replicate the export ranked
                                                                   closest to the medians: index.json)
  <scenario>.<system>.n                 complete replicates in the cell
  <scenario>.<system>.dnc_note          " - 1 of 5 did not stop within 60 min", empty if none
      scenario: a full id (S1-entra) or a unique prefix (S2 -> S2-nested-group-removal)
  S6a|S6b|S6c|S6pc.<arm>.leaks|trials|served   one cell of the S6 report (S6pc is the positive control)
  S6.naive.leaks|trials, S6.guarded.leaks|trials   sums over S6a-c and the naive / guarded arms
  S6a.naive.leaks|trials (also S6b, S6c, S6pc; guarded)   one scenario, summed over the naive / guarded arms
  S6.naive.leaks_via_cache_memory|leaks_via_retrieval   leaked trials by the path of their leaking turn (bundles)
  run.documents, run.personas           from tenant_spec.yaml
  run.read_interval_s, run.n_consecutive, run.poll_cap_s    from report.json config
  summary.<scenario>.<system>.first_deny_s.median|min|max, .n, .n_label; summary.totals.<field>; summary.caveats
                                        read from summary.json (README); site.links.writeup_line (empty while the
                                        link is null); env.names (the NAMES in .env.example)
  figures.timeline, figures.timeline_bundle, figures.dotplot  files of the figures folder
  meta.*                                values of meta.yaml (not measured: dates, URLs, contact, API
                                        version, schedule); meta.replicates_range and, if meta.yaml says
                                        "auto", meta.measurement_dates are computed from the report
FILTER  |dur  seconds -> "10 s", "10.3 s", "1.6 min", "42.8 min" (one decimal, no trailing .0)

A token that is unknown, or whose value is missing (null, TODO), makes the fill FAIL; nothing is
left half-filled. `fill` returns a table of every token with its value and source.
"""

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from leaklab.generator.plan import build_plan
from leaklab.spec import TenantSpec

TOKEN = re.compile(r"\{\{\s*([^{}|\\]+?)\s*(?:\\?\|\s*(\w+)\s*)?\}\}")
MIN_S = 60.0
METRICS = {
    "first_deny_s": "first_deny",
    "leak_exposure_s": "leak_exposure",
    "time_to_conformance_s": "conformance",
}
SUMMARY_FIELD = {
    "first_deny_s": "first_deny_s",
    "leak_exposure_s": "leak_exposure_s",
    "time_to_conformance_s": "time_to_conformance_s",
}
NAIVE_ARMS = ("agent_naive_a", "agent_naive_b")
GUARDED_ARMS = ("agent_guarded_b",)
S6_VARIANT = {"S6a": "S6a", "S6b": "S6b", "S6c": "S6c", "S6pc": "control"}


class WriteupError(Exception):
    """One or more tokens could not be filled; the message lists every one."""


def dur(seconds: float) -> str:
    """10 -> '10 s', 10.3 -> '10.3 s', 93.3 -> '1.6 min', 3600 -> '60 min'."""
    value, unit = (seconds, "s") if seconds < MIN_S else (seconds / MIN_S, "min")
    return f"{round(value, 1):g} {unit}"


@dataclass
class Row:
    token: str
    value: str
    source: str


@dataclass
class Context:
    report: Mapping[str, Any]
    spec: TenantSpec
    meta: Mapping[str, Any]
    bundles: Mapping[str, Mapping[str, Any]]  # scenario id -> representative bundle
    figures: Mapping[str, str]
    out_dir: Path
    rows: list[Row] = field(default_factory=list)
    agent: list[Mapping[str, Any]] = field(
        default_factory=list
    )  # agent_trial bundles of the replays folder
    summary: Mapping[str, Any] | None = None  # the replays folder's summary.json
    site: Mapping[str, Any] | None = None  # the site's meta.json (links)
    env_names: list[str] = field(default_factory=list)  # variable NAMES of .env.example

    # -- helpers ---------------------------------------------------------------------------------
    def scenario(self, token: str) -> str:
        ids = sorted({str(c["scenario"]) for c in self.report["cells"]})
        if token in ids:
            return token
        hits = [i for i in ids if i.startswith(token + "-")]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"unknown scenario {token!r} (known: {', '.join(ids)})")

    def cell(self, scenario: str, system: str) -> Mapping[str, Any]:
        for c in self.report["cells"]:
            if c["scenario"] == scenario and c["system"] == system:
                return c  # type: ignore[no-any-return]
        raise KeyError(f"no cell {scenario}/{system} in report.json")

    def s6_cell(self, variant: str, arm: str) -> Mapping[str, Any]:
        for c in (self.report.get("s6") or {}).get("cells", []):
            if c["variant"] == variant and c["arm"] == arm:
                return c  # type: ignore[no-any-return]
        raise KeyError(f"no S6 cell {variant}/{arm} in report.json")


Value = tuple[Any, str]  # value, source


def _scenario_token(ctx: Context, parts: list[str]) -> Value:
    scenario = ctx.scenario(parts[0])
    cell = ctx.cell(scenario, parts[1])
    where = f"report.json cells[{scenario}/{parts[1]}]"
    rest = parts[2:]
    if rest == ["n"]:
        return cell["replicates"], where + ".replicates"
    if rest == ["dnc_note"]:
        dnc, n = int(cell["did_not_converge"]), int(cell["replicates"])
        cap = float(ctx.report["config"]["cap_s"])
        note = f" — {dnc} of {n} did not stop within {dur(cap)}" if dnc else ""
        return note, where + ".did_not_converge, config.cap_s"
    if len(rest) == 2 and rest[0] in METRICS:
        metric, stat = rest
        if stat == "this_bundle":
            bundle = ctx.bundles.get(scenario)
            if bundle is None:
                raise KeyError(
                    f"no representative bundle for {scenario} (replays folder / index.json)"
                )
            row = next((s for s in bundle["summary"] if s["system"] == parts[1]), None)
            if row is None:
                raise KeyError(f"the bundle of {scenario} has no summary for {parts[1]}")
            name = f"{bundle['run']['run_id']}__{scenario}__r{bundle['scenario']['replicate']}"
            return row[SUMMARY_FIELD[metric]], f"bundle {name}.summary"
        if stat in ("median", "min", "max"):
            return cell[METRICS[metric]][stat], where + f".{METRICS[metric]}.{stat}"
    raise KeyError(f"unknown field {'.'.join(parts)!r}")


def _s6_sum(ctx: Context, arms: tuple[str, ...], what: str) -> Value:
    total = 0
    for variant in ("S6a", "S6b", "S6c"):
        for arm in arms:
            cell = ctx.s6_cell(variant, arm)
            total += int(cell["leak_trials"] if what == "leaks" else cell["trials"])
    return total, f"report.json s6.cells summed over S6a-c x {', '.join(arms)}"


def _leak_origin(ctx: Context, mode: str) -> tuple[int, int]:
    """(leaked trials delivered from the shared cache or memory, leaked trials delivered via retrieval)
    among the S6a-c trials of the naive or guarded arms, from the agent bundles' leaking turns."""
    via_store = via_retrieval = 0
    for b in ctx.agent:
        if b["trial"]["scenario"] not in ("S6a", "S6b", "S6c") or b["trial"]["arm"]["mode"] != mode:
            continue
        if b["summary"]["verdict"] != "LEAK":
            continue
        paths = {t["path"] for t in b["turns"] if t["verdict"] == "leak"}
        if "retrieval" in paths:
            via_retrieval += 1
        else:
            via_store += 1
    return via_store, via_retrieval


def _s6_token(ctx: Context, parts: list[str]) -> Value:
    head, arm, what = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
    if (
        head == "S6"
        and len(parts) == 3
        and arm in ("naive", "guarded")
        and what.startswith("leaks_via_")
    ):
        if not ctx.agent:
            raise KeyError("no agent_trial bundles in the replays folder")
        via_store, via_retrieval = _leak_origin(ctx, arm)
        src = "agent_trial bundles: path of the leaking turn"
        if what == "leaks_via_cache_memory":
            return via_store, src
        if what == "leaks_via_retrieval":
            return via_retrieval, src
    if (
        head == "S6"
        and len(parts) == 3
        and arm in ("naive", "guarded")
        and what in ("leaks", "trials")
    ):
        return _s6_sum(ctx, NAIVE_ARMS if arm == "naive" else GUARDED_ARMS, what)
    if (
        head in S6_VARIANT
        and len(parts) == 3
        and arm in ("naive", "guarded")
        and what in ("leaks", "trials")
    ):
        arms = NAIVE_ARMS if arm == "naive" else GUARDED_ARMS
        total = 0
        for a in arms:
            cell = ctx.s6_cell(S6_VARIANT[head], a)
            total += int(cell["leak_trials"] if what == "leaks" else cell["trials"])
        return total, f"report.json s6.cells[{S6_VARIANT[head]}/{', '.join(arms)}] summed"
    if head in S6_VARIANT and len(parts) == 3:
        cell = ctx.s6_cell(S6_VARIANT[head], arm)
        where = f"report.json s6.cells[{S6_VARIANT[head]}/{arm}]"
        if what == "leaks":
            return cell["leak_trials"], where + ".leak_trials"
        if what == "trials":
            return cell["trials"], where + ".trials"
        if what == "served":
            return cell["verdicts"].get("SERVED", 0), where + ".verdicts.SERVED"
    raise KeyError(f"unknown field {'.'.join(parts)!r}")


def _run_token(ctx: Context, parts: list[str]) -> Value:
    cfg = ctx.report["config"]
    if parts == ["run", "documents"]:
        plan = build_plan(ctx.spec)
        count = sum(len(f.docs) for f in plan.folders) + len(plan.special_files)
        return count, "tenant_spec.yaml (documents in the plan)"
    if parts == ["run", "personas"]:
        return sum(1 for u in ctx.spec.users if u.persona), "tenant_spec.yaml (persona users)"
    keys = {
        "read_interval_s": "interval_s",
        "n_consecutive": "n_consecutive",
        "poll_cap_s": "cap_s",
    }
    if len(parts) == 2 and parts[1] in keys:
        return cfg[keys[parts[1]]], f"report.json config.{keys[parts[1]]}"
    raise KeyError(f"unknown field {'.'.join(parts)!r}")


def _meta_token(ctx: Context, parts: list[str]) -> Value:
    key = ".".join(parts[1:])
    if key == "replicates_range":
        counts = [int(c["replicates"]) for c in ctx.report["cells"] if int(c["replicates"]) > 0]
        if not counts:
            raise KeyError("no cell has a complete replicate")
        lo, hi = min(counts), max(counts)
        return (str(lo) if lo == hi else f"{lo}–{hi}"), "report.json cells[].replicates (min–max)"
    if key == "measurement_dates" and ctx.meta.get(key) == "auto":
        runs = [r for r in ctx.report["runs"] if r.get("started_utc")]
        days = sorted(
            {str(r["started_utc"])[:10] for r in runs}
            | {str(r["ended_utc"])[:10] for r in runs if r.get("ended_utc")}
        )
        if not days:
            raise KeyError("the report has no run dates")
        return (
            days[0] if len(days) == 1 else f"{days[0]} to {days[-1]}"
        ), "report.json runs[] dates (UTC)"
    if key in ctx.meta:
        return ctx.meta[key], "meta.yaml"
    raise KeyError(f"meta.yaml has no {key!r}")


def _figures_token(ctx: Context, parts: list[str]) -> Value:
    key = parts[1] if len(parts) == 2 else ""
    if key in ctx.figures:
        return ctx.figures[key], "figures folder / meta.yaml"
    raise KeyError(f"no figure {key!r} (have: {', '.join(sorted(ctx.figures))})")


NL = chr(10)
PROVISIONAL_BELOW = 3  # the site's rule: fewer replicates than this is "provisional"


def _summary_token(ctx: Context, parts: list[str]) -> Value:
    """Tokens read from summary.json (the file the Findings page shows), e.g.
    summary.S1-entra.setup0.first_deny_s.median | .n | .n_label, summary.totals.replicates, summary.caveats."""
    if ctx.summary is None:
        raise KeyError("no summary.json in the replays folder")
    src = "summary.json"
    if parts[1] == "totals" and len(parts) == 3:
        return ctx.summary["totals"][parts[2]], f"{src} totals.{parts[2]}"
    if parts[1] == "caveats" and len(parts) == 2:
        return NL.join(f"- {c}" for c in ctx.summary["caveats"]), f"{src} caveats"
    ids = sorted({str(c["scenario"]) for c in ctx.summary["cells"]})
    hits = [i for i in ids if i == parts[1] or i.startswith(parts[1] + "-")]
    if len(parts) < 4 or len(hits) != 1:
        raise KeyError(f"unknown summary field {'.'.join(parts)!r}")
    cell = next(
        (c for c in ctx.summary["cells"] if c["scenario"] == hits[0] and c["system"] == parts[2]),
        None,
    )
    if cell is None:
        raise KeyError(f"no summary cell {hits[0]}/{parts[2]}")
    where = f"{src} cells[{hits[0]}/{parts[2]}]"
    rest = parts[3:]
    if rest == ["n"]:
        return cell["replicates"], where + ".replicates"
    if rest == ["n_label"]:
        n = int(cell["replicates"])
        return (
            f"provisional (n = {n})" if n < PROVISIONAL_BELOW else f"n = {n}"
        ), where + ".replicates"
    if len(rest) == 2 and rest[0] == "first_deny_s" and rest[1] in ("median", "min", "max"):
        return cell["first_deny"][rest[1]], where + f".first_deny.{rest[1]}"
    raise KeyError(f"unknown summary field {'.'.join(parts)!r}")


def _site_token(ctx: Context, parts: list[str]) -> Value:
    if ctx.site is None:
        raise KeyError("no site meta.json given")
    if parts == ["site", "links", "writeup_line"]:
        url = ctx.site["links"].get("writeup")
        return (
            f"Write-up: {url}" if url else ""
        ), "site meta.json links.writeup (empty while null)"
    raise KeyError(f"unknown site field {'.'.join(parts)!r}")


def resolve(ctx: Context, path: str) -> Value:
    parts = path.split(".")
    head = parts[0]
    if head == "summary":
        return _summary_token(ctx, parts)
    if head == "site":
        return _site_token(ctx, parts)
    if parts == ["env", "names"]:
        if not ctx.env_names:
            raise KeyError("no .env.example names")
        return NL.join(f"- `{n}`" for n in ctx.env_names), ".env.example (names only)"
    if head == "meta":
        return _meta_token(ctx, parts)
    if head == "run":
        return _run_token(ctx, parts)
    if head == "figures":
        return _figures_token(ctx, parts)
    if head == "S6" or head in S6_VARIANT:
        return _s6_token(ctx, parts)
    return _scenario_token(ctx, parts)


FILTERS: dict[str, Callable[[Any], str]] = {"dur": lambda v: dur(float(v))}


def fill(text: str, ctx: Context) -> str:
    """The text with every token replaced; WriteupError lists every token that could not be."""
    text = re.sub(
        r"<!--.*?-->", "", text, flags=re.DOTALL
    )  # template notes are not part of the output
    problems: list[str] = []
    failed_raw: set[str] = set()

    def one(m: re.Match[str]) -> str:
        path, filt = m.group(1), m.group(2)
        try:
            value, source = resolve(ctx, path)
            if value is None or (isinstance(value, str) and value.startswith("TODO")):
                raise KeyError("no value (null or TODO): " + source)
            if filt is not None:
                if filt not in FILTERS:
                    raise KeyError(f"unknown filter {filt!r}")
                if isinstance(value, str) and filt == "dur":
                    float(value)
                out = FILTERS[filt](value)
            else:
                out = (
                    str(int(value))
                    if isinstance(value, float) and value.is_integer()
                    else str(value)
                )
        except (KeyError, ValueError, TypeError) as exc:
            problems.append(f"{{{{{path}}}}}: {exc.args[0] if exc.args else exc}")
            failed_raw.add(m.group(0))
            return m.group(0)
        token = f"{path}|{filt}" if filt else path
        ctx.rows.append(Row(token, out, source))
        return out

    result = TOKEN.sub(one, text)
    leftover = [m for m in re.findall(r"\{\{[^}]*(?:\}\}?)?", result) if m not in failed_raw]
    problems += [f"{m}: not a token this fill understands" for m in leftover]
    if problems:
        raise WriteupError("\n".join(dict.fromkeys(problems)))
    return result


def _rel(path: Path, out_dir: Path) -> str:
    return os.path.relpath(path, out_dir).replace("\\", "/")


def build_context(
    report_path: Path,
    replays: Path,
    meta_path: Path,
    spec: TenantSpec,
    out_dir: Path,
    figures_dir: Path,
    allow_unset_meta: bool = False,
    site_meta: Path | None = None,
    env_example: Path | None = None,
) -> Context:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    if allow_unset_meta:
        meta = {k: (f"[unset: {k}]" if str(v).startswith("TODO") else v) for k, v in meta.items()}
    index = json.loads((replays / "index.json").read_text(encoding="utf-8"))
    bundles: dict[str, Mapping[str, Any]] = {}
    for e in index["bundles"]:
        if e.get("kind", "scenario_run") == "scenario_run" and e.get("representative"):
            bundles[str(e["scenario"])] = json.loads(
                (replays / e["bundle"]).read_text(encoding="utf-8")
            )
    agent = [
        json.loads((replays / e["bundle"]).read_text(encoding="utf-8"))
        for e in index["bundles"]
        if e.get("kind") == "agent_trial"
    ]
    summary_file = replays / "summary.json"
    summary = (
        json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.exists() else None
    )
    site = (
        json.loads(site_meta.read_text(encoding="utf-8"))
        if site_meta and site_meta.exists()
        else None
    )
    env_names: list[str] = []
    if env_example and env_example.exists():
        for line in env_example.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z][A-Z0-9_]+)=", line)
            if m:
                env_names.append(m.group(1))
    figures: dict[str, str] = {}
    timeline = bundles.get("S1-entra")
    if timeline is not None:
        name = f"{timeline['run']['run_id']}__S1-entra__r{timeline['scenario']['replicate']}"
        figures["timeline_bundle"] = name
        png = next(iter(sorted(figures_dir.glob(f"timeline__{name}__*.png"))), None)
        if png is not None:
            figures["timeline"] = _rel(png, out_dir)
    dot = figures_dir / "first_deny_dotplot.png"
    if dot.exists():
        figures["dotplot"] = _rel(dot, out_dir)
    if meta.get("architecture_figure"):
        figures["architecture"] = str(meta["architecture_figure"])
    return Context(
        report,
        spec,
        meta,
        bundles,
        figures,
        out_dir,
        agent=agent,
        summary=summary,
        site=site,
        env_names=env_names,
    )


def format_table(rows: list[Row]) -> list[tuple[str, str, str]]:
    seen: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        seen.setdefault(r.token, (r.token, r.value, r.source))
    return list(seen.values())
