"""Schema v1.1 of the replay bundles: window, last_served, NOTHING_TO_REVOKE, caveats, S6 agent trials,
ranking. Everything runs on fakes (no tenant, no network)."""

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from benchmark_fakes import FakeRuns
from leaklab.agent.s6.report import build_s6_report
from leaklab.replay import export as export_mod
from leaklab.replay.agent import export_agent
from leaklab.replay.export import (
    Bundle,
    ExportResult,
    _indexer_runs,
    dur,
    export_folder,
    load_schema,
    rank_replicates,
    write_bundles,
)
from leaklab.replay.redact import find_leaks
from leaklab.replay.summary import SummaryMismatch
from leaklab.runner import RunConfig, load_events
from leaklab.runner.run import spec_sha256
from leaklab.spec import generate_canaries
from runner_fakes import SPEC, SPEC_PATH

SHA = spec_sha256(SPEC_PATH)
CONFIG = RunConfig(baseline_k=3, baseline_cap_s=300.0, n_consecutive=4, cap_s=900.0, interval_s=5.0)
SYSTEMS = ["setup0", "setup_a_basic", "setup_b"]
CAN = generate_canaries(SPEC)
D1, D2 = CAN["S1-Direct"]
NEVER = 10_000_000.0
V10_SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "replay_bundle.schema.json"


def make_folder(tmp_path: Path, replicates: int = 1, **kw: Any) -> Path:
    runs = FakeRuns({"setup0": {"lag_s": 100.0}, "setup_a_basic": {"lag_s": 40.0}, **kw})
    folder = tmp_path / "runs"
    folder.mkdir(exist_ok=True)
    for i in range(1, replicates + 1):
        runs(
            spec_path=SPEC_PATH,
            scenario_ids=["S1-direct"],
            system_names=SYSTEMS,
            config=CONFIG,
            out_dir=folder,
            suffix=f"_r{i}",
        )
    return folder


def reads_of(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in bundle["events"] if e["type"] == "read"]


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> ExportResult:
    return export_folder(make_folder(tmp_path_factory.mktemp("v11")), SPEC, SHA)


# -- 1 status enum -------------------------------------------------------------------------------


def test_nothing_to_revoke_is_its_own_status_not_not_applicable(tmp_path: Path) -> None:
    folder = make_folder(
        tmp_path,
        setup_a_basic={
            "lag_s": 0.0,
            "deny_until": {("p-plateng", D1): NEVER, ("p-plateng", D2): NEVER},
        },
    )
    result = export_folder(folder, SPEC, SHA)
    assert not result.skipped
    status = {s["system"]: s["status"] for s in result.bundles[0].data["summary"]}
    assert status["setup_a_basic"] == "NOTHING_TO_REVOKE"
    assert status["setup0"] == "CONVERGED" and "NOT_APPLICABLE" not in status.values()
    enum = load_schema()["$defs"]["scenario_run"]["properties"]["summary"]["items"]["properties"][
        "status"
    ]["enum"]
    assert "NOTHING_TO_REVOKE" in enum


# -- 2 window ------------------------------------------------------------------------------------


def test_bundle_is_trimmed_to_the_window_and_records_what_was_dropped(
    exported: ExportResult,
) -> None:
    b = exported.bundles[0].data
    prov = b["provenance"]
    reads = reads_of(b)
    assert all(prov["window_start"] <= r["t"] <= prov["window_end"] for r in reads)
    assert b["schema_version"] == "1.1" and b["kind"] == "scenario_run"
    full = sum(s["n_reads"] for s in b["summary"])  # the summary covers the whole journal
    assert len(reads) + prov["reads_dropped"] == full


def test_a_shorter_pad_drops_reads_but_never_changes_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = make_folder(tmp_path)
    wide = export_folder(folder, SPEC, SHA).bundles[0].data
    monkeypatch.setattr(export_mod, "WINDOW_PAD_S", -30.0)
    narrow = export_folder(folder, SPEC, SHA).bundles[0].data
    assert narrow["provenance"]["reads_dropped"] > wide["provenance"]["reads_dropped"]
    assert narrow["summary"] == wide["summary"]  # still computed (and asserted) on the full journal


