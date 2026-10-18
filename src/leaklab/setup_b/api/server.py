"""Wire the API for real: Postgres state, Graph/SharePoint pollers, the unfiltered index, uvicorn.

  build_service(stores, ...)   QueryService over real parts (index endpoint from settings, the
                               decision log in its own Postgres schema, keys from the tenant's JWKS)
  LocalServer                  the FastAPI app in a uvicorn thread on 127.0.0.1 (port 0 = any free
                               port): how the runner and the tests get a real HTTP endpoint

The service token (driver app, search scope) is created inside HttpIndexPort and goes nowhere else.
The server binds to 127.0.0.1 only: there is no TLS and the operator key travels in a header.
"""

import socket
import threading
import time
from collections.abc import Callable
from contextlib import closing

import uvicorn

from leaklab.config import get_settings
from leaklab.setup_b.api.decisionlog import PgDecisionLog
from leaklab.setup_b.api.search import HttpIndexPort
from leaklab.setup_b.api.service import MAX_PAGES, QueryService
from leaklab.setup_b.api.tokens import JwksKeys, TokenValidator
from leaklab.setup_b.state.run import Stores

LOG_SCHEMA = "leaklab_api"


def search_endpoint() -> str:
    from leaklab.runner.tenant import saved_endpoint

    endpoint = get_settings().search_endpoint.strip() or saved_endpoint()
    if not endpoint:
        raise RuntimeError("no search endpoint: set LEAKLAB_SEARCH_ENDPOINT")
    return endpoint


def build_service(
    stores: Stores,
    *,
    log_schema: str = LOG_SCHEMA,
    max_pages: int = MAX_PAGES,
    page_size: Callable[[int], int] | None = None,
) -> QueryService:
    settings = get_settings()
    return QueryService(
        reader=stores.reader,
        repo=stores.repo,
        index=HttpIndexPort(search_endpoint()),
        log=PgDecisionLog(settings.pg_dsn.get_secret_value(), log_schema),
        validator=TokenValidator(
            tenant_id=settings.tenant_id,
            audience=settings.api_audience,
            keys=JwksKeys(settings.tenant_id.strip()),
        ),
        clock=time.time,
        max_pages=max_pages,
        page_size=page_size,
        readiness=stores.readiness,
    )


def free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalServer:
    """uvicorn in a daemon thread. `url` is ready once __enter__ returns."""

    def __init__(self, app: object, port: int = 0) -> None:
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")  # type: ignore[arg-type]
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "LocalServer":
        self._thread.start()
        deadline = time.time() + 20.0
        while not self._server.started:
            if time.time() > deadline or not self._thread.is_alive():
                raise RuntimeError("the API server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
