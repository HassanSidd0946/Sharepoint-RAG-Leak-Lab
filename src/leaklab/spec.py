"""Pydantic v2 models for tenant_spec.yaml, plus deterministic canary generation.

Inputs: the path to tenant_spec.yaml (load_spec) or an already-built TenantSpec.
Outputs: a validated, immutable TenantSpec; generate_canaries() maps each folder path to its
list of canary strings. Unknown YAML keys are rejected so typos fail loudly. All
cross-reference problems are collected and reported together in one error message.

This module must stay free of engine code (it is shared with the independent oracle).
"""

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


class _Model(BaseModel):
    """Base: reject unknown keys and make instances immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Naming(_Model):
    prefix: str
    domain: str
    seed: int


class Principal(_Model):
    kind: Literal["user", "entra_group"]
    ref: str


class Grant(_Model):
    principal: Principal
    role: Literal["read", "full_control"]


class Site(_Model):
    title: str
    url: str
    library: str
    root_grants: list[Grant]


class User(_Model):
    id: str
    display: str
    persona: bool
    licensed: bool
    note: str = ""


class Group(_Model):
    id: str
    members: list[str] = []
    member_groups: list[str] = []


class Folder(_Model):
    path: str
    inherits: bool
    grants: list[Grant] = []
    doc_count: int


class SpecialFile(_Model):
    id: str
    folder: str
    name: str
    inherits: bool
    grants: list[Grant] = []
    note: str = ""


class Documents(_Model):
    format: str
    canary_pattern: str
    folder_codes: dict[str, str]
    body_template: str
    topics: list[str]


class Policy(_Model):
    org_wide_sharing_link: str
    unknown_principal: Literal["deny"]  # fail closed is not configurable
    guest_users: str


class Scenario(_Model):
    id: str
    description: str


class TenantSpec(_Model):
    version: int
    naming: Naming
    site: Site
    users: list[User]
    groups: list[Group]
    folders: list[Folder]
    special_files: list[SpecialFile] = []
    documents: Documents
    policy: Policy
    scenarios: list[Scenario] = []

    @model_validator(mode="after")
    def _check_cross_references(self) -> "TenantSpec":
        errors: list[str] = []
        errors += self._check_ids()
        errors += self._check_principals()
        errors += self._check_group_members()
        errors += self._check_group_cycles()
        errors += self._check_folders()
        errors += self._check_special_files()
        errors += self._check_documents()
        if errors:
            raise ValueError("invalid tenant spec:\n- " + "\n- ".join(errors))
        return self

    # -- helpers ---------------------------------------------------------------------------

    @property
    def user_ids(self) -> set[str]:
        return {u.id for u in self.users}

    @property
    def group_ids(self) -> set[str]:
        return {g.id for g in self.groups}

    def _check_grants(self, owner: str, grants: list[Grant]) -> list[str]:
        errors: list[str] = []
        for grant in grants:
            p = grant.principal
            known = self.user_ids if p.kind == "user" else self.group_ids
            if p.ref not in known:
                errors.append(f"{owner}: grant refers to unknown {p.kind} {p.ref!r}")
        return errors

    def _check_inherit_rules(self, owner: str, inherits: bool, grants: list[Grant]) -> list[str]:
        if not inherits and not grants:
            return [f"{owner}: inherits=false requires at least one grant"]
        if inherits and grants:
            return [f"{owner}: inherits=true must not have grants"]
        return []

    # -- rules -----------------------------------------------------------------------------

    def _check_ids(self) -> list[str]:
        errors: list[str] = []
        seen_users: set[str] = set()
        for u in self.users:
            if u.id in seen_users:
                errors.append(f"duplicate user id {u.id!r}")
            seen_users.add(u.id)
        seen_groups: set[str] = set()
        for g in self.groups:
            if g.id in seen_groups:
                errors.append(f"duplicate group id {g.id!r}")
            seen_groups.add(g.id)
        for clash in sorted(seen_users & seen_groups):
            errors.append(f"id {clash!r} is used by both a user and a group")
        return errors

    def _check_principals(self) -> list[str]:
        errors = self._check_grants("site.root_grants", self.site.root_grants)
        for folder in self.folders:
            errors += self._check_grants(f"folder {folder.path!r}", folder.grants)
        for sf in self.special_files:
            errors += self._check_grants(f"special file {sf.id!r}", sf.grants)
        return errors

    def _check_group_members(self) -> list[str]:
        errors: list[str] = []
        for g in self.groups:
            for m in g.members:
                if m not in self.user_ids:
                    errors.append(f"group {g.id!r}: member {m!r} is not a known user")
            for mg in g.member_groups:
                if mg not in self.group_ids:
                    errors.append(f"group {g.id!r}: member_group {mg!r} is not a known group")
        return errors

    def _check_group_cycles(self) -> list[str]:
        children = {g.id: [c for c in g.member_groups if c in self.group_ids] for g in self.groups}
        done: set[str] = set()
        errors: list[str] = []

        def visit(node: str, stack: list[str]) -> None:
            if node in stack:
                cycle = stack[stack.index(node) :] + [node]
                errors.append("group nesting cycle: " + " -> ".join(cycle))
                return
            if node in done:
                return
            for child in children.get(node, []):
                visit(child, [*stack, node])
            done.add(node)

        for gid in children:
            visit(gid, [])
        return errors

    def _check_folders(self) -> list[str]:
        errors: list[str] = []
        paths: set[str] = set()
        for f in self.folders:
            if f.path in paths:
                errors.append(f"duplicate folder path {f.path!r}")
            paths.add(f.path)
        for f in self.folders:
            segments = f.path.split("/")
            if not f.path or any(not s.strip() for s in segments):
                errors.append(f"folder path {f.path!r} is empty or has an empty segment")
                continue
            if len(segments) > 1:
                parent = "/".join(segments[:-1])
                if parent not in paths:
                    errors.append(f"folder {f.path!r}: parent folder {parent!r} does not exist")
            if f.doc_count < 0:
                errors.append(f"folder {f.path!r}: doc_count must not be negative")
            errors += self._check_inherit_rules(f"folder {f.path!r}", f.inherits, f.grants)
        return errors

    def _check_special_files(self) -> list[str]:
        errors: list[str] = []
        paths = {f.path for f in self.folders}
        seen: set[str] = set()
        for sf in self.special_files:
            if sf.id in seen:
                errors.append(f"duplicate special file id {sf.id!r}")
            seen.add(sf.id)
            if sf.folder not in paths:
                errors.append(f"special file {sf.id!r}: folder {sf.folder!r} does not exist")
            errors += self._check_inherit_rules(f"special file {sf.id!r}", sf.inherits, sf.grants)
        return errors

    def _check_documents(self) -> list[str]:
        errors: list[str] = []
        codes = self.documents.folder_codes
        for f in self.folders:
            if f.path not in codes:
                errors.append(f"folder {f.path!r} has no entry in documents.folder_codes")
        for code in sorted({c for c in codes.values() if list(codes.values()).count(c) > 1}):
            errors.append(f"documents.folder_codes: code {code!r} is used by more than one folder")
        try:
            self.documents.canary_pattern.format(folder_code="X", index=1, hex6="abcdef")
        except (KeyError, IndexError, ValueError) as exc:
            errors.append(
                "documents.canary_pattern may only use {folder_code}, {index} and {hex6}: "
                f"{exc!r}"
            )
        return errors


def load_spec(path: str | Path) -> TenantSpec:
    """Read and validate a tenant spec YAML file. Raises on any problem."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return TenantSpec.model_validate(data)


def generate_canaries(spec: TenantSpec) -> dict[str, list[str]]:
    """Return folder path -> canary strings (doc_count per folder), deterministically.

    Each canary's hex part is the first 6 hex chars of sha256("<seed>|<folder>|<index>"),
    with index starting at 1. The same spec always yields the same canaries. Raises
    ValueError if two canaries collide (e.g. a pattern that drops the folder code or index).
    """
    result: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for folder in spec.folders:
        code = spec.documents.folder_codes[folder.path]
        canaries: list[str] = []
        for index in range(1, folder.doc_count + 1):
            digest = hashlib.sha256(
                f"{spec.naming.seed}|{folder.path}|{index}".encode()
            ).hexdigest()
            canary = spec.documents.canary_pattern.format(
                folder_code=code, index=index, hex6=digest[:6]
            )
            if canary in seen:
                raise ValueError(
                    f"canary collision: {canary!r} generated for both "
                    f"{seen[canary]!r} and {folder.path!r}"
                )
            seen[canary] = folder.path
            canaries.append(canary)
        result[folder.path] = canaries
    return result