def test_the_window_is_capped_at_the_poll_cap(exported: ExportResult) -> None:
    b = exported.bundles[0].data
    t_change = next(e for e in b["events"] if e["type"] == "mutation")["t"]
    assert b["provenance"]["window_end"] <= t_change + b["run"]["poll_cap_s"] + 1e-6


# -- 3 indexer status ----------------------------------------------------------------------------


def test_an_indexer_run_without_a_finish_is_unfinished_at_journal_end() -> None:
    events = [
        {
            "type": "system_event",
            "system": "setup_a_basic",
            "kind": "indexer_run_started",
            "start": 100.0,
        }
    ]
    runs = _indexer_runs(events, SYSTEMS, (0.0, 500.0), 50.0)
    assert len(runs) == 1
    assert runs[0]["status"] == "unfinished_at_journal_end" and runs[0]["t_end"] is None


def test_a_finished_indexer_run_keeps_its_own_items_processed() -> None:
    events = [
        {"type": "system_event", "system": "setup_a_basic", "kind": "indexer_run_started", "start": 100.0},
        {"type": "system_event", "system": "setup_a_basic", "kind": "indexer_run", "start": 100.0, "end": 110.0,
         "status": "success", "items_processed": 7},
        {"type": "system_event", "system": "setup_b", "kind": "indexer_run", "start": 300.0, "end": 310.0,
         "status": "success", "items_processed": 99},
    ]  # fmt: skip
    first = _indexer_runs(events, SYSTEMS, (0.0, 200.0), 50.0)
    assert [(r["status"], r["items_processed"]) for r in first] == [("success", 7)]


# -- 4 last_served -------------------------------------------------------------------------------


def test_last_served_is_the_last_served_flipped_read_after_the_change(
    exported: ExportResult,
) -> None:
    b = exported.bundles[0].data
    flipped = {
        (p["persona"], p["canary"])
        for p in b["scenario"]["tracked_pairs"]
        if p["role"] == "flipped"
    }
    marks = {
        e["system"]: e["t_rel_s"]
        for e in b["events"]
        if e["type"] == "phase" and e["name"] == "last_served"
    }
    assert set(marks) == {"setup0", "setup_a_basic"}  # setup_b never serves after the change
    for system, rel in marks.items():
        served = [
            r["t_rel_s"]
            for r in reads_of(b)
            if r["system"] == system and r["outcome"] == "served" and r["t_rel_s"] >= 0
            and (r["persona"], r["canary"]) in flipped
        ]  # fmt: skip
        assert rel == max(served)
    assert marks["setup0"] > marks["setup_a_basic"]


# -- 5 caveats -----------------------------------------------------------------------------------


def test_caveats_come_from_the_run_metadata_and_say_the_sample_size(exported: ExportResult) -> None:
    c = " ".join(exported.bundles[0].data["caveats"])
    assert "One test tenant" in c and "tenant admin" in c.lower()
    for s in exported.bundles[0].data["run"]["systems"]:
        assert s["config_label"] in c
    assert "Sample size: 1 replicate of S1-direct" in c
    assert "Reads every 5 s" in c and "Polling stopped 15 min" in c


def test_sample_size_counts_the_replicates_of_the_export(tmp_path: Path) -> None:
    result = export_folder(make_folder(tmp_path, replicates=2), SPEC, SHA)
    assert len(result.bundles) == 2
    for b in result.bundles:
        assert any("Sample size: 2 replicates of S1-direct" in c for c in b.data["caveats"])


def test_every_bundle_needs_caveats() -> None:
    schema = load_schema()
    assert "caveats" in schema["$defs"]["scenario_run"]["required"]
    assert "caveats" in schema["$defs"]["agent_trial"]["required"]


