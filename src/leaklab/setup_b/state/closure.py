"""Pure functions: nested-group closure and the reading of SharePoint grants.

transitive_groups(edges) gives, for every user that is a member of anything, the set of groups that
contain the user directly or through nesting (cycle-safe: a cycle never loops and never adds a group
twice). parse_login() reduces a SharePoint login claim to what the stores understand.
"""

from collections import defaultdict
from collections.abc import Iterable

from leaklab.setup_b.state.model import Edge, GrantKind

USER_PREFIX = "i:0#.f|membership|"  # verified 2026-10-15: ensureuser returns it for a UPN
GROUP_PREFIX = "c:0t.c|tenant|"  # verified 2026-10-15: an Entra security group as a principal


def transitive_groups(edges: Iterable[Edge]) -> dict[str, set[str]]:
    """user id -> every group id that contains the user, directly or through nested groups."""
    parents: dict[str, set[str]] = defaultdict(set)  # group id -> groups that contain it
    direct: dict[str, set[str]] = defaultdict(set)  # user id -> groups that contain the user
    for e in edges:
        if e.member_kind == "group":
            parents[e.member_id].add(e.group_id)
        else:
            direct[e.member_id].add(e.group_id)
    closure: dict[str, set[str]] = {}
    for user, groups in direct.items():
        seen: set[str] = set()
        stack = list(groups)
        while stack:
            group = stack.pop()
            if group in seen:
                continue
            seen.add(group)
            stack.extend(parents.get(group, ()))
        closure[user] = seen
    return closure


def parse_login(login: str, principal_type: int) -> tuple[GrantKind, str]:
    """(kind, ref) of a SharePoint principal; anything unrecognised is ("unknown", login).

    Only a user claim (type 1) and an Entra group claim (type 4) are understood. A SharePoint group
    (type 8) is expanded by the caller, never parsed here.
    """
    if principal_type == 1 and login.startswith(USER_PREFIX):
        return "user", login[len(USER_PREFIX) :].lower()
    if principal_type == 4 and login.startswith(GROUP_PREFIX):
        return "entra_group", login[len(GROUP_PREFIX) :].lower()
    return "unknown", login
