"""The run journal: an append-only JSON-lines file with one event per line.

Everything the runner learns is written here as it happens (flushed and fsynced), so a crash loses
at most the line being written, and `report` and `--resume` work from this file alone. A line that
was cut off by a crash (the last line, not valid JSON) is skipped when loading. Files are never
overwritten: a new run gets a timestamped name.

Event types (field "type"):
  run_started       run id, spec path and hash, systems, scenario ids, settings
  group_started     the scenarios polled together, with their pairs (see scenarios.ScenarioPlan)
  group_resumed     a resume continued this group
  obs               one Observation, plus the cycle number it belongs to
  baseline_done     a scenario's baseline ended: BASELINE_OK or BASELINE_UNSTABLE
  mutation_started written BEFORE apply_mutation runs (a crash after it leaves the outcome unknown)
  mutation          the LogEntry of an applied mutation (with confirmed_at)
  mutation_failed   apply_mutation raised; the scenario ends without being scored
  scenario_done     a scenario stopped being polled, and why
  run_finished      the run ended normally
Secrets are never written: observations hold ids and statuses only.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

Event = dict[str, Any]


class Journal:
    """Appends events to one file. Use as a context manager, or call close()."""

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self.path = path
        self._fsync = fsync
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, event: Event) -> None:
        self._fh.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")
        self._fh.flush()
        if self._fsync:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def load_events(path: Path) -> list[Event]:
    """All complete events of a journal. A truncated last line is ignored; a bad line elsewhere
    means the file was edited or damaged and raises ValueError."""
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[Event] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                break  # a crash cut the last line
            raise ValueError(f"{path}: line {index + 1} is not valid JSON") from exc
        if not isinstance(event, dict) or "type" not in event:
            raise ValueError(f"{path}: line {index + 1} is not a journal event")
        events.append(event)
    return events


def new_journal_path(out_dir: Path, suffix: str = "", now: datetime | None = None) -> Path:
    """runs/run_<UTC timestamp>[<suffix>].jsonl, never an existing file."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"run_{stamp}{suffix}.jsonl"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"run_{stamp}{suffix}_{n}.jsonl"
    return path
