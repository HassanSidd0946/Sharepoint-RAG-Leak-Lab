"""The write-up fill: tokens, the dur filter, S6 sums, dnc_note, failure on anything unknown or unset."""

from pathlib import Path
from typing import Any

import pytest

from leaklab.replay.writeup import Context, WriteupError, dur, fill, format_table
from runner_fakes import SPEC


def stat(median: float | None, lo: float | None = None, hi: float | None = None) -> dict[str, Any]:
    return {
        "n": 1,
        "min": lo if lo is not None else median,
        "median": median,
        "max": hi if hi is not None else median,
    }


def cell(
    scenario: str, system: str, median: float | None, replicates: int = 5, dnc: int = 0
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "system": system,
        "replicates": replicates,
        "did_not_converge": dnc,
        "first_deny": stat(median, 9.0, 99.0),
        "leak_exposure": stat(median),
        "conformance": stat(median),
    }


def s6_cell(variant: str, arm: str, leaks: int, trials: int = 3, served: int = 0) -> dict[str, Any]:
    return {
        "variant": variant,
        "arm": arm,
        "leak_trials": leaks,
        "trials": trials,
        "verdicts": {"SERVED": served},
    }


def make_ctx(**meta: Any) -> Context:
    s6 = [
        s6_cell(v, a, 3 if a != "agent_guarded_b" else 0)
        for v in ("S6a", "S6b", "S6c")
        for a in ("agent_naive_a", "agent_naive_b", "agent_guarded_b")
    ] + [
        s6_cell("control", a, 0, served=3)
        for a in ("agent_naive_a", "agent_naive_b", "agent_guarded_b")
    ]
    report = {
        "config": {"interval_s": 5.0, "n_consecutive": 10.0, "cap_s": 3600.0},
        "cells": [
            cell("S1-entra", "setup0", 2565.0, replicates=5, dnc=1),
            cell("S1-entra", "setup_b", 10.0, replicates=7),
            cell("S2-nested-group-removal", "setup0", 93.3, replicates=2),
            cell("S1-spgroup", "setup_a_basic", None, replicates=0),
        ],
        "runs": [
            {"started_utc": "2026-10-18 11:46:27Z", "ended_utc": "2026-10-18 12:51:06Z"},
            {"started_utc": "2026-10-24 01:00:00Z", "ended_utc": "2026-10-24 03:00:00Z"},
        ],
        "s6": {"cells": s6},
    }
    bundle = {
        "run": {"run_id": "run_x_r3"},
        "scenario": {"replicate": 3},
        "summary": [
            {
                "system": "setup0",
                "first_deny_s": 2700.0,
                "leak_exposure_s": 3000.0,
                "time_to_conformance_s": 3100.0,
            }
        ],
    }
    metadata = {
        "publish_date": "2026-10-26",
        "measurement_dates": "auto",
        "stale_after_s": 30,
        **meta,
    }
    figures = {"timeline": "../f/t.png"}
    return Context(report, SPEC, metadata, {"S1-entra": bundle}, figures, Path("."))


def test_dur_formats_seconds_and_minutes_with_one_decimal() -> None:
    assert dur(10) == "10 s"
    assert dur(10.34) == "10.3 s"
    assert dur(59.9) == "59.9 s"
    assert dur(93.3) == "1.6 min"
    assert dur(2565) == "42.8 min"
    assert dur(3600) == "60 min"


def test_scenario_tokens_median_min_max_n_and_the_dur_filter() -> None:
    ctx = make_ctx()
    out = fill(
        "{{S1-entra.setup0.first_deny_s.median|dur}} ({{S1-entra.setup0.first_deny_s.min|dur}}-{{S1-entra.setup0.first_deny_s.max|dur}}) n={{S1-entra.setup0.n}}",
        ctx,
    )
    assert out == "42.8 min (9 s-1.6 min) n=5"


def test_an_escaped_pipe_works_inside_a_table_cell() -> None:
    assert fill("{{S1-entra.setup_b.first_deny_s.median\\|dur}}", make_ctx()) == "10 s"


def test_a_unique_scenario_prefix_is_an_alias_but_an_ambiguous_one_fails() -> None:
    ctx = make_ctx()
    assert fill("{{S2.setup0.first_deny_s.median|dur}}", ctx) == "1.6 min"
    with pytest.raises(WriteupError, match="unknown scenario"):
        fill("{{S1.setup0.first_deny_s.median|dur}}", ctx)


def test_this_bundle_reads_the_representative_bundle() -> None:
    assert fill("{{S1-entra.setup0.first_deny_s.this_bundle|dur}}", make_ctx()) == "45 min"
    with pytest.raises(WriteupError, match="no representative bundle"):
        fill("{{S2.setup0.first_deny_s.this_bundle|dur}}", make_ctx())


