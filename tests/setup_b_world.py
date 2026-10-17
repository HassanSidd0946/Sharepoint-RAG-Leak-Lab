"""A generated fake tenant with both Setup B stores, the reader and fake sources wired over a repo."""

from typing import Any

from leaklab.generator.mutate import apply_mutation
from leaklab.oracle import Mutation, MutationType
from leaklab.runner.tenant import resolve_items
from leaklab.setup_b.state.access import StateReader
from leaklab.setup_b.state.acl import AclStore
from leaklab.setup_b.state.directory import DirectoryStore
from leaklab.setup_b.state.membership import MembershipStore
from runner_fakes import make_world
from setup_b_fakes import FakeDirectory, FakeDriveDelta, FakeGroupDelta, FakeSharePointSource


class World:
    def __init__(
        self, repo: Any, *, report_shared_changes: bool = True, max_age_s: float = 30.0
    ) -> None:
        self.repo = repo
        self.tenant, self.env, self.clock = make_world()
        self.group_src = FakeGroupDelta(self.tenant)
        self.directory = FakeDirectory(self.tenant)
        self.drive = FakeDriveDelta(self.tenant, report_shared_changes=report_shared_changes)
        self.sp = FakeSharePointSource(self.tenant)
        store = DirectoryStore(self.directory)
        self.membership = MembershipStore(repo, self.group_src, store, self.clock.time)
        self.acl = AclStore(
            repo, self.drive, self.sp, self.clock.time, container="ll-lab", directory=store
        )
        self.reader = StateReader(repo, self.clock.time, max_age_s=max_age_s)

    def sync(self) -> tuple[Any, Any]:
        """One poll of each store (one 5 s cycle)."""
        self.clock.sleep(5.0)
        return self.membership.poll_once(), self.acl.poll_once()

    def apply(self, kind: str, **params: Any) -> Any:
        return apply_mutation(self.env, Mutation(type=MutationType(kind), params=params))

    def uid(self, persona: str) -> str:
        return str(self.env.user_id(persona))

    def doc_id(self, canary: str) -> str:
        return str(resolve_items(self.env, [canary])[canary]["id"])
