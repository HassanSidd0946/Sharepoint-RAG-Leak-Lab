"""LIVE test of the Setup B state stores against the real test tenant. Skipped unless --live.

Real Graph group delta, real Graph drive delta (with the three Prefer headers) and real SharePoint
REST feed the stores, which run on a PostgreSQL (the `pg_dsn` fixture: LEAKLAB_TEST_PG_DSN or an
embedded server). Checks:
  1. after the first sync, what the store says each persona may read EQUALS what the oracle says;
  2. three real changes (a direct grant, a SharePoint-group member, an Entra-group member) show up in
     the reader within a bounded time; the time from the change to the reader's new answer is
     printed (the numbers go into docs/FINDINGS.md);
  3. the safety-net verification finds nothing the delta missed;
  4. after the tenant is restored the store equals the oracle again.
The tenant is only changed through the generator's own mutations, and restored at the end.

Run:  uv run pytest --live tests/test_setup_b_state_live.py -q -s     (about 5-6 minutes)
"""

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from leaklab.generator import generate
from leaklab.generator.cli import open_env
from leaklab.generator.mutate import apply_mutation
from leaklab.oracle import Mutation, MutationType, expected_access
from leaklab.runner.tenant import resolve_items
from leaklab.setup_b.state.run import Stores, open_stores
from leaklab.spec import generate_canaries, load_spec

SPEC_PATH = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"
SPEC = load_spec(SPEC_PATH)
CANARIES = generate_canaries(SPEC)
ALL = [c for cs in CANARIES.values() for c in cs]
PERSONAS = [u.id for u in SPEC.users if u.persona]
ARMS = [CANARIES["S1-Direct"][0], CANARIES["S1-SPGroup"][0], CANARIES["S1-Entra"][0]]


def wait_until(stores: Stores, check: Callable[[], bool], limit_s: float = 150.0) -> None:
    """Poll like the real loop (both stores every 5 s) until check() holds."""
    start = time.time()
    while time.time() - start < limit_s:
        stores.membership.poll_once()
        stores.acl.poll_once()
        if check():
            return
        time.sleep(5.0)
    raise AssertionError(f"not reflected after {limit_s} s")


@pytest.mark.live
def test_the_stores_follow_the_real_tenant_and_agree_with_the_oracle(pg_dsn: str) -> None:
    schema = f"live_{uuid.uuid4().hex[:10]}"
    with open_env(SPEC_PATH) as env, open_stores(schema, dsn=pg_dsn) as s:
        generate(env)  # the spec state (idempotent)
        time.sleep(20)  # let Entra settle
        doc = {c: str(i["id"]) for c, i in resolve_items(env, ALL).items()}
        by_doc = {d: c for c, d in doc.items()}
        uid = {p: env.user_id(p) for p in PERSONAS}

        def readable(persona: str) -> set[str]:
            return {by_doc[d] for d in s.reader.readable(uid[persona]) if d in by_doc}

        def assert_equals_oracle() -> None:
            expected = expected_access(SPEC, env.log.oracle_mutations(), float("inf"))
            for p in PERSONAS:
                got = readable(p)
                assert got == expected[p], (
                    f"{p}: store-only {sorted(got - expected[p])}, "
                    f"oracle-only {sorted(expected[p] - got)}"
                )

        latencies: dict[str, dict[str, float]] = {}

        def change(label: str, kind: MutationType, params: dict[str, Any], arm: str) -> None:
            called = time.time()
            entry = apply_mutation(env, Mutation(type=kind, params=params))
            confirmed = entry.confirmed_at or time.time()
            wait_until(s, lambda: arm not in readable("p-plateng"))
            seen = time.time()
            latencies[label] = {
                "from_the_call_s": round(seen - called, 1),
                "from_confirmed_at_s": round(seen - confirmed, 1),
            }

        try:
            wait_until(s, lambda: s.reader.freshness().fresh)
            assert_equals_oracle()  # 1. the real tenant, read through the real delta and REST
            change(
                "direct grant removed",
                MutationType.GRANT_REMOVED,
                {"target": "S1-Direct", "principal_kind": "user", "principal_ref": "p-plateng"},
                ARMS[0],
            )
            change(
                "SharePoint-group member removed",
                MutationType.GROUP_MEMBER_REMOVED,
                {"group": "sp-s1-spgroup", "member_kind": "user", "member_ref": "p-plateng"},
                ARMS[1],
            )
            change(
                "Entra-group member removed",
                MutationType.GROUP_MEMBER_REMOVED,
                {"group": "grp-s1-entra", "member_kind": "user", "member_ref": "p-plateng"},
                ARMS[2],
            )
            print("\nLATENCIES (change -> the reader's new answer):", latencies)
            assert_equals_oracle()  # 2. all three reflected, nothing else moved
            drift = s.acl.verify_all()
            assert drift.ok and drift.events == 0, s.repo.events("acl")[-5:]  # 3. nothing missed
        finally:
            generate(env)  # restore the spec state
            time.sleep(20)
        # 4. the tenant is back: the stores catch up and agree with the oracle again
        wait_until(s, lambda: all(a in readable("p-plateng") for a in ARMS))
        expected = expected_access(SPEC, [], 0.0)
        for p in PERSONAS:
            assert readable(p) == expected[p], p
        applied = [round(e.applied_at - e.seen_at, 3) for e in s.repo.events()[-6:]]
        print("events:", len(s.repo.events()), "| last seen->applied (s):", applied)
