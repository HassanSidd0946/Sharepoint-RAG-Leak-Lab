"""The plan: what the tenant must contain, computed from the spec ONLY (no network, no state).

Inputs: a TenantSpec. Output: a Plan listing the users, groups, memberships, folders, documents
(with deterministic text and their canaries) and the grants to apply, in dependency order.

Decisions (see docs/FINDINGS.md, "Generator notes"):
  - Everything generated lives under ONE container folder named "<prefix>lab" in the document
    library. The spec's folder paths ("General", "Finance/Leadership", ...) do not start with the
    prefix, so they are created inside the container and teardown deletes only the container.
    The container inherits the site root and is transparent to the oracle, which works with spec
    paths.
  - Users the spec marks as "not generated" (the admin) are neither created nor granted anything;
    grants that name them are skipped and listed in Plan.skipped.
  - Document file names are "<prefix><folder code>-<nn>.txt"; their text is rendered from
    documents.body_template deterministically from the spec's seed.

This module must not talk to Graph or SharePoint.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from leaklab.spec import (
    Grant,
    TenantSpec,
    generate_canaries,
    group_display_name,
    special_key,
    user_mail_nickname,
    user_upn,
)

ROLE_NAMES = {"read": "Read", "full_control": "Full Control"}
CONTAINER_SUFFIX = "lab"


class GeneratorError(RuntimeError):
    """The generator cannot do what was asked safely. Never ignored."""


@dataclass(frozen=True)
class UserPlan:
    id: str
    upn: str
    nickname: str
    display: str
    licensed: bool
    persona: bool


@dataclass(frozen=True)
class GroupPlan:
    id: str
    name: str
    user_members: tuple[str, ...]
    group_members: tuple[str, ...]


@dataclass(frozen=True)
class GrantPlan:
    kind: str  # "user" | "entra_group"
    ref: str  # spec user id or group id
    role_name: str  # SharePoint role definition name, e.g. "Read"


@dataclass(frozen=True)
class DocPlan:
    name: str
    canary: str
    text: str


@dataclass(frozen=True)
class FolderPlan:
    path: str  # spec path, relative to the container
    inherits: bool
    grants: tuple[GrantPlan, ...]
    docs: tuple[DocPlan, ...]


@dataclass(frozen=True)
class SpecialPlan:
    id: str
    folder: str
    doc: DocPlan
    inherits: bool
    grants: tuple[GrantPlan, ...]


@dataclass(frozen=True)
class Plan:
    prefix: str
    container: str
    users: tuple[UserPlan, ...]
    groups: tuple[GroupPlan, ...]
    folders: tuple[FolderPlan, ...]  # parents before children
    special_files: tuple[SpecialPlan, ...]
    root_grants: tuple[GrantPlan, ...]
    skipped: tuple[str, ...]


def render_document(spec: TenantSpec, title: str, key: str, index: int, canary: str) -> str:
    """Deterministic body text from body_template (placeholders: title, topic, canary, filler)."""
    topics = spec.documents.topics
    topic = topics[(spec.naming.seed + index + len(key)) % len(topics)]
    digest = hashlib.sha256(f"{spec.naming.seed}|{key}|{index}|filler".encode()).hexdigest()
    filler = " ".join(digest[i : i + 8] for i in range(0, 48, 8))
    try:
        return spec.documents.body_template.format(
            title=title, topic=topic, canary=canary, filler=filler
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise GeneratorError(
            "documents.body_template may only use {title}, {topic}, {canary} and {filler}: "
            f"{exc!r}"
        ) from exc


def external_user_ids(spec: TenantSpec) -> set[str]:
    """Users the spec says are not generated (e.g. the admin account)."""
    return {u.id for u in spec.users if "not generated" in u.note.lower()}


def build_plan(spec: TenantSpec) -> Plan:
    prefix = spec.naming.prefix
    external = external_user_ids(spec)
    skipped: list[str] = []

    def grants_of(owner: str, grants: Sequence[Grant]) -> tuple[GrantPlan, ...]:
        out: list[GrantPlan] = []
        for g in grants:
            if g.principal.kind == "user" and g.principal.ref in external:
                skipped.append(f"{owner}: grant to the not-generated user {g.principal.ref!r}")
                continue
            if g.role not in ROLE_NAMES:
                raise GeneratorError(f"{owner}: role {g.role!r} has no SharePoint role mapping")
            out.append(GrantPlan(g.principal.kind, g.principal.ref, ROLE_NAMES[g.role]))
        return tuple(out)

    users = tuple(
        UserPlan(
            id=u.id,
            upn=user_upn(spec, u.id),
            nickname=user_mail_nickname(spec, u.id),
            display=u.display,
            licensed=u.licensed,
            persona=u.persona,
        )
        for u in spec.users
        if u.id not in external
    )
    groups = tuple(
        GroupPlan(
            id=g.id,
            name=group_display_name(spec, g.id),
            user_members=tuple(m for m in g.members if m not in external),
            group_members=tuple(g.member_groups),
        )
        for g in spec.groups
    )

    canaries = generate_canaries(spec)
    folders: list[FolderPlan] = []
    for f in sorted(spec.folders, key=lambda f: (f.path.count("/"), f.path)):
        code = spec.documents.folder_codes[f.path].lower()
        docs = tuple(
            DocPlan(
                name=f"{prefix}{code}-{i:02d}.txt",
                canary=canary,
                text=render_document(spec, f"{f.path} document {i:02d}", f.path, i, canary),
            )
            for i, canary in enumerate(canaries[f.path], start=1)
        )
        folders.append(
            FolderPlan(f.path, f.inherits, grants_of(f"folder {f.path!r}", f.grants), docs)
        )

    specials = []
    for sf in spec.special_files:
        canary = canaries[special_key(sf.id)][0]
        doc = DocPlan(
            sf.name, canary, render_document(spec, sf.name, f"special:{sf.id}", 1, canary)
        )
        specials.append(
            SpecialPlan(
                sf.id, sf.folder, doc, sf.inherits, grants_of(f"special file {sf.id!r}", sf.grants)
            )
        )

    return Plan(
        prefix=prefix,
        container=f"{prefix}{CONTAINER_SUFFIX}",
        users=users,
        groups=groups,
        folders=tuple(folders),
        special_files=tuple(specials),
        root_grants=grants_of("site root", spec.site.root_grants),
        skipped=tuple(skipped),
    )


def assert_prefixed(name: str, prefix: str) -> None:
    """Teardown safety: refuse to touch anything whose name does not start with the prefix."""
    if not name.startswith(prefix):
        raise GeneratorError(f"refusing to touch {name!r}: it does not start with {prefix!r}")
