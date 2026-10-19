"""Human labels for a bundle: systems, documents and mutations. Only fictional, spec-level names.

System labels and config labels are NOT in the journals; they are fixed here, once, with the exact
caveat the UI must show (CLAUDE.md: every claim comes from a real run, and the configuration is
part of the claim). Document labels come from the spec and its generated plan ("folder/file name"):
never a tenant URL or an item id. Mutation text is built from spec ids and the spec's fictional
group names.
"""

from typing import Any

from leaklab.generator.plan import build_plan
from leaklab.oracle import Mutation, MutationType
from leaklab.spec import TenantSpec, group_display_name

SYSTEMS: dict[str, dict[str, str]] = {
    "setup0": {
        "label": "SharePoint",
        "config_label": "SharePoint itself, read as the persona; no search layer",
    },
    "setup_a_basic": {
        "label": "Azure AI Search",
        "config_label": (
            "Azure AI Search SharePoint indexer, basic ACL configuration "
            "(users and Entra groups, no SharePoint-group support), 5-min schedule, ACL-filtered index"
        ),
    },
    "setup_b": {
        "label": "Revocation layer",
        "config_label": (
            "Setup B: our API over a separate, unfiltered index of the same SharePoint data source; "
            "access decided from live membership and ACL state (polled every 5 s)"
        ),
    },
}
INDEX_OF = {"setup_a_basic": "ll-sp-index", "setup_b": "ll-sp-index-b"}


def doc_labels(spec: TenantSpec) -> dict[str, str]:
    """canary -> 'Folder/file name' for the spec's documents (spec must carry the run's generation)."""
    plan = build_plan(spec)
    labels: dict[str, str] = {}
    for folder in plan.folders:
        for doc in folder.docs:
            labels[doc.canary] = f"{folder.path}/{doc.name}"
    for special in plan.special_files:
        labels[special.doc.canary] = f"{special.folder}/{special.doc.name}"
    return labels


def _principal(spec: TenantSpec, kind: str, ref: str) -> str:
    if kind == "entra_group":
        return group_display_name(spec, ref)
    if kind == "sp_group":
        return f"{spec.naming.prefix}{ref}"
    return ref  # a user: the spec id (p-plateng), not a UPN


def _target(spec: TenantSpec, target: str) -> str:
    if target.startswith("special:"):
        file_id = target[len("special:") :]
        return next((s.name for s in spec.special_files if s.id == file_id), file_id)
    return target


def describe_mutation(spec: TenantSpec, mutation: Mutation) -> str:
    p: dict[str, Any] = dict(mutation.params)
    kind = mutation.type
    if kind in (MutationType.GRANT_REMOVED, MutationType.GRANT_ADDED):
        who = _principal(spec, str(p["principal_kind"]), str(p["principal_ref"]))
        verb = "removed from" if kind is MutationType.GRANT_REMOVED else "added to"
        return f"read grant of {who} {verb} {_target(spec, str(p['target']))}"
    if kind in (MutationType.GROUP_MEMBER_REMOVED, MutationType.GROUP_MEMBER_ADDED):
        group = str(p["group"])
        group_name = (
            f"{spec.naming.prefix}{group}"
            if group in {g.id for g in spec.sp_groups}
            else group_display_name(spec, group)
        )
        member = _principal(
            spec,
            "entra_group" if p["member_kind"] == "entra_group" else "user",
            str(p["member_ref"]),
        )
        verb = "removed from" if kind is MutationType.GROUP_MEMBER_REMOVED else "added to"
        return f"{member} {verb} {group_name}"
    if kind is MutationType.FILE_MOVED:
        name = next((s.name for s in spec.special_files if s.id == p["file"]), str(p["file"]))
        return f"{name} moved to {p['to_folder']}"
    return kind.value.replace("_", " ")
