"""Pytest config: --live, and the PostgreSQL fixtures of the Setup B state tests.

Tests marked @pytest.mark.live are skipped without --live. `pg_dsn` gives a PostgreSQL to test
against (LEAKLAB_TEST_PG_DSN, e.g. from docker compose, else an embedded server) and `make_repo`
yields factories of empty StateRepositories: the in-memory one and a Postgres one, so a test that
uses it runs against both.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from leaklab.setup_b.state.pg import PgRepository
from leaklab.setup_b.state.repository import MemoryRepository, StateRepository


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


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A PostgreSQL DSN: LEAKLAB_TEST_PG_DSN if set, else an embedded server (pgserver) started in a
    temp directory. Skips the test if neither is available."""
    configured = os.environ.get("LEAKLAB_TEST_PG_DSN", "").strip()
    if configured:
        yield configured
        return
    try:
        import pgserver

        server = pgserver.get_server(Path(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    except Exception as exc:
        pytest.skip(f"no PostgreSQL available (set LEAKLAB_TEST_PG_DSN or install pgserver): {exc}")
    try:
        yield str(server.get_uri())
    finally:
        server.cleanup()


@pytest.fixture(params=["memory", "pg"])
def make_repo(request: pytest.FixtureRequest) -> Iterator[Callable[[], StateRepository]]:
    """A factory of empty StateRepositories: the in-memory one and a Postgres one (own schema)."""
    made: list[PgRepository] = []

    def factory() -> StateRepository:
        if request.param == "memory":
            return MemoryRepository()
        repo = PgRepository(request.getfixturevalue("pg_dsn"), f"t_{uuid.uuid4().hex[:12]}")
        made.append(repo)
        return repo

    yield factory
    for repo in made:
        repo.drop_schema()
