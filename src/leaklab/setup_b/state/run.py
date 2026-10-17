"""Run the pollers against the real tenant and Postgres: `python -m leaklab.setup_b.state ...`.

  up       create the schema and run both pollers every 5 s (membership: group delta; ACL: drive
           delta + REST) until interrupted, with a full ACL verification every `--verify-every` s
  once     one membership poll and one ACL poll, print the result (a smoke test)
  status   freshness of both pollers and the latest events

The Postgres DSN comes from LEAKLAB_PG_DSN (see .env.example and docker-compose.yml). The tenant
guard runs first (CLAUDE.md rule 4): this only READS the tenant, but it reads the test tenant only.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import typer
from rich.console import Console

from leaklab.config import get_settings
from leaklab.generator.cli import guard_test_tenant
from leaklab.graph_client import GraphClient
from leaklab.setup_b.state.access import StateReader
from leaklab.setup_b.state.acl import AclStore
from leaklab.setup_b.state.directory import DirectoryStore
from leaklab.setup_b.state.membership import MembershipStore
from leaklab.setup_b.state.pg import PgRepository
from leaklab.setup_b.state.sources import (
    GraphDirectory,
    GraphDriveDelta,
    GraphGroupDelta,
    RestSharePoint,
)
from leaklab.sp_rest import SharePointRest

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)
CONTAINER = "ll-lab"  # only the generated content is tracked


@dataclass
class Stores:
    membership: MembershipStore
    acl: AclStore
    reader: StateReader
    repo: PgRepository


@contextmanager
def open_stores(schema: str = "leaklab_state", dsn: str | None = None) -> Iterator[Stores]:
    """Guard the tenant, connect Postgres (LEAKLAB_PG_DSN, or `dsn`) and build the stores."""
    settings = get_settings()
    dsn = dsn or settings.pg_dsn.get_secret_value()
    if not dsn:
        raise typer.BadParameter("LEAKLAB_PG_DSN is not set (see .env.example, docker-compose.yml)")
    guard_test_tenant()
    site_url = settings.site_url.strip()
    repo = PgRepository(dsn, schema)
    with GraphClient() as gc, SharePointRest(site_url) as sp:
        site = gc.get(f"/sites/{settings.sp_host.strip()}:{urlsplit(site_url).path.rstrip('/')}")
        drive_id = str(gc.get(f"/sites/{site['id']}/drive")["id"])
        directory = DirectoryStore(GraphDirectory(gc))
        yield Stores(
            membership=MembershipStore(repo, GraphGroupDelta(gc), directory, time.time),
            acl=AclStore(
                repo,
                GraphDriveDelta(gc, drive_id),
                RestSharePoint(sp),
                time.time,
                container=CONTAINER,
                directory=directory,
            ),
            reader=StateReader(repo, time.time),
            repo=repo,
        )
    repo.close()


@app.command("once")
def cmd_once() -> None:
    """One poll of each store; prints what happened."""
    with open_stores() as s:
        console.print("membership:", s.membership.poll_once())
        console.print("acl:       ", s.acl.poll_once())
        console.print(s.reader.freshness())


@app.command("up")
def cmd_up(
    interval_s: float = typer.Option(5.0, help="Seconds between polls."),
    verify_every_s: float = typer.Option(60.0, help="Full ACL verification this often."),
    max_cycles: int = typer.Option(0, help="Stop after this many cycles (0 = until interrupted)."),
) -> None:
    """Keep the state current until interrupted."""
    with open_stores() as s:
        last_verify = time.time()
        cycle = 0
        while max_cycles == 0 or cycle < max_cycles:
            started = time.time()
            m, a = s.membership.poll_once(), s.acl.poll_once()
            if time.time() - last_verify >= verify_every_s:
                v = s.acl.verify_all()
                last_verify = time.time()
                console.print("verify:", v)
            if m.changes or a.events or not (m.ok and a.ok):
                console.print(f"cycle {cycle}: membership {m} | acl {a}")
            cycle += 1
            time.sleep(max(0.0, started + interval_s - time.time()))


@app.command("status")
def cmd_status(events: int = typer.Option(10, help="How many recent events to show.")) -> None:
    """Freshness of both pollers and the latest events."""
    with open_stores() as s:
        console.print(s.reader.freshness())
        for e in s.repo.events()[-events:]:
            console.print(
                f"{e.source:10} {e.kind:22} {e.subject[:60]:60} "
                f"seen->applied {e.applied_at - e.seen_at:.3f}s"
            )


if __name__ == "__main__":
    app()
