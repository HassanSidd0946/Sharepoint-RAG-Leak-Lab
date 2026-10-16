"""LIVE test of the generator against the real test tenant. Skipped unless pytest gets --live.

generate -> verify structure through REST/Graph against the spec -> generate again (must change
nothing) -> teardown (always, even if an assertion fails). It creates real users, groups, folders
and documents named with the spec's prefix, and removes them again; it also removes any earlier
objects with those names (the persona users ll-p-intern, ll-p-plateng, ... are part of the spec).

open_env() runs the tenant guard first (assert_test_tenant on the Graph and SharePoint tokens).

Run:  uv run pytest --live tests/test_generator_live.py -q -s
Expect several minutes: user and group creation, membership propagation and ~45 uploads.
"""

from pathlib import Path

import pytest

from leaklab.generator import generate, teardown, verify
from leaklab.generator.cli import open_env

SPEC_PATH = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"
CREATED = (
    "users_created",
    "groups_created",
    "folders_created",
    "files_created",
    "inheritance_broken",
    "assignments_added",
    "root_assignments_added",
    "members_added",
    "extra_assignments_removed",
)


@pytest.mark.live
def test_generate_verify_and_teardown_against_the_real_tenant() -> None:
    with open_env(SPEC_PATH) as env:
        try:
            first = generate(env)
            print("first run:", first.counts)
            report = verify(env)
            for issue in report.issues:
                print(f"{issue.severity}: {issue.what} {issue.expected} {issue.actual}")
            assert report.ok, [i.what for i in report.issues if i.severity == "error"]

            second = generate(env)  # idempotent: nothing more to create or change
            print("second run:", second.counts)
            assert {k: second.counts.get(k, 0) for k in CREATED} == dict.fromkeys(CREATED, 0)
            assert verify(env).ok
        finally:
            removed = teardown(env)
            print("teardown:", removed.counts)
