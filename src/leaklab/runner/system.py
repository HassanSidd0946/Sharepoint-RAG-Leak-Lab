"""The interface every system under test implements.

A system answers one question: if `persona` asks for `canary` right now, is the canary served?
`read` must never raise for an operational problem (network error, expired token, throttling): it
returns an Observation with served=None and an error text. The engine also guards against a system
that raises anyway, and records the read as an error, never as a denial.

Setup0 (plain SharePoint, no RAG) is in setup0.py. Setup A (Azure AI Search) and Setup B (our
overlay) will implement the same protocol.
"""

from collections.abc import Sequence
from typing import Protocol

from leaklab.runner.model import Observation


class SystemUnderTest(Protocol):
    name: str

    def prepare(self, personas: Sequence[str], canaries: Sequence[str]) -> None:
        """Get ready to read: sign the personas in, resolve the canaries. Raise if that fails."""
        ...

    def read(self, persona: str, canary: str) -> Observation:
        """One read. Never raises for operational problems; see the module docstring."""
        ...

    def close(self) -> None: ...
