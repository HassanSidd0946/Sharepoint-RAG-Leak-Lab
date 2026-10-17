"""run_once(): one complete run against the real tenant, from "is the tenant ready" to the report.

  1. (optional --fresh) tear the generated tenant down and wait, so the run starts from the spec;
  2. ensure the tenant is generated and verified (verify; if not OK, generate, then verify);
  3. build the systems (persona sign-in happens in Engine, before any mutation);
  4. run the engine into a new timestamped journal (or continue one with `resume`);
  5. write the report next to the journal; optionally tear down.

The tenant guard (assert_test_tenant on both app tokens) runs inside open_env(), before anything
touches the tenant. A run MUTATES the tenant: run `--fresh` (or teardown + generate) before the
next one. Resuming skips step 2, because a half-run tenant legitimately differs from the spec.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from leaklab.generator import Env, generate, teardown, verify
from leaklab.generator.cli import open_env
from leaklab.runner.engine import Engine, restore_env
from leaklab.runner.journal import Journal, load_events, new_journal_path
from leaklab.runner.metrics import RunReport, build_report
from leaklab.runner.model import RunConfig, RunnerError
from leaklab.runner.tenant import SystemContext, build_systems, canary_locations
from leaklab.spec import load_spec

SETTLE_AFTER_TEARDOWN_S = 60.0  # deletes lag behind lookups (docs/FINDINGS.md)


def spec_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_ready(env: Env) -> dict[str, Any]:
    """Verify the tenant against the spec; if that fails, generate and verify again."""
    first = verify(env)
    if first.ok:
        return {"generated": False, "verify_ok": True}
    env.event("verify_failed_generating", issues=[i.what for i in first.issues][:10])
    report = generate(env)
    second = verify(env)
    if not second.ok:
        problems = [f"{i.what} {i.expected} {i.actual}".strip() for i in second.issues]
        raise RunnerError(
            "the tenant does not match the spec even after generate; run teardown and generate, "
            f"or use --fresh. Problems: {problems[:8]}"
        )
    return {"generated": True, "verify_ok": True, "counts": report.counts}


def write_report(report: RunReport, journal_path: Path) -> Path:
    path = journal_path.with_suffix(".report.json")
    n = 1
    while path.exists():
        n += 1
        path = journal_path.with_name(f"{journal_path.stem}.report_{n}.json")
    path.write_text(json.dumps(report.model_dump(), indent=2) + "\n", encoding="utf-8")
    return path


def _check_resume(
    events: list[dict[str, Any]], scenarios: list[str], systems: list[str], sha: str
) -> None:
    header = next((e for e in events if e["type"] == "run_started"), None)
    if header is None:
        raise RunnerError("the journal has no run_started event; it cannot be resumed")
    if list(header["scenarios"]) != scenarios or list(header["systems"]) != systems:
        raise RunnerError(
            f"--resume needs the same scenarios and systems as the journal "
            f"({header['scenarios']}, {header['systems']})"
        )
    if header.get("spec_sha256") != sha:
        raise RunnerError("tenant_spec.yaml changed since this run started; it cannot be resumed")
    if any(e["type"] == "run_finished" for e in events):
        raise RunnerError("this run already finished; there is nothing to resume")


def run_once(
    *,
    spec_path: Path,
    scenario_ids: list[str],
    system_names: list[str],
    config: RunConfig,
    out_dir: Path,
    fresh: bool = False,
    resume: Path | None = None,
    sku_part_number: str = "",
    teardown_after: bool = False,
    suffix: str = "",
) -> tuple[Path, RunReport]:
    spec = load_spec(spec_path)
    sha = spec_sha256(spec_path)
    if fresh and resume is None:
        with open_env(spec_path, sku_part_number) as env0:
            teardown(env0)
            env0.sleep(SETTLE_AFTER_TEARDOWN_S)
    with open_env(spec_path, sku_part_number) as env:
        events = load_events(resume) if resume else []
        tenant: dict[str, Any]
        if resume:
            _check_resume(events, scenario_ids, system_names, sha)
            applied = restore_env(env, events)
            journal_path = resume
            tenant = {"resumed": True}
        else:
            applied = []
            journal_path = new_journal_path(out_dir, suffix)
            tenant = ensure_ready(env)
        journal = Journal(journal_path)
        systems = []
        try:
            if not resume:
                journal.append(
                    {
                        "type": "run_started",
                        "run_id": journal_path.stem,
                        "t": env.clock(),
                        "spec_path": str(spec_path),
                        "spec_sha256": sha,
                        "scenarios": scenario_ids,
                        "systems": system_names,
                        "config": config.as_dict(),
                        "tenant": tenant,
                    }
                )
            ctx = SystemContext(env, list(canary_locations(env)), env.clock)
            systems = build_systems(system_names, ctx)
            engine = Engine(
                env=env, spec=spec, systems=systems, config=config, journal=journal, applied=applied
            )
            engine.run(scenario_ids, replay=events)
            journal.append({"type": "run_finished", "t": env.clock()})
        finally:
            for system in systems:
                system.close()
            journal.close()
        report = build_report(spec, load_events(journal_path))
        write_report(report, journal_path)
        if teardown_after:
            teardown(env)
    return journal_path, report