def test_dur_formats_like_the_write_up() -> None:
    assert [dur(10), dur(10.3), dur(93.3), dur(2565), dur(3600), dur(5)] == [
        "10 s", "10.3 s", "1.6 min", "42.8 min", "60 min", "5 s",
    ]  # fmt: skip


# -- 6 S6 agent bundles --------------------------------------------------------------------------

FLD = CAN["Finance/Leadership"]
OWNER_OID = "e0aba0f9-9b9e-40e4-a271-b52d96d96084"
ITEM_ID = "01RQLOFTUVBCRNGAO2IFB2B36KAHRIP45A"


def _answer(*canaries: str) -> str:
    return "Here is what the documents say:\n" + "\n".join(f"Reference code: {c}" for c in canaries)


def _step(
    arm: str, trial: int, step: str, persona: str, t: float, answer: str, **kw: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "s6_step", "variant": "S6a", "arm": arm, "trial": trial, "step": step, "persona": persona,
        "question": f"question {step}", "t0": t, "t1": t + 1.0, "answer": answer, "via": "llm",
        "cache": {"similarity": 0.0, "threshold": 0.65, "candidate": False, "entry_id": None,
                  "entry_owner": None, "served": False, "denied_by_guard": False},
        "memory": [], "passages": [ITEM_ID], "guard": [],
    }  # fmt: skip
    base.update(kw)
    return base


def s6_journal(path: Path) -> None:
    hit = {"similarity": 0.83, "threshold": 0.65, "candidate": True, "entry_id": 1, "entry_owner": OWNER_OID,
           "served": True, "denied_by_guard": False}  # fmt: skip
    denied = {
        **hit,
        "served": False,
        "denied_by_guard": True,
        "guard_reason": "denied:no_matching_grant",
    }
    events: list[dict[str, Any]] = [
        {
            "type": "s6_started", "t": 900.0, "version": 2, "n": 3, "settle_s": 30.0, "generation": SPEC.naming.generation,
            "llm": "stub", "embedder": "hash-256", "spec_sha256": SHA,
            "arms": [{"name": "agent_naive_a", "mode": "naive", "retrieval": "setup_a"},
                     {"name": "agent_guarded_b", "mode": "guarded", "retrieval": "setup_b"}],
            "queries": {"version": 2, "cache_threshold": 0.65, "memory_threshold": 0.3, "variants": {
                "S6a": {"kind": "cross_user_cache", "seed": "seed", "followups": ["f"],
                        "source": "Finance/Leadership document 01"}}},
        }
    ]  # fmt: skip
    seed_answer = _answer(FLD[0])
    for arm, trial, followup in (
        ("agent_naive_a", 1, _step("agent_naive_a", 1, "followup", "p-intern", 1010.0, seed_answer, via="cache", cache=hit)),
        ("agent_guarded_b", 1, _step("agent_guarded_b", 1, "followup", "p-intern", 1110.0, _answer(CAN["General"][0]),
            cache=denied, guard=[{"target": "cache", "entry_id": 1, "allowed": False,
                                  "reason": "denied:no_matching_grant", "results": [[ITEM_ID, False, "x"]]}])),
        ("agent_naive_a", 2, _step("agent_naive_a", 2, "followup", "p-intern", 1210.0, _answer(CAN["General"][0]))),
    ):  # fmt: skip
        t = float(followup["t0"]) - 10.0
        events += [
            {"type": "s6_trial_started", "t": t, "variant": "S6a", "arm": arm, "trial": trial, "kind": "cross_user_cache"},
            _step(arm, trial, "seed", "p-finlead", t, seed_answer),
            followup,
            {"type": "s6_snapshot", "t": t + 2, "variant": "S6a", "arm": arm, "trial": trial, "texts": [seed_answer]},
            {"type": "s6_trial_done", "t": t + 3, "variant": "S6a", "arm": arm, "trial": trial},
        ]  # fmt: skip
    events.append({"type": "run_finished", "t": 1300.0})
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def s6_folder(tmp_path: Path) -> tuple[Path, Path]:
    journal = tmp_path / "s6_fake.jsonl"
    s6_journal(journal)
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"s6": build_s6_report(SPEC, load_events(journal), journal.name).model_dump()}),
        encoding="utf-8",
    )
    return journal, report


