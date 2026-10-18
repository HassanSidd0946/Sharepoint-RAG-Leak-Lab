"""The pure parts of probes/search_acl_probe.py (no Azure, no network): reading stored ACL values,
extracting canaries, comparing with the oracle, and keeping errors apart from empty answers."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PROBE = Path(__file__).resolve().parents[1] / "probes" / "search_acl_probe.py"


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("search_acl_probe", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NAMES = {"u-1": "user:p-plateng", "g-1": "group:grp-s1-entra"}


def test_stored_acl_values_are_labelled_and_unknowns_are_never_hidden(probe: ModuleType) -> None:
    labelled = probe.label_acl(["u-1", "g-1", "spg:7", "zzz"], NAMES)
    assert labelled == [
        "SHAREPOINT-GROUP-ID:spg:7",
        "UNMAPPED:zzz",
        "group:grp-s1-entra",
        "user:p-plateng",
    ]
    assert probe.label_acl(None, NAMES) == []


def test_canaries_are_found_in_content_and_nowhere_else(probe: ModuleType) -> None:
    text = "x CANARY-S1D-01-b67fb8 y CANARY-SPC-0a1b2c z CANARY-S1D-01-b67fb8 canary-low"
    assert probe.canaries_in(text) == ["CANARY-S1D-01-b67fb8", "CANARY-SPC-0a1b2c"]
    assert probe.canaries_in(None) == []


def test_acl_is_grouped_by_the_folder_code_of_the_canary(probe: ModuleType) -> None:
    docs = [
        {"content": "CANARY-S1D-01-b67fb8", "UserIds": ["u-1"], "GroupIds": []},
        {"content": "CANARY-S1D-02-7600e0", "UserIds": ["u-1"], "GroupIds": []},
        {"content": "CANARY-S1E-01-b24592", "UserIds": [], "GroupIds": ["g-1"]},
        {"content": "no canary", "UserIds": None, "GroupIds": None},
    ]
    out = probe.acl_by_folder(docs, NAMES)
    assert out["S1D"] == [{"UserIds": ["user:p-plateng"], "GroupIds": []}]  # identical docs merge
    assert out["S1E"] == [{"UserIds": [], "GroupIds": ["group:grp-s1-entra"]}]
    assert out["?"] == [{"UserIds": [], "GroupIds": []}]


def test_the_comparison_names_every_leak_and_every_lockout(probe: ModuleType) -> None:
    expected = {"p-plateng": {"A", "B"}}
    assert probe.compare_with_oracle({"A", "B"}, "p-plateng", expected)["ok"] is True
    result = probe.compare_with_oracle({"A", "C"}, "p-plateng", expected)
    assert result["leaks"] == ["C"] and result["lockouts"] == ["B"] and result["ok"] is False
    assert probe.compare_with_oracle({"A"}, "p-nobody", expected)["leaks"] == ["A"]  # deny unknown


def test_a_failed_query_is_not_an_empty_result(probe: ModuleType) -> None:
    ok = {"value": [{"content": "CANARY-GEN-01-aaaaaa"}]}
    assert probe.returned_canaries(200, ok) == {"CANARY-GEN-01-aaaaaa"}
    assert probe.returned_canaries(200, {"value": []}) == set()
    for status, body in ((401, {"error": {}}), (500, "boom"), (0, "transport:ConnectError")):
        assert probe.returned_canaries(status, body) is None  # unknown, never "nothing served"


def test_the_recorder_never_keeps_the_secret_of_a_logged_connection_string(
    probe: ModuleType,
) -> None:
    text = open(PROBE, encoding="utf-8").read()
    assert '"connectionString": "<redacted>"' in text  # the recorded data source body is redacted
    assert "get_secret_value()" in text and text.count("ApplicationSecret=") == 1


def test_canaries_with_a_generation_are_recognized_too(probe: ModuleType) -> None:
    text = "x CANARY-GTN3HC5-S1D-01-b67fb8 y CANARY-GTN3HC5-SPC-0a1b2c z CANARY-S1D-02-7600e0"
    assert probe.canaries_in(text) == [
        "CANARY-GTN3HC5-S1D-01-b67fb8",
        "CANARY-GTN3HC5-SPC-0a1b2c",
        "CANARY-S1D-02-7600e0",
    ]
