"""Min / median / max of a small sample: shared by the benchmark report and the S6 report."""

from collections.abc import Sequence
from statistics import median

from pydantic import BaseModel


class Stat(BaseModel):
    n: int
    min: float | None = None
    median: float | None = None
    max: float | None = None


def stat(values: Sequence[float]) -> Stat:
    if not values:
        return Stat(n=0)
    return Stat(
        n=len(values),
        min=round(min(values), 3),
        median=round(float(median(values)), 3),
        max=round(max(values), 3),
    )