def agent_bundles(tmp_path: Path) -> dict[str, dict[str, Any]]:
    journal, report = s6_folder(tmp_path)
    res = export_agent(journal, report, SPEC, spec_sha256=SHA)
    return {b.name.split("__", 1)[1]: b.data for b in res.bundles}


def test_s6_naive_trial_shows_the_leak_in_the_bundle(tmp_path: Path) -> None:
    b = agent_bundles(tmp_path)["S6a__agent_naive_a__t1"]
    assert (
        b["kind"] == "agent_trial"
        and b["validity"] == "VALID"
        and b["summary"]["verdict"] == "LEAK"
    )
    seed, follow = b["turns"]
    assert (seed["step"], seed["persona"], seed["verdict"]) == ("seed", "p-finlead", "ok")
    assert (
        follow["step"] == "followup"
        and follow["path"] == "cache_hit"
        and follow["similarity"] == 0.83
    )
    assert (
        follow["verdict"] == "leak"
        and follow["guard_decision"] == "n/a"
        and follow["guard_reason"] is None
    )
    leak = follow["delivered_canaries"][0]
    assert (leak["expected"], leak["verdict"]) == ("deny", "leak")
    assert leak["doc_label"] in follow["source_doc_labels"]


def test_s6_guarded_trial_shows_the_guard_decision_and_no_leak(tmp_path: Path) -> None:
    b = agent_bundles(tmp_path)["S6a__agent_guarded_b__t1"]
    follow = b["turns"][1]
    assert b["summary"]["verdict"] == "OK" and follow["verdict"] == "ok"
    assert (
        follow["guard_decision"] == "denied"
        and follow["guard_reason"] == "denied:no_matching_grant"
    )
    assert follow["path"] == "retrieval" and follow["similarity"] == 0.83


def test_s6_trial_without_a_cache_candidate_is_invalid_never_ok(tmp_path: Path) -> None:
    b = agent_bundles(tmp_path)["S6a__agent_naive_a__t2"]
    assert b["validity"] == "INVALID" and b["summary"]["verdict"] == "INVALID"
    assert b["summary"]["reason"] == "no_cache_candidate"


def test_s6_bundles_hold_no_object_or_item_ids(tmp_path: Path) -> None:
    for b in agent_bundles(tmp_path).values():
        text = json.dumps(b)
        assert OWNER_OID not in text and ITEM_ID not in text
        assert find_leaks(b) == []


def test_s6_planted_report_mismatch_fails_loudly(tmp_path: Path) -> None:
    journal, report = s6_folder(tmp_path)
    data = json.loads(report.read_text(encoding="utf-8"))
    data["s6"]["cells"][0]["rows"][0]["verdict"] = "OK"
    report.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SummaryMismatch):
        export_agent(journal, report, SPEC, spec_sha256=SHA)


def test_s6_bundles_are_in_the_export_and_the_index(tmp_path: Path) -> None:
    folder = make_folder(tmp_path)
    journal, report = s6_folder(tmp_path)
    (folder / journal.name).write_text(journal.read_text(encoding="utf-8"), encoding="utf-8")
    data = json.loads(report.read_text(encoding="utf-8"))
    (folder / "report.json").write_text(json.dumps(data), encoding="utf-8")
    result = export_folder(folder, SPEC, SHA)
    kinds = [b.data["kind"] for b in result.bundles]
    assert kinds.count("agent_trial") == 3 and kinds.count("scenario_run") == 1
    write_bundles(result, tmp_path / "out")
    index = json.loads((tmp_path / "out" / "index.json").read_text(encoding="utf-8"))
    agent = [e for e in index["bundles"] if e["kind"] == "agent_trial"]
    assert {(e["arm"], e["trial"], e["validity"]) for e in agent} >= {
        ("agent_naive_a", 2, "INVALID")
    }


