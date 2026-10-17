"""`python -m leaklab.setup_b.api serve`: the pollers and the API in one process.

Needs docker compose Postgres (LEAKLAB_PG_DSN), the search endpoint, the driver app certificate and
(for /explain and /metrics) LEAKLAB_API_OPERATOR_KEY. Binds 127.0.0.1 only.
"""

import threading

import typer
from rich.console import Console

from leaklab.config import get_settings
from leaklab.setup_b.api.app import create_app
from leaklab.setup_b.api.server import LocalServer, build_service
from leaklab.setup_b.state.run import open_stores, start_pollers

console = Console()
app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("serve")
def serve(
    port: int = typer.Option(8080, help="Port on 127.0.0.1."),
    interval_s: float = typer.Option(5.0, help="Seconds between polls."),
    verify_every_s: float = typer.Option(60.0, help="Full ACL verification this often."),
    debug_stats: bool = typer.Option(False, help="Honor `debug` in /query (test/benchmark only)."),
    duration_s: float = typer.Option(0.0, help="Stop after this many seconds (0 = until Ctrl+C)."),
) -> None:
    """First sync, then serve while the pollers keep the state current."""
    stop = threading.Event()
    with open_stores() as stores:
        console.print("first sync ...")
        console.print(f"membership: {stores.membership.poll_once()}")
        console.print(f"acl:        {stores.acl.poll_once()}")
        threads = start_pollers(stores, stop, interval_s, verify_every_s, console.print)
        service = build_service(stores)
        api = create_app(
            service,
            operator_key=get_settings().api_operator_key.get_secret_value(),
            allow_debug=debug_stats,
        )
        try:
            with LocalServer(api, port) as server:
                console.print(f"serving on {server.url}  (Ctrl+C to stop)")
                stop.wait(duration_s or None)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=60)


if __name__ == "__main__":
    app()
