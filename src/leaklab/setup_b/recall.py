"""Measure the recall cost of post-filtering on the live index: `python -m leaklab.setup_b.recall`.

For every persona and k in (3, 5, 10) a broad query ("*", every document matches) is asked of two
services over the SAME state and the SAME index: the adaptive one (pages until k results are allowed
or the page cap is reached) and the old fixed window (one page of 5k hits). Each answer is compared
with the most the persona could have been given: the readable documents among ALL matches, capped at
k. Read-only: the tenant is not changed. The state is built in a scratch Postgres schema (first sync
about 45 s, then it waits for /ready conditions) and dropped afterwards. The result goes to
probes/out/recall_<UTC>.json, with the number of pages fetched for every query.
"""

import json
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel
from rich.console import Console

from leaklab.auth import get_persona_token
from leaklab.generator.cli import open_env
from leaklab.setup_a.client import SEARCH_SCOPE
from leaklab.setup_b.api.server import build_service
from leaklab.setup_b.state.run import open_stores, start_pollers

KS = (3, 5, 10)
QUERY = "*"
ROOT = Path(__file__).resolve().parents[3]
console = Console()
app = typer.Typer(add_completion=False)


class Sample(BaseModel):
    mode: str
    persona: str
    k: int
    returned: int
    possible: int  # min(k, readable documents among all matches)
    pages: int
    fetched: int
    capped: bool
    exhausted: bool

    @property
    def lost(self) -> int:
        return self.possible - self.returned


def summarize(samples: Sequence[Sample]) -> dict[str, dict[str, dict[str, float | int]]]:
    """mode -> k -> totals: samples, returned, possible, lost, short (returned < possible),
    pages (sum, max) and capped queries."""
    out: dict[str, dict[str, dict[str, float | int]]] = {}
    for s in samples:
        row = out.setdefault(s.mode, {}).setdefault(
            str(s.k),
            {
                "samples": 0,
                "returned": 0,
                "possible": 0,
                "lost": 0,
                "short": 0,
                "pages_sum": 0,
                "pages_max": 0,
                "capped": 0,
            },
        )
        row["samples"] += 1
        row["returned"] += s.returned
        row["possible"] += s.possible
        row["lost"] += s.lost
        row["short"] += 1 if s.lost > 0 else 0
        row["pages_sum"] += s.pages
        row["pages_max"] = max(int(row["pages_max"]), s.pages)
        row["capped"] += 1 if s.capped else 0
    return out


@app.command()
def main(
    out_dir: Path = typer.Option(ROOT / "probes" / "out", help="Where the result is saved."),
) -> None:
    """Run the measurement against the live tenant (read-only)."""
    with open_env(ROOT / "tenant_spec.yaml") as env:
        personas = {u.id: u.upn for u in env.plan.users if u.persona}
        generation = env.generation
    schema = f"leaklab_recall_{int(time.time())}"
    samples: list[Sample] = []
    with open_stores(schema=schema) as stores:
        try:
            for label, poll in (
                ("membership", stores.membership.poll_once),
                ("acl", stores.acl.poll_once),
            ):
                result = poll()
                if not result.ok:
                    raise RuntimeError(f"first {label} sync failed: {result.error}")
            stop = threading.Event()
            threads = start_pollers(stores, stop, 5.0, 60.0)
            deadline = time.time() + 300
            while not stores.readiness.state().ready and time.time() < deadline:
                time.sleep(2)
            console.print(f"state ready: {stores.readiness.state().ready}")
            services = {
                "adaptive": build_service(stores, log_schema="leaklab_api_recall"),
                "fixed_5k": build_service(
                    stores, log_schema="leaklab_api_recall", max_pages=1, page_size=lambda k: 5 * k
                ),
            }
            tokens = {
                p: get_persona_token(upn, [SEARCH_SCOPE], allow_device_code=False, use_cache=False)
                for p, upn in personas.items()
            }
            for mode, service in services.items():
                for persona in personas:
                    for k in KS:
                        out = service.query(tokens[persona], QUERY, k, measure_recall=True)
                        st = out.stats
                        samples.append(
                            Sample(
                                mode=mode,
                                persona=persona,
                                k=k,
                                returned=st.returned,
                                possible=min(k, st.allowed_total or 0),
                                pages=st.pages,
                                fetched=st.fetched,
                                capped=st.capped,
                                exhausted=st.exhausted,
                            )
                        )
            stop.set()
            for t in threads:
                t.join(timeout=60)
            for service in services.values():
                service.close()
        finally:
            stores.repo.drop_schema()
    data: dict[str, Any] = {
        "generation": generation,
        "query": QUERY,
        "personas": list(personas),
        "summary": summarize(samples),
        "samples": [s.model_dump() for s in samples],
        "when_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"recall_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for mode, by_k in data["summary"].items():
        for k, row in by_k.items():
            console.print(
                f"{mode:9} k={k:>2}: returned {row['returned']} of {row['possible']} possible "
                f"(lost {row['lost']}, queries short {row['short']} of {row['samples']}, "
                f"pages mean {row['pages_sum'] / row['samples']:.1f} max {row['pages_max']}, "
                f"capped {row['capped']})"
            )
    console.print(f"saved {path}")


if __name__ == "__main__":
    app()
