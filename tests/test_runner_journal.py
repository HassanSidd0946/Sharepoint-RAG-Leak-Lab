"""The append-only journal: survives a crash, never overwrites, refuses damage it cannot explain."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from leaklab.runner.journal import Journal, load_events, new_journal_path


def test_events_are_appended_in_order_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "run.jsonl"
    with Journal(path) as j:
        j.append({"type": "run_started", "n": 1})
        j.append({"type": "obs", "cycle": 0, "served": None})
    assert [e["type"] for e in load_events(path)] == ["run_started", "obs"]
    with Journal(path) as j:  # appending to an existing journal keeps what is there
        j.append({"type": "run_finished"})
    assert len(load_events(path)) == 3


def test_every_line_is_on_disk_before_the_next_is_written(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    journal = Journal(path)
    journal.append({"type": "obs", "cycle": 0})
    assert path.read_text(encoding="utf-8").count("\n") == 1  # readable without close()
    journal.close()


def test_a_line_cut_off_by_a_crash_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"type": "a"}) + "\n" + '{"type": "obs", "cyc', encoding="utf-8")
    assert [e["type"] for e in load_events(path)] == ["a"]


def test_damage_in_the_middle_is_an_error_not_silently_skipped(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text('{"type": "a"}\nnot json\n{"type": "b"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        load_events(path)


def test_a_line_that_is_not_an_event_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text('{"type": "a"}\n[1, 2]\n{"type": "b"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a journal event"):
        load_events(path)


def test_a_new_journal_never_takes_an_existing_name(tmp_path: Path) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    first = new_journal_path(tmp_path, now=now)
    assert first.name == "run_20261016T120000Z.jsonl"
    first.write_text("x", encoding="utf-8")
    second = new_journal_path(tmp_path, now=now)
    assert second.name == "run_20261016T120000Z_2.jsonl"
    second.write_text("x", encoding="utf-8")
    assert new_journal_path(tmp_path, "_r2", now=now).name == "run_20261016T120000Z_r2.jsonl"
    assert new_journal_path(tmp_path, now=now).name == "run_20261016T120000Z_3.jsonl"
