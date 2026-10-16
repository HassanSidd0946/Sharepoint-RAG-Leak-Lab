"""Builds the synthetic test tenant from tenant_spec.yaml; verifies, mutates and tears it down.

generate()  creates what the spec describes (idempotent); verify() compares the tenant with the spec
through REST/Graph; apply_mutation() changes it and logs when each change was confirmed, in the form
the oracle consumes; teardown() removes only prefix-named objects. Uses only behaviors recorded as
verified in docs/FINDINGS.md.
"""

from leaklab.generator.env import Env, GenSettings
from leaklab.generator.generate import GenerateReport, choose_sku, generate
from leaklab.generator.log import LogEntry, MutationLog
from leaklab.generator.mutate import apply_mutation
from leaklab.generator.plan import GeneratorError, Plan, build_plan
from leaklab.generator.teardown import TeardownReport, teardown
from leaklab.generator.verify import Issue, VerifyReport, verify

__all__ = [
    "Env",
    "GenSettings",
    "GenerateReport",
    "GeneratorError",
    "Issue",
    "LogEntry",
    "MutationLog",
    "Plan",
    "TeardownReport",
    "VerifyReport",
    "apply_mutation",
    "build_plan",
    "choose_sku",
    "generate",
    "teardown",
    "verify",
]
