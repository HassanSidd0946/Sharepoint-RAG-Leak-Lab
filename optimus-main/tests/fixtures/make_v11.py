"""Builds the v1.1 fixtures from the small v1.0 fixture. Test data only: it says nothing about any real run.

python tests/fixtures/make_v11.py     (run from optimus-main/)
"""

import copy
import json
from pathlib import Path

here = Path(__file__).parent
v10 = json.loads((here / "bundle.v10.json").read_text(encoding="utf-8"))

# -- scenario_run v1.1: setup_b never served the pair (NOTHING_TO_REVOKE), the others have last_served --
b = copy.deepcopy(v10)
b["schema_version"] = "1.1"
b["kind"] = "scenario_run"
b["caveats"] = ["fixture caveat one", "fixture caveat two"]
b["provenance"].update({"window_start": 980.0, "window_end": 1100.0, "reads_dropped": 3})
flipped = {
    (p["persona"], p["canary"]) for p in b["scenario"]["tracked_pairs"] if p["role"] == "flipped"
}
for e in b["events"]:
    if e["type"] == "read" and e["system"] == "setup_b" and (e["persona"], e["canary"]) in flipped:
        e["outcome"] = "denied"
        e["expected"] = "allow" if e["t_rel_s"] < 0 else "deny"
        e["verdict"] = "lockout" if e["t_rel_s"] < 0 else "ok"
b["events"] = [e for e in b["events"] if not (e["type"] == "phase" and e["system"] == "setup_b")]
for system, rel in (("setup0", 35.0), ("setup_a_basic", 15.0)):
    b["events"].append(
        {
            "type": "phase",
            "t": 1000.0 + rel,
            "t_rel_s": rel,
            "name": "last_served",
            "system": system,
            "detail": None,
        }
    )
b["events"].sort(key=lambda e: e["t"])
for s in b["summary"]:
    if s["system"] == "setup_b":
        s.update(
            {
                "status": "NOTHING_TO_REVOKE",
                "first_deny_s": None,
                "leak_exposure_s": None,
                "time_to_conformance_s": None,
            }
        )
(here / "bundle.v11.json").write_text(json.dumps(b, indent=1), encoding="utf-8")


# -- agent_trial v1.1 --
def turn(t, step, persona, query, path, sim, labels, canaries, verdict, guard, reason):
    return {
        "t": 5000.0 + t, "t_rel_s": t, "step": step, "persona": persona, "query": query, "path": path,
        "similarity": sim, "source_doc_labels": labels, "delivered_canaries": canaries, "verdict": verdict,
        "guard_decision": guard, "guard_reason": reason,
    }  # fmt: skip


secret = {
    "canary": "CANARY-GFIXTUR-FIN-01-aaaaaa",
    "doc_label": "Finance/doc-01.txt",
    "expected": "deny",
    "verdict": "leak",
}
own = {
    "canary": "CANARY-GFIXTUR-GEN-01-bbbbbb",
    "doc_label": "General/doc-01.txt",
    "expected": "allow",
    "verdict": "ok",
}
common_run = {
    "run_id": "s6_fixture", "generation": "GFIXTUR", "started_at": 4990.0, "llm": "stub", "embedder": "hash-fixture",
    "cache_threshold": 0.65, "memory_threshold": 0.3, "personas": b["run"]["personas"],
}  # fmt: skip
prov = {
    "journal_file": "s6_fixture.jsonl",
    "report_file": "report.json",
    "exporter_version": "fixture",
    "git_commit": "abc1234",
    "exported_at": 6000.0,
}


def trial(
    arm_id, arm_label, mode, number, turns, verdict, reason, valid=True, leaks=0, guard_denials=0
):
    return {
        "schema_version": "1.1", "kind": "agent_trial", "run": common_run,
        "trial": {"scenario": "S6a", "kind": "cross_user_cache", "arm": {"id": arm_id, "label": arm_label, "mode": mode},
                  "n": 3, "trial": number, "question_source": "Finance/Leadership document 01"},
        "turns": turns, "mutations": [], "validity": "VALID" if valid else "INVALID",
        "summary": {"verdict": verdict, "reason": reason, "similarity": 0.83, "cache_status": "hit", "delivered": 1,
                    "leaks": leaks, "guard_denials": guard_denials, "exposure_stored": 2, "exposure_unsafe": 1, "exposure_pairs": 4},
        "caveats": ["fixture agent caveat"], "provenance": prov,
    }  # fmt: skip


seed = turn(
    0.5,
    "seed",
    "p-b",
    "what does the finance document say",
    "retrieval",
    None,
    ["Finance/doc-01.txt"],
    [{**secret, "expected": "allow", "verdict": "ok"}],
    "ok",
    "n/a",
    None,
)
naive = trial("agent_naive_a", "Naive agent, retrieval A", "naive", 1,
              [seed, turn(4.0, "followup", "p-a", "tell me about the finance document", "cache_hit", 0.83, ["Finance/doc-01.txt"], [secret], "leak", "n/a", None)],
              "LEAK", "1 canary delivery(ies) not allowed", leaks=1)  # fmt: skip
guarded = trial("agent_guarded_b", "Guarded agent, retrieval B", "guarded", 1,
                [seed, turn(4.0, "followup", "p-a", "tell me about the finance document", "retrieval", 0.83, ["General/doc-01.txt"], [own], "ok", "denied", "denied:no_matching_grant")],
                "OK", "designed situation occurred, nothing leaked", guard_denials=1)  # fmt: skip
invalid = trial("agent_naive_a", "Naive agent, retrieval A", "naive", 2,
                [seed, turn(4.0, "followup", "p-a", "tell me about the finance document", "retrieval", None, ["General/doc-01.txt"], [own], "ok", "n/a", None)],
                "INVALID", "no_cache_candidate", valid=False)  # fmt: skip
invalid["summary"].update({"similarity": None, "cache_status": "below_threshold"})
for name, data in (("agent.leak", naive), ("agent.guarded", guarded), ("agent.invalid", invalid)):
    (here / f"{name}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def agent_entry(file, d):
    t = d["trial"]
    return {"bundle": file, "kind": "agent_trial", "run_id": d["run"]["run_id"], "scenario": t["scenario"], "arm": t["arm"]["id"],
            "trial": t["trial"], "validity": d["validity"], "verdict": d["summary"]["verdict"]}  # fmt: skip


index = {
    "schema_version": "1.1",
    "ranking": {"metric": "fixture metric"},
    "bundles": [
        {"bundle": "fixture-v11.json", "kind": "scenario_run", "run_id": "run_fixture_r1", "scenario": "S-fixture", "replicate": 1,
         "systems": ["setup0", "setup_a_basic", "setup_b"],
         "summary": [{"system": "setup0", "status": "CONVERGED"}, {"system": "setup_b", "status": "NOTHING_TO_REVOKE"}],
         "rank": 1, "representative": True},
        agent_entry("agent-leak.json", naive),
        agent_entry("agent-guarded.json", guarded),
        agent_entry("agent-invalid.json", invalid),
    ],
}  # fmt: skip
(here / "index.v11.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
print("written")