# -- 7 ranking -----------------------------------------------------------------------------------


def fake_bundle(
    rep: int, first_deny: dict[str, float | None], status: dict[str, str] | None = None
) -> Bundle:
    status = status or {}
    return Bundle(
        f"r{rep}",
        {
            "kind": "scenario_run",
            "scenario": {"id": "S", "replicate": rep},
            "run": {"poll_cap_s": 3600.0},
            "summary": [
                {"system": s, "first_deny_s": v, "status": status.get(s, "CONVERGED")}
                for s, v in first_deny.items()
            ],
        },
    )


def test_ranking_picks_the_replicate_closest_to_the_per_system_medians() -> None:
    bundles = [
        fake_bundle(1, {"setup0": 100.0, "setup_b": 10.0}),
        fake_bundle(2, {"setup0": 200.0, "setup_b": 10.0}),  # on both medians
        fake_bundle(3, {"setup0": 300.0, "setup_b": 50.0}),
    ]
    ranks = rank_replicates(bundles)
    assert ranks == {"r2": (1, True), "r1": (2, False), "r3": (3, False)}


def test_ranking_counts_a_replicate_that_did_not_converge_at_the_cap_and_skips_nothing_to_revoke() -> (
    None
):
    bundles = [
        fake_bundle(1, {"setup0": 3000.0, "setup_a_basic": None}, {"setup_a_basic": "NOTHING_TO_REVOKE"}),
        fake_bundle(2, {"setup0": None, "setup_a_basic": None}, {"setup0": "DID_NOT_CONVERGE", "setup_a_basic": "NOTHING_TO_REVOKE"}),
        fake_bundle(3, {"setup0": 3000.0, "setup_a_basic": None}, {"setup_a_basic": "NOTHING_TO_REVOKE"}),
    ]  # fmt: skip
    ranks = rank_replicates(bundles)
    assert ranks["r2"][0] == 3 and not ranks["r2"][1]
    assert ranks["r1"] == (1, True)  # tie with r3: the lower replicate wins


def test_the_index_carries_rank_and_representative(tmp_path: Path) -> None:
    result = export_folder(make_folder(tmp_path, replicates=3), SPEC, SHA)
    write_bundles(result, tmp_path / "out")
    index = json.loads((tmp_path / "out" / "index.json").read_text(encoding="utf-8"))
    entries = [e for e in index["bundles"] if e["kind"] == "scenario_run"]
    assert sorted(e["rank"] for e in entries) == [1, 2, 3]
    assert [e["representative"] for e in entries].count(True) == 1
    assert next(e for e in entries if e["representative"])["rank"] == 1
    assert index["schema_version"] == "1.1" and "metric" in index["ranking"]


# -- schema versions -----------------------------------------------------------------------------


def downgrade(b: dict[str, Any]) -> dict[str, Any]:
    old = copy.deepcopy(b)
    old["schema_version"] = "1.0"
    for key in ("kind", "caveats"):
        del old[key]
    for key in ("window_start", "window_end", "reads_dropped"):
        del old["provenance"][key]
    old["events"] = [
        e for e in old["events"] if not (e["type"] == "phase" and e["name"] == "last_served")
    ]
    return old


def test_a_v10_bundle_is_still_valid_against_the_v10_schema_and_not_against_v11(
    exported: ExportResult,
) -> None:
    old = downgrade(exported.bundles[0].data)
    v10 = json.loads(V10_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(v10).validate(old)
    assert not jsonschema.Draft202012Validator(load_schema()).is_valid(old)


def test_v11_rejects_a_bundle_without_caveats_or_kind(exported: ExportResult) -> None:
    v = jsonschema.Draft202012Validator(load_schema())
    for key in ("caveats", "kind"):
        broken = copy.deepcopy(exported.bundles[0].data)
        del broken[key]
        assert not v.is_valid(broken), key