def test_dnc_note_names_the_count_and_the_cap_and_is_empty_without_any() -> None:
    ctx = make_ctx()
    assert fill("[{{S1-entra.setup0.dnc_note}}]", ctx) == "[ — 1 of 5 did not stop within 60 min]"
    assert fill("[{{S1-entra.setup_b.dnc_note}}]", ctx) == "[]"


def test_s6_cells_and_sums() -> None:
    ctx = make_ctx()
    assert fill("{{S6a.agent_naive_a.leaks}}/{{S6a.agent_naive_a.trials}}", ctx) == "3/3"
    assert (
        fill("{{S6pc.agent_guarded_b.served}} of {{S6pc.agent_guarded_b.trials}}", ctx) == "3 of 3"
    )
    assert (
        fill("{{S6.naive.leaks}} of {{S6.naive.trials}}", ctx) == "18 of 18"
    )  # S6a-c x two naive arms x 3
    assert fill("{{S6.guarded.leaks}} of {{S6.guarded.trials}}", ctx) == "0 of 9"


def test_run_meta_and_figure_tokens() -> None:
    ctx = make_ctx()
    assert fill("{{run.documents}} docs, {{run.personas}} personas", ctx) == "45 docs, 7 personas"
    assert (
        fill("{{run.read_interval_s|dur}} {{run.n_consecutive}} {{run.poll_cap_s|dur}}", ctx)
        == "5 s 10 60 min"
    )
    assert fill("{{meta.replicates_range}}", ctx) == "2–7"
    assert fill("{{meta.measurement_dates}}", ctx) == "2026-10-18 to 2026-10-24"
    assert fill("{{meta.stale_after_s|dur}}", ctx) == "30 s"
    assert fill("{{figures.timeline}}", ctx) == "../f/t.png"


def test_unknown_tokens_fail_and_all_problems_are_listed() -> None:
    ctx = make_ctx()
    with pytest.raises(WriteupError) as err:
        fill("{{nonsense.thing}} and {{S1-entra.setup0.first_deny_s.mean}} and {{meta.nope}}", ctx)
    text = str(err.value)
    assert "nonsense.thing" in text and "first_deny_s.mean" in text and "meta.nope" in text


def test_a_missing_value_or_a_todo_fails_instead_of_printing_nothing() -> None:
    ctx = make_ctx(contact="TODO")
    with pytest.raises(WriteupError, match="no value"):
        fill("{{S1-spgroup.setup_a_basic.first_deny_s.median|dur}}", ctx)
    with pytest.raises(WriteupError, match="TODO"):
        fill("{{meta.contact}}", ctx)
    with pytest.raises(WriteupError, match="no figure"):
        fill("{{figures.architecture}}", ctx)


def test_a_malformed_token_and_an_unknown_filter_fail() -> None:
    with pytest.raises(WriteupError, match="unknown filter"):
        fill("{{S1-entra.setup0.n|round}}", make_ctx())
    with pytest.raises(WriteupError):
        fill("{{S1-entra.setup0.n", make_ctx())


def test_template_comments_are_dropped_and_the_token_table_lists_every_token() -> None:
    ctx = make_ctx()
    out = fill("<!-- {{token}} is a note -->A {{S1-entra.setup_b.n}} A {{S1-entra.setup_b.n}}", ctx)
    assert out == "A 7 A 7"
    rows = format_table(ctx.rows)
    assert rows == [("S1-entra.setup_b.n", "7", "report.json cells[S1-entra/setup_b].replicates")]


def agent_bundle(
    scenario: str, mode: str, verdict: str, paths: list[tuple[str, str]]
) -> dict[str, Any]:
    return {
        "trial": {"scenario": scenario, "arm": {"mode": mode}},
        "summary": {"verdict": verdict},
        "turns": [{"path": p, "verdict": v} for p, v in paths],
    }


def test_leak_origin_counts_the_path_of_the_leaking_turn() -> None:
    ctx = make_ctx()
    ctx.agent = [
        agent_bundle("S6a", "naive", "LEAK", [("retrieval", "ok"), ("cache_hit", "leak")]),
        agent_bundle("S6c", "naive", "LEAK", [("memory_hit", "leak")]),
        agent_bundle("S6b", "naive", "LEAK", [("cache_hit", "leak"), ("retrieval", "leak")]),
        agent_bundle("S6a", "guarded", "OK", [("retrieval", "ok")]),
        agent_bundle("positive_control", "naive", "SERVED", [("cache_hit", "ok")]),
    ]
    assert (
        fill("{{S6.naive.leaks_via_cache_memory}}/{{S6.naive.leaks_via_retrieval}}", ctx) == "2/1"
    )
    assert (
        fill("{{S6.guarded.leaks_via_cache_memory}}/{{S6.guarded.leaks_via_retrieval}}", ctx)
        == "0/0"
    )


def test_leak_origin_fails_without_agent_bundles() -> None:
    with pytest.raises(WriteupError, match="no agent_trial bundles"):
        fill("{{S6.naive.leaks_via_retrieval}}", make_ctx())
