"""The scenario runner: apply a tenant change, read through a system, score how fast it caught up.

  scenarios.py  which (persona, canary) pairs to watch, from the spec and the oracle
  system.py     the SystemUnderTest interface; setup0.py reads plain SharePoint as the persona
  engine.py     baseline -> mutate -> poll, journaling every event (resumable)
  metrics.py    the results of a journal (time to first deny, leak exposure, conformance, lockout)
  run.py, cli.py  one full run, and the command line (`python -m leaklab.runner`)

Leaks are detected by exact canary-string match only. The oracle (leaklab.oracle) says what should
have been served; this package never decides that itself.
"""

from leaklab.runner.engine import Engine, restore_env
from leaklab.runner.journal import Journal, load_events, new_journal_path
from leaklab.runner.metrics import RunReport, ScenarioResult, build_report
from leaklab.runner.model import Observation, RunConfig, RunnerError
from leaklab.runner.scenarios import ScenarioPlan, derive_plan, group_scenarios
from leaklab.runner.system import SystemUnderTest

__all__ = [
    "Engine",
    "Journal",
    "Observation",
    "RunConfig",
    "RunReport",
    "RunnerError",
    "ScenarioPlan",
    "ScenarioResult",
    "SystemUnderTest",
    "build_report",
    "derive_plan",
    "group_scenarios",
    "load_events",
    "new_journal_path",
    "restore_env",
]
