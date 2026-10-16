"""Independent expected-access model and scoring.

Computes who SHOULD be able to read which canary from tenant_spec.yaml plus the applied mutations,
and scores what a system actually served against that. It must never import engine code: no
setup_a, setup_b, graph_client, sp_rest, auth, config or HTTP libraries (enforced by
tests/test_oracle_isolation.py). Its only first-party dependency is leaklab.spec.
"""

from leaklab.oracle.access import IntentTimeline, expected_access
from leaklab.oracle.model import (
    KNOWN_KINDS,
    READ_ROLES,
    ROOT_TARGET,
    Mutation,
    MutationType,
    OracleError,
)
from leaklab.oracle.scoring import (
    CaseResult,
    Convergence,
    Observation,
    Segment,
    StaticExpected,
    Verdict,
    classify,
    summarize,
)

__all__ = [
    "KNOWN_KINDS",
    "READ_ROLES",
    "ROOT_TARGET",
    "CaseResult",
    "Convergence",
    "IntentTimeline",
    "Mutation",
    "MutationType",
    "Observation",
    "OracleError",
    "Segment",
    "StaticExpected",
    "Verdict",
    "classify",
    "expected_access",
    "summarize",
]
