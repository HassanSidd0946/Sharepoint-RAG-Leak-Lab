"""The HTTP layer of Setup B: FastAPI routes over QueryService. Nothing here decides anything.

  POST /query    {persona_token, query, k?}  -> results the user may read, never more than k
  GET  /health   ok | degraded (a poller's last success is older than the limit) | down
  GET  /explain  ?user=&doc=   why the state allows or denies (operator key required)
  GET  /metrics  poller lag, decisions, recall numbers (operator key required)

Errors never carry a stack trace, a token or document content: 401 {"error": <short code>} for a
token that is not acceptable, 422 for a malformed request, 503 when the index, the state or the
decision log cannot be used (an error is never an empty-but-successful answer and never an allow),
500 for anything unexpected. /explain and /metrics are DISABLED (403) unless an operator key is
configured, and compare it in constant time. `debug` stats (counts of what the filter removed) are
only honored when the server was started with allow_debug, because they reveal that documents exist.
"""

import hmac
import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from leaklab.setup_b.api.search import IndexUnavailable
from leaklab.setup_b.api.service import MAX_K, MAX_QUERY_CHARS, LogUnavailable, QueryService
from leaklab.setup_b.api.tokens import InvalidToken

log = logging.getLogger(__name__)
KNOWN_PATHS = frozenset({"/query", "/health", "/explain", "/metrics"})


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_token: str = Field(min_length=1, repr=False)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    k: int = Field(default=5, ge=1, le=MAX_K)
    debug: bool = False
    measure_recall: bool = False


def create_app(
    service: QueryService, *, operator_key: str = "", allow_debug: bool = False
) -> FastAPI:
    app = FastAPI(title="Leak Lab Setup B", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def count_requests(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        path = request.url.path if request.url.path in KNOWN_PATHS else "other"  # bounded labels
        service.count(f"http:{path}:{response.status_code}")
        return response

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("unexpected error: %s", type(exc).__name__)  # the type only: no token, no content
        return JSONResponse({"error": "internal"}, status_code=500)

    def operator(key: str | None) -> None:
        if not operator_key:
            raise HTTPException(403, "disabled: no operator key is configured")
        if key is None or not hmac.compare_digest(key.encode(), operator_key.encode()):
            raise HTTPException(401, "operator key required")

    @app.post("/query")
    def query(req: QueryRequest) -> JSONResponse:
        try:
            outcome = service.query(
                req.persona_token,
                req.query,
                req.k,
                measure_recall=req.measure_recall and req.debug and allow_debug,
            )
        except InvalidToken as exc:
            service.count(f"invalid_token:{exc.code}")
            return JSONResponse({"error": exc.code}, status_code=401)
        except ValueError:
            return JSONResponse({"error": "bad_request"}, status_code=422)
        except IndexUnavailable:
            service.count("index_errors")
            return JSONResponse({"error": "index_unavailable"}, status_code=503)
        except LogUnavailable:
            return JSONResponse({"error": "decision_log_unavailable"}, status_code=503)
        body: dict[str, Any] = {
            "request_id": outcome.request_id,
            "degraded": outcome.stats.degraded,
            "results": [
                {"doc_id": r.doc_id, "name": r.name, "content": r.content} for r in outcome.results
            ],
        }
        if req.debug and allow_debug:
            s = outcome.stats
            body["stats"] = {
                "k": s.k,
                "fetch_limit": s.fetch_limit,
                "fetched": s.fetched,
                "unmapped": s.unmapped,
                "allowed": s.allowed,
                "returned": s.returned,
                "shortfall": s.shortfall,
                "denied": s.denied,
                "allowed_total": s.allowed_total,
            }
        return JSONResponse(body)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            return JSONResponse(service.health())
        except Exception as exc:  # the state store itself is unreachable
            log.error("health check failed: %s", type(exc).__name__)
            return JSONResponse({"status": "down"}, status_code=503)

    @app.get("/explain")
    def explain(
        user: str, doc: str, x_operator_key: str | None = Header(default=None)
    ) -> JSONResponse:
        operator(x_operator_key)
        try:
            return JSONResponse(service.explain(user, doc))
        except LogUnavailable:
            return JSONResponse({"error": "decision_log_unavailable"}, status_code=503)

    @app.get("/metrics")
    def metrics(x_operator_key: str | None = Header(default=None)) -> JSONResponse:
        operator(x_operator_key)
        return JSONResponse(service.metrics())

    return app
