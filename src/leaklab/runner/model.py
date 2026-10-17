"""Shared types of the scenario runner: one read (Observation), the run settings, and errors.

An Observation is what ONE system returned to ONE persona for ONE canary at one moment:
  served True   the canary string was in what the system returned (exact string match)
  served False  the system answered and the canary was NOT served (a denial, or other content)
  served None   we do NOT know (transport error, 401, 429, 5xx, ...). Never treated as a denial:
                it is counted as an error and left out of the scoring.
Times (`ts`) are epoch seconds from the same clock as the generator's mutation log, so scoring can
compare a read with the moment a mutation was confirmed.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

Pair = tuple[str, str]  # (persona id, canary string)


class RunnerError(RuntimeError):
    """The runner cannot do what was asked safely. Never ignored."""


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str
    persona: str
    canary: str
    ts: float
    served: bool | None
    status: int | None = None  # the first HTTP status (Graph's answer), if there was one
    final_status: int | None = None  # the status of the download after a redirect, if any
    error: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True)
class RunConfig:
    """How a run is paced and judged. The defaults are the ones the design asks for."""

    baseline_k: int = 5  # consecutive conforming cycles needed before the mutation
    baseline_cap_s: float = 15 * 60  # give up waiting for a stable baseline after this
    n_consecutive: int = 10  # conforming reads in a row that count as "converged"
    cap_s: float = 60 * 60  # stop polling after this long past the mutation
    interval_s: float = 10.0  # time between the starts of two read cycles
    steady_window_s: float = 120.0  # how long to watch a scenario whose intent does not flip
    workers: int = 1  # parallel reads inside one cycle (1 = sequential)

    def __post_init__(self) -> None:
        if self.baseline_k < 1 or self.n_consecutive < 1 or self.workers < 1:
            raise ValueError("baseline_k, n_consecutive and workers must be at least 1")
        if self.interval_s < 0 or self.cap_s <= 0 or self.baseline_cap_s <= 0:
            raise ValueError("interval_s must be >= 0; cap_s and baseline_cap_s must be > 0")

    def as_dict(self) -> dict[str, float]:
        return {
            "baseline_k": self.baseline_k,
            "baseline_cap_s": self.baseline_cap_s,
            "n_consecutive": self.n_consecutive,
            "cap_s": self.cap_s,
            "interval_s": self.interval_s,
            "steady_window_s": self.steady_window_s,
            "workers": self.workers,
        }
