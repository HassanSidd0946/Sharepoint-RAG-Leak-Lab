"""A run_fn for the benchmark that needs no tenant: the real Engine over the fake tenant, one fake
system per system name, writing real journals into the benchmark folder (like runner.run.run_once).

Time is the fake clock, so a 10-minute baseline or a 60-minute cap costs milliseconds.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leaklab.runner import Engine, Journal, RunConfig, build_report, load_events
from leaklab.runner.journal import new_journal_path
from leaklab.runner.metrics import RunReport
from runner_fakes import SPEC, FakeSystem, make_world


class BFakeSystem(FakeSystem):
    """Setup B-like: also reports poller lag and a health change, as the real adapter does."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._lag_n = 0

    def drain_events(self) -> list[dict[str, object]]:
        self._lag_n += 1
        out: list[dict[str, object]] = list(super().drain_events())
        if self._lag_n == 1:
            out.append({"kind": "system_health", "status": "ok", "reasons": []})
            for k in (3, 5):
                out.append(
                    {
                        "kind": "postfilter_recall", "persona": "p-ceo", "query": "*", "k": k,
                        "fetch_limit": 5 * k, "fetched": 5 * k, "allowed_in_window": k - 1,
                        "returned": k - 1, "allowed_total": k, "shortfall": 1,
                        "lost_vs_unlimited": 1,
                    }
                )  # fmt: skip
        if self._lag_n % 6 == 0:
            out.append(
                {"kind": "poller_lag", "membership_age_s": 4.0, "acl_age_s": 2.0 + self._lag_n % 5}
            )
        return out


class FakeRuns:
    """Callable with run_once's keywords; records every call."""

    def __init__(
        self,
        system_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        crash_runs: set[str] | None = None,
    ) -> None:
        self.system_kwargs = {k: dict(v) for k, v in (system_kwargs or {}).items()}
        self.calls: list[dict[str, Any]] = []
        self.crash_runs = crash_runs or set()

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
    ) -> tuple[Path, RunReport]:
        self.calls.append(
            {
                "fresh": fresh,
                "resume": resume,
                "suffix": suffix,
                "scenarios": list(scenario_ids),
                "config": config,
            }
        )
        if resume is not None:  # only the bookkeeping is faked: close the interrupted journal
            with Journal(resume, fsync=False) as journal:
                journal.append({"type": "run_finished", "t": 0.0})
            return resume, build_report(SPEC, load_events(resume))
        _, env, clock = make_world()
        holder: dict[str, Engine] = {}
        systems = []
        for name in system_names:
            cls = BFakeSystem if name == "setup_b" else FakeSystem
            systems.append(
                cls(
                    SPEC,
                    clock,
                    lambda: holder["engine"].applied,
                    name=name,
                    **self.system_kwargs.get(name, {}),
                )
            )
        path = new_journal_path(out_dir, suffix)
        with Journal(path, fsync=False) as journal:
            engine = holder["engine"] = Engine(
                env=env, spec=SPEC, systems=systems, config=config, journal=journal
            )
            journal.append(
                {
                    "type": "run_started", "run_id": path.stem, "t": clock.time(),
                    "scenarios": scenario_ids, "systems": system_names,
                    "config": config.as_dict(), "spec_sha256": "x", "tenant": {},
                }
            )  # fmt: skip
            if suffix.lstrip("_") in self.crash_runs:
                raise KeyboardInterrupt("simulated crash")
            engine.run(scenario_ids)
            journal.append({"type": "run_finished", "t": clock.time()})
        return path, build_report(SPEC, load_events(path))
