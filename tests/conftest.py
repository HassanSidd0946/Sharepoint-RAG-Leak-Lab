"""Pytest config: adds --live; tests marked @pytest.mark.live are skipped without it."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests marked 'live' (they call real Microsoft/Azure endpoints)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live test: pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
