"""Run the planned runs one after the other, resumably. The state lives in `manifest.json`.

Folder layout (default runs/benchmark/):
  plan.json       the plan this folder belongs to (written by `plan` and by `run`)
  manifest.json   one entry per planned run: pending | running | done | failed, and its journal
  run_*.jsonl     the runner's journals (one per run), with their .report.json next to them
  report.json / report.html   written by `report`

Every run starts from a FRESH tenant (teardown, generate, settle: run_once(fresh=True)): replicates
are only independent if each starts from the spec. A run that was interrupted is CONTINUED from its
journal (run_once(resume=...)); if the runner refuses (a mutation whose outcome is unknown needs a
human), the run is marked failed with the reason and the benchmark stops: `run --redo N` then
abandons that journal (kept, renamed *.abandoned) and starts run N again on a fresh tenant.

The runner is injected (`run_fn`) so this module is tested without a tenant.
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from leaklab.benchmark.matrix import BenchmarkPlan, PlannedRun
from leaklab.runner import RunConfig, RunnerError, load_events
from leaklab.runner.metrics import RunReport

Status = Literal["pending", "running", "done", "failed"]
MANIFEST = "manifest.json"
PLAN = "plan.json"


class RunFn(Protocol):
    def __call__(
        self,
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
    ) -> tuple[Path, RunReport]: ...


class ManifestRun(BaseModel):
    index: int
    name: str
    status: Status = "pending"
    journal: str | None = None  # file name, relative to the benchmark folder
    started_utc: str | None = None
    finished_utc: str | None = None
    error: str | None = None
    abandoned: list[str] = []


class Manifest(BaseModel):
    plan_sha256: str
    runs: list[ManifestRun]


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_manifest(folder: Path) -> Manifest | None:
    path = folder / MANIFEST
    return Manifest.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def save_manifest(folder: Path, manifest: Manifest) -> None:
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest.model_dump(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(folder / MANIFEST)  # a crash never leaves half a manifest


def write_plan(folder: Path, plan: BenchmarkPlan) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / PLAN).write_text(json.dumps(plan.model_dump(), indent=2) + "\n", encoding="utf-8")


def load_plan(folder: Path) -> BenchmarkPlan:
    path = folder / PLAN
    if not path.exists():
        raise RunnerError(f"{path} does not exist: run `benchmark plan` first")
    return BenchmarkPlan.model_validate_json(path.read_text(encoding="utf-8"))


def init_manifest(folder: Path, plan: BenchmarkPlan) -> Manifest:
    """The folder's manifest, created if missing; refused if it was made for another plan."""
    existing = load_manifest(folder)
    if existing is None:
        manifest = Manifest(
            plan_sha256=plan.sha256(),
            runs=[ManifestRun(index=r.index, name=r.name) for r in plan.runs],
        )
        save_manifest(folder, manifest)
        return manifest
    if existing.plan_sha256 != plan.sha256():
        raise RunnerError(
            f"{folder / MANIFEST} belongs to a different plan; use a new --dir or delete the folder"
        )
    return existing


def journal_finished(path: Path) -> bool:
    return any(e["type"] == "run_finished" for e in load_events(path))


def abandon(folder: Path, entry: ManifestRun) -> None:
    """Keep the journal of a run that is being redone (renamed *.abandoned), reset the entry."""
    if entry.journal:
        old = folder / entry.journal
        if old.exists():
            old.rename(old.with_suffix(old.suffix + ".abandoned"))
        entry.abandoned.append(entry.journal + ".abandoned")
    entry.status, entry.journal = "pending", None
    entry.started_utc = entry.finished_utc = entry.error = None


def run_benchmark(
    plan: BenchmarkPlan,
    folder: Path,
    *,
    spec_path: Path,
    run_fn: RunFn,
    only: Sequence[int] | None = None,
    redo: Sequence[int] = (),
    sku_part_number: str = "",
    log: Callable[[str], object] = lambda text: None,
) -> Manifest:
    """Run every planned run that is not done (or only `only`); stop at the first failure."""
    folder.mkdir(parents=True, exist_ok=True)
    manifest = init_manifest(folder, plan)
    write_plan(folder, plan)
    by_index = {r.index: r for r in plan.runs}
    entries = {e.index: e for e in manifest.runs}
    for number in redo:
        if number not in entries:
            raise RunnerError(f"--redo {number}: the plan has runs {sorted(entries)}")
        abandon(folder, entries[number])
        save_manifest(folder, manifest)
    if only is not None and (missing := [n for n in only if n not in entries]):
        raise RunnerError(f"--only {missing}: the plan has runs {sorted(entries)}")
    for number in sorted(entries):
        if only is not None and number not in only:
            continue
        entry = entries[number]
        if entry.status == "done":
            log(f"run {number}: done already ({entry.journal})")
            continue
        _run_one(
            plan, by_index[number], entry, manifest, folder, spec_path, run_fn, sku_part_number, log
        )
    return manifest


def _run_one(
    plan: BenchmarkPlan,
    planned: PlannedRun,
    entry: ManifestRun,
    manifest: Manifest,
    folder: Path,
    spec_path: Path,
    run_fn: RunFn,
    sku_part_number: str,
    log: Callable[[str], object],
) -> None:
    resume = folder / entry.journal if entry.journal and (folder / entry.journal).exists() else None
    if resume is not None and journal_finished(resume):  # finished, but the manifest never heard
        entry.status, entry.finished_utc = "done", entry.finished_utc or now_utc()
        save_manifest(folder, manifest)
        log(f"run {entry.index}: its journal is finished; marked done")
        return
    entry.status, entry.error = "running", None
    entry.started_utc = entry.started_utc or now_utc()
    save_manifest(folder, manifest)
    log(
        f"run {entry.index}: {planned.scenarios} on {plan.systems}, "
        f"baseline {planned.baseline_minutes} min"
        + (f" (continuing {resume.name})" if resume else "")
    )
    config = RunConfig(**_config_kwargs(planned.config))
    try:
        path, _ = run_fn(
            spec_path=spec_path,
            scenario_ids=list(planned.scenarios),
            system_names=list(plan.systems),
            config=config,
            out_dir=folder,
            fresh=resume is None,
            resume=resume,
            sku_part_number=sku_part_number,
            suffix=f"_{planned.name}",
        )
    except BaseException as exc:
        entry.status, entry.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
        _remember_journal(entry, folder, planned)
        save_manifest(folder, manifest)
        raise
    entry.status, entry.journal, entry.finished_utc = "done", path.name, now_utc()
    save_manifest(folder, manifest)


def _remember_journal(entry: ManifestRun, folder: Path, planned: PlannedRun) -> None:
    """After a failure keep the newest journal of this run, so `run` can continue it."""
    if entry.journal:
        return
    found = [
        *folder.glob(f"run_*_{planned.name}.jsonl"),
        *folder.glob(f"run_*_{planned.name}_[0-9]*.jsonl"),
    ]
    candidates = sorted(found, key=lambda p: p.stat().st_mtime)
    if candidates:
        entry.journal = candidates[-1].name


def _config_kwargs(config: dict[str, float]) -> dict[str, Any]:
    ints = {"baseline_k", "n_consecutive", "workers"}
    return {k: (int(v) if k in ints else v) for k, v in config.items()}
