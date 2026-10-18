"""Run the pollers against the real tenant and Postgres: `python -m leaklab.setup_b.state ...`.

  up       create the schema and run both pollers every 5 s (membership: group delta; ACL: drive
           delta + REST) until interrupted, with a full ACL verification every `--verify-every` s
  once     one membership poll and one ACL poll, print the result (a smoke test)
  status   freshness of both pollers and the latest events

The Postgres DSN comes from LEAKLAB_PG_DSN (see .env.example and docker-compose.yml). The tenant
guard runs first (CLAUDE.md rule 4): this only READS the tenant, but it reads the test tenant only.
"""

import threading
import time
from collections.abc import Callable, Iterator
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
from leaklab.setup_b.state.readiness import Readiness
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
    readiness: Readiness


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
        readiness = Readiness()
        yield Stores(
            membership=MembershipStore(repo, GraphGroupDelta(gc), directory, time.time, readiness),
            acl=AclStore(
                repo,
                GraphDriveDelta(gc, drive_id),
                RestSharePoint(sp),
                time.time,
                container=CONTAINER,
                directory=directory,
                readiness=readiness,
            ),
            reader=StateReader(repo, time.time),
            repo=repo,
            readiness=readiness,
        )
    repo.close()


@app.command("once")
def cmd_once(
    verify: bool = typer.Option(False, help="Also run the full ACL verification."),
) -> None:
    """One poll of each store (and optionally the verification), with how long each took."""
    with open_stores() as s:
        for label, call in (("membership", s.membership.poll_once), ("acl", s.acl.poll_once)):
            started = time.time()
            result = call()
            console.print(f"{label:11} {result}  ({time.time() - started:.1f} s)")
        if verify:
            started = time.time()
            console.print(f"verify_all  {s.acl.verify_all()}  ({time.time() - started:.1f} s)")
        console.print(s.reader.freshness())


@app.command("up")
def cmd_up(
    interval_s: float = typer.Option(5.0, help="Seconds between polls."),
    verify_every_s: float = typer.Option(60.0, help="Full ACL verification this often."),
    duration_s: float = typer.Option(0.0, help="Stop after this many seconds (0 = until Ctrl+C)."),
) -> None:
    """Keep the state current until interrupted (three independent loops: see start_pollers)."""
    stop = threading.Event()
    with open_stores() as s:
        threads = start_pollers(s, stop, interval_s, verify_every_s, console.print)
        try:
            stop.wait(duration_s or None)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=60)


def start_pollers(
    s: Stores,
    stop: threading.Event,
    interval_s: float,
    verify_every_s: float,
    report: Callable[[str], object] = lambda text: None,
) -> list[threading.Thread]:
    """Start the three loops (daemon threads) and return them; `stop` ends them.

    Three independent loops: the membership poll, the ACL delta poll and the ACL verification. The
    verification takes about 30 s (tens of REST calls); in one shared loop it would hold the other
    two back past the reader's freshness limit and everything would be denied for those seconds.
    """

    def loop(name: str, every_s: float, step: Callable[[], object], quiet_ok: bool) -> None:
        while not stop.is_set():
            started = time.time()
            try:
                result = step()
            except Exception as exc:  # a loop must survive anything
                report(f"[red]{name}: {type(exc).__name__}: {exc}[/red]")
            else:
                changed = bool(getattr(result, "changes", 0) or getattr(result, "events", 0))
                if changed or not getattr(result, "ok", True) or not quiet_ok:
                    report(f"{name}: {result}")
            stop.wait(max(0.0, started + every_s - time.time()))

    threads = [
        threading.Thread(
            target=loop, args=("membership", interval_s, s.membership.poll_once, True)
        ),
        threading.Thread(target=loop, args=("acl", interval_s, s.acl.poll_once, True)),
        threading.Thread(target=loop, args=("verify", verify_every_s, s.acl.verify_all, False)),
    ]
    for t in threads:
        t.daemon = True
        t.start()
    return threads


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
