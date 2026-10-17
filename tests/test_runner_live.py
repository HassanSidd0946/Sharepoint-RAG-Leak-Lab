"""LIVE test of the scenario runner on the real test tenant. Skipped unless pytest gets --live.

Runs ONE fast scenario (S1-direct: a direct user grant, which SharePoint revokes within ~16 s)
through Setup0 with persona tokens, from a freshly generated tenant, and tears the tenant down at
the end (always). It checks the plumbing - sign-in, real reads, baseline, a real mutation, journal,
report - not any particular lag. The slow Entra-group arm (S1-entra, ~36 min) is deliberately not
part of this test.

Run:  uv run pytest --live tests/test_runner_live.py -q -s
Expect about 6-8 minutes: generate (~4 min), baseline, revocation, teardown.
"""

from pathlib import Path

import pytest

from leaklab.generator import teardown
from leaklab.generator.cli import open_env
from leaklab.runner.model import RunConfig
from leaklab.runner.run import run_once

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "tenant_spec.yaml"


@pytest.mark.live
def test_s1_direct_through_setup0_on_the_real_tenant() -> None:
    config = RunConfig(
        baseline_k=3,
        baseline_cap_s=300.0,
        n_consecutive=5,
        cap_s=300.0,
        interval_s=5.0,
        workers=4,
    )
    try:
        journal, report = run_once(
            spec_path=SPEC_PATH,
            scenario_ids=["S1-direct"],
            system_names=["setup0"],
            config=config,
            out_dir=ROOT / "runs",  # git-ignored; the journal is the evidence
            fresh=True,
        )
        [r] = report.results
        print(report.model_dump_json(indent=2, exclude={"results": {"__all__": {"pairs"}}}))
        assert report.finished and journal.exists()
        assert r.mutation_status == "confirmed" and r.t_change is not None
        assert r.baseline_status == "BASELINE_OK"
        assert r.end_reason == "converged" and r.convergence == "CONFORMED"
        assert r.nothing_to_revoke == []
    finally:
        with open_env(SPEC_PATH) as env:
            print("teardown:", teardown(env).counts)
