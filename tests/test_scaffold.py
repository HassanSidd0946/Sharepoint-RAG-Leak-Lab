"""Smoke test: every scaffolded module/package imports. No logic is tested yet."""

import importlib

import pytest

MODULES = [
    "config",
    "auth",
    "graph_client",
    "sp_rest",
    "spec",
    "generator",
    "oracle",
    "setup_a",
    "setup_b",
    "agent",
    "runner",
    "report",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    module = importlib.import_module(f"leaklab.{name}")
    assert module.__doc__
