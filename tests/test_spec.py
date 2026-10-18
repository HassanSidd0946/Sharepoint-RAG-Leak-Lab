"""Tests for spec.py: loading, each validation rule, and canary generation."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from leaklab.spec import (
    TenantSpec,
    generate_canaries,
    group_display_name,
    load_spec,
    special_key,
    user_mail_nickname,
    user_upn,
)

REPO_SPEC = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"


def base() -> dict[str, Any]:
    """A fresh, valid spec dict (the real tenant_spec.yaml)."""
    with open(REPO_SPEC, encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def folder(data: dict[str, Any], path: str) -> dict[str, Any]:
    found: dict[str, Any] = next(f for f in data["folders"] if f["path"] == path)
    return found


def group(data: dict[str, Any], gid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(g for g in data["groups"] if g["id"] == gid)
    return found


def assert_invalid(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        TenantSpec.model_validate(data)


def test_real_spec_loads() -> None:
    spec = load_spec(REPO_SPEC)
    assert spec.naming.prefix == "ll-"
    assert len(spec.users) == 13
    assert len(spec.folders) == 11
    assert spec.special_files[0].id == "f-direct-share"


def test_unknown_key_rejected() -> None:
    data = base()
    data["naming"]["typo"] = 1
    assert_invalid(data, "typo")


def test_duplicate_user_id() -> None:
    data = base()
    data["users"].append(copy.deepcopy(data["users"][0]))
    assert_invalid(data, "duplicate user id 'admin'")


def test_duplicate_group_id() -> None:
    data = base()
    data["groups"].append({"id": "grp-hr"})
    assert_invalid(data, "duplicate group id 'grp-hr'")


def test_user_group_id_collision() -> None:
    data = base()
    data["groups"].append({"id": "p-hr"})
    assert_invalid(data, "used by both a user and a group")


def test_unknown_group_member() -> None:
    data = base()
    group(data, "grp-hr")["members"].append("nobody")
    assert_invalid(data, "member 'nobody' is not a known user")


def test_unknown_member_group() -> None:
    data = base()
    group(data, "grp-hr")["member_groups"] = ["grp-ghost"]
    assert_invalid(data, "member_group 'grp-ghost' is not a known group")


def test_unknown_principal_in_folder_grant() -> None:
    data = base()
    folder(data, "HR")["grants"][0]["principal"]["ref"] = "grp-ghost"
    assert_invalid(data, "unknown entra_group 'grp-ghost'")


def test_principal_kind_must_match_ref_type() -> None:
    data = base()
    folder(data, "HR")["grants"][0]["principal"] = {"kind": "user", "ref": "grp-hr"}
    assert_invalid(data, "unknown user 'grp-hr'")


def test_unknown_principal_in_root_grants() -> None:
    data = base()
    data["site"]["root_grants"][0]["principal"]["ref"] = "grp-ghost"
    assert_invalid(data, "site.root_grants")


def test_unknown_principal_in_special_file() -> None:
    data = base()
    data["special_files"][0]["grants"][0]["principal"]["ref"] = "nobody"
    assert_invalid(data, "special file 'f-direct-share'")


def test_group_cycle() -> None:
    data = base()
    group(data, "grp-eng-platform")["member_groups"] = ["grp-all-staff"]
    assert_invalid(data, "group nesting cycle")


def test_group_self_cycle() -> None:
    data = base()
    group(data, "grp-hr")["member_groups"] = ["grp-hr"]
    assert_invalid(data, "grp-hr -> grp-hr")


def test_missing_parent_folder() -> None:
    data = base()
    data["folders"] = [f for f in data["folders"] if f["path"] != "Engineering"]
    del data["documents"]["folder_codes"]["Engineering"]
    assert_invalid(data, "parent folder 'Engineering' does not exist")


def test_non_inheriting_folder_needs_grant() -> None:
    data = base()
    folder(data, "HR")["grants"] = []
    assert_invalid(data, "folder 'HR': inherits=false requires at least one grant")


def test_inheriting_folder_must_not_have_grants() -> None:
    data = base()
    folder(data, "General")["grants"] = [
        {"principal": {"kind": "entra_group", "ref": "grp-hr"}, "role": "read"}
    ]
    assert_invalid(data, "folder 'General': inherits=true must not have grants")


def test_folder_without_code() -> None:
    data = base()
    del data["documents"]["folder_codes"]["Board"]
    assert_invalid(data, "folder 'Board' has no entry in documents.folder_codes")


def test_duplicate_folder_code() -> None:
    data = base()
    data["documents"]["folder_codes"]["Board"] = "HR"
    assert_invalid(data, "code 'HR' is used by more than one folder")


def test_special_file_unknown_folder() -> None:
    data = base()
    data["special_files"][0]["folder"] = "Nowhere"
    assert_invalid(data, "folder 'Nowhere' does not exist")


def test_bad_canary_pattern_placeholder() -> None:
    data = base()
    data["documents"]["canary_pattern"] = "CANARY-{nope}"
    assert_invalid(data, "canary_pattern")


def test_unknown_principal_policy_must_deny() -> None:
    data = base()
    data["policy"]["unknown_principal"] = "allow"
    assert_invalid(data, "unknown_principal")


def test_all_errors_reported_together() -> None:
    data = base()
    folder(data, "HR")["grants"] = []
    del data["documents"]["folder_codes"]["Board"]
    with pytest.raises(ValidationError) as exc:
        TenantSpec.model_validate(data)
    text = str(exc.value)
    assert "folder 'HR'" in text and "folder 'Board'" in text


def test_canaries_deterministic_and_unique() -> None:
    first = generate_canaries(load_spec(REPO_SPEC))
    second = generate_canaries(load_spec(REPO_SPEC))
    assert first == second
    spec = load_spec(REPO_SPEC)
    expected = {f.path: f.doc_count for f in spec.folders}
    expected[special_key("f-direct-share")] = 1
    assert {p: len(c) for p, c in first.items()} == expected
    flat = [c for canaries in first.values() for c in canaries]
    assert len(flat) == 45  # 44 folder documents (36 + 3 S1 arms x 2 + S2 x 2) + 1 special file
    assert len(set(flat)) == len(flat)
    assert flat[0].startswith("CANARY-GEN-01-")
    assert len(flat[0].rsplit("-", 1)[1]) == 6


def test_special_file_canary_is_spc_and_stable() -> None:
    spec = load_spec(REPO_SPEC)
    [canary] = generate_canaries(spec)[special_key("f-direct-share")]
    assert canary.startswith("CANARY-SPC-") and len(canary) == len("CANARY-SPC-") + 6
    assert canary == generate_canaries(load_spec(REPO_SPEC))[special_key("f-direct-share")][0]


def test_special_file_canaries_distinct_per_file() -> None:
    data = base()
    second = copy.deepcopy(data["special_files"][0])
    second["id"] = "f-second"
    second["name"] = "second.txt"
    data["special_files"].append(second)
    result = generate_canaries(TenantSpec.model_validate(data))
    first_canary = result[special_key("f-direct-share")]
    assert first_canary != result[special_key("f-second")]


def test_special_canary_collision_detected() -> None:
    data = base()
    data["documents"]["special_canary_pattern"] = "CANARY-SPC-CONSTANT"
    second = copy.deepcopy(data["special_files"][0])
    second["id"] = "f-second"
    data["special_files"].append(second)
    with pytest.raises(ValueError, match="canary collision"):
        generate_canaries(TenantSpec.model_validate(data))


def test_bad_special_canary_pattern_placeholder() -> None:
    data = base()
    data["documents"]["special_canary_pattern"] = "CANARY-{nope}"
    assert_invalid(data, "special_canary_pattern")


def test_canaries_change_with_seed() -> None:
    a = generate_canaries(load_spec(REPO_SPEC))
    data = base()
    data["naming"]["seed"] += 1
    b = generate_canaries(TenantSpec.model_validate(data))
    assert a != b


def test_canary_collision_detected() -> None:
    data = base()
    data["documents"]["canary_pattern"] = "CANARY-{hex6}-X"
    spec = TenantSpec.model_validate(data)
    # Collisions are only possible if two 6-hex values match; force one via a constant pattern.
    data["documents"]["canary_pattern"] = "CANARY-CONSTANT"
    with pytest.raises(ValueError, match="canary collision"):
        generate_canaries(TenantSpec.model_validate(data))
    assert generate_canaries(spec)


def test_name_helpers_use_prefix_and_domain() -> None:
    spec = load_spec(REPO_SPEC)
    assert user_upn(spec, "p-intern") == f"ll-p-intern@{spec.naming.domain}"
    assert user_mail_nickname(spec, "p-intern") == "ll-p-intern"
    assert group_display_name(spec, "grp-hr") == "ll-grp-hr"


def test_name_helpers_reject_unknown_ids() -> None:
    spec = load_spec(REPO_SPEC)
    with pytest.raises(ValueError):
        user_upn(spec, "nobody")
    with pytest.raises(ValueError):
        user_mail_nickname(spec, "grp-hr")  # a group id is not a user id
    with pytest.raises(ValueError):
        group_display_name(spec, "p-intern")


def test_sharepoint_groups_and_scenarios_are_checked() -> None:
    data = base()
    data["sp_groups"] = [{"id": "sg", "members": ["nobody"]}]
    assert_invalid(data, "SharePoint group 'sg': member 'nobody' is not a known user")
    data = base()
    data["sp_groups"] = [{"id": "sg", "members": []}, {"id": "sg", "members": []}]
    assert_invalid(data, "duplicate SharePoint group id 'sg'")
    data = base()
    data["sp_groups"] = [{"id": data["users"][0]["id"], "members": []}]
    assert_invalid(data, "used by a SharePoint group and by a user or group")
    data = base()
    data["scenarios"] = [{"id": "x", "description": "d", "control": "no-such-folder"}]
    assert_invalid(data, "control 'no-such-folder' is not a folder or file")
    data = base()
    data["scenarios"] = [{"id": "x", "description": "d"}, {"id": "x", "description": "e"}]
    assert_invalid(data, "duplicate scenario id 'x'")


def test_a_grant_to_an_unknown_sharepoint_group_is_rejected() -> None:
    data = base()
    data["folders"][0]["grants"] = [
        {"principal": {"kind": "sp_group", "ref": "ghost"}, "role": "read"}
    ]
    data["folders"][0]["inherits"] = False
    assert_invalid(data, "unknown sp_group 'ghost'")
