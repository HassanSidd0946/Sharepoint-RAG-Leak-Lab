"""The directory: user object id <-> UPN, for every user the stores can meet.

Why it exists: Graph reports group members by object id, SharePoint reports a user grant by UPN. To
decide "may THIS user (an object id) read a scope that grants Read to alice@x" the two must be tied
together. A user matters if they are a member of any group (an edge) OR are named by a user grant,
so the directory covers the union. (A first version covered group members only: a user who left
their last group lost the UPN their direct grant needed. Found by the oracle property test.)

DirectoryStore.refresh(repo) resolves what is missing, never forgets what it has, and is called
inside the poller's atomic block, so the mapping commits with the change that needed it. An id or
UPN the source cannot resolve stays unmapped: a user grant never matches it (deny).
"""

from collections.abc import Sequence
from typing import Protocol

from leaklab.setup_b.state.repository import StateRepository


class DirectorySource(Protocol):
    def resolve(self, user_ids: Sequence[str]) -> dict[str, str]:
        """user object id -> lower-case UPN, for the ids it knows."""
        ...

    def resolve_upns(self, upns: Sequence[str]) -> dict[str, str]:
        """user object id -> lower-case UPN, for the UPNs it knows (the key is the id found)."""
        ...


class DirectoryStore:
    def __init__(self, source: DirectorySource) -> None:
        self._source = source

    def refresh(self, repo: StateRepository) -> None:
        """Extend the stored directory to every user in a group edge or a user grant."""
        directory = repo.load_directory()
        known_upns = set(directory.values())
        ids = {e.member_id for e in repo.load_edges() if e.member_kind == "user"}
        upns = {
            g.ref for grants in repo.load_acl().grants.values() for g in grants if g.kind == "user"
        }
        before = dict(directory)
        missing_ids = sorted(ids - set(directory))
        if missing_ids:
            try:
                directory.update(self._source.resolve(missing_ids))
            except Exception:  # unresolved: no UPN, so no user grant can match (deny)
                pass
        missing_upns = sorted(upns - known_upns - set(directory.values()))
        if missing_upns:
            try:
                directory.update(self._source.resolve_upns(missing_upns))
            except Exception:
                pass
        if directory != before:
            repo.save_directory(directory)
