"""LIVE test of setup_a_basic against the real search service. Skipped unless pytest gets --live.

Needs: the tenant generated (`python -m leaklab.generator generate`), the search service provisioned
(probes/search_provision.py), the index populated by at least one indexer run, and the persona app
consented for the Azure AI Search resource. It only READS: it provisions nothing that is missing
(the system's factory does PUT missing objects, with the smallest schedule), changes no tenant data.

Run:  uv run pytest --live tests/test_setup_a_live.py -q -s
"""

from pathlib import Path

import pytest

from leaklab.generator.cli import open_env
from leaklab.runner.tenant import SYSTEMS, SystemContext, canary_locations
from leaklab.spec import generate_canaries

SPEC_PATH = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"


@pytest.mark.live
def test_setup_a_basic_reads_the_real_index_as_each_persona() -> None:
    import time

    spec_canaries = None
    with open_env(SPEC_PATH) as env:
        spec_canaries = generate_canaries(env.spec)
        canaries = list(canary_locations(env))
        system = SYSTEMS["setup_a_basic"](SystemContext(env, canaries, time.time))
        try:
            general = spec_canaries["General"][0]
            board = spec_canaries["Board"][0]
            system.prepare(["p-intern", "p-ceo"], [general, board])
            allowed = system.read("p-intern", general)  # General: everybody in all-staff
            denied = system.read("p-intern", board)  # Board: only grp-board and p-ceo
            print("allowed:", allowed.model_dump())
            print("denied:", denied.model_dump())
            assert allowed.system == "setup_a_basic"
            assert allowed.served is True and allowed.error is None
            # Present in the index (an entitled persona sees it) and not shown to p-intern
            assert system.read("p-ceo", board).served is True
            assert denied.served in (False, None)
            if denied.served is None:
                assert denied.error in ("not_indexed", "presence_unknown")
            assert not system.read("p-intern", board).served  # never a leak
            events = system.drain_events()  # type: ignore[attr-defined]
            print("indexer events:", events[:3])
            assert all(e["kind"].startswith("indexer_run") for e in events)
        finally:
            system.close()
