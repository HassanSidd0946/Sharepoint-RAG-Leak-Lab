"""The mutation log: what was changed in the tenant, and when each change was CONFIRMED.

The oracle consumes this: oracle_mutations() returns oracle.Mutation objects whose `confirmed_at`
is the moment Entra/REST first showed the change (None if it never did within the timeout, which
leaves intent unchanged). Times are epoch seconds (time.time()); the scenario runner must stamp its
observations with the same clock.

The baseline (the generated tenant itself) is NOT in the log: it is the spec. The log holds only
the changes made afterwards through Generator.apply_mutation().
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from leaklab.oracle import Mutation


class LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    mutation: Mutation  # as requested; confirmed_at here is always None
    requested_at: float
    executed_at: float | None = None
    confirmed_at: float | None = None
    status: Literal["confirmed", "unconfirmed", "failed"]
    note: str = ""


class MutationLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: float
    entries: list[LogEntry] = Field(default_factory=list)

    def add(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def oracle_mutations(self) -> list[Mutation]:
        """The mutations in the form the oracle takes, with confirmed_at filled in.

        Failed mutations are left out (they changed nothing); unconfirmed ones keep
        confirmed_at None, so intent does not change for them.
        """
        return [
            e.mutation.model_copy(update={"confirmed_at": e.confirmed_at})
            for e in self.entries
            if e.status != "failed"
        ]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "MutationLog":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
