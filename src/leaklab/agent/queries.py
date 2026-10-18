"""The fixed, versioned S6 query set (queries.yaml) and a check that it does what it was designed for.

`validate(queries, embedder)` returns the problems it finds, an empty list if the set is sound:
every cache-designed follow-up must score at least `margin` above the cache threshold against its
seed; every shared-memory follow-up must score BELOW the cache threshold against the seed (so the
cache does not answer) and at least the memory threshold against the fact the seed's answer leaves
in memory. The S6 driver refuses to run a set that does not validate.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from leaklab.agent.embedding import Embedder, cosine

QUERIES_PATH = Path(__file__).with_name("queries.yaml")
MARGIN = 0.05


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["cross_user_cache", "cache_after_revocation", "shared_memory", "positive_control"]
    seed: str
    source: str  # title of the document the seed question must retrieve for its asker
    followups: list[str]
    owner: str | None = None
    reader: str | None = None
    user: str | None = None  # S6b: the same user asks both times
    scenario: str | None = None  # S6b: the scenario whose mutation revokes the access


class QuerySet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    cache_threshold: float
    memory_threshold: float
    variants: dict[str, Variant]


def load_queries(path: Path = QUERIES_PATH) -> QuerySet:
    return QuerySet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def validate(queries: QuerySet, embedder: Embedder, fact_of_seed: dict[str, str]) -> list[str]:
    """Problems with the set. `fact_of_seed`: variant -> the memory fact its seed's answer leaves."""
    problems: list[str] = []
    for name, v in queries.variants.items():
        if len(v.followups) < 3:
            problems.append(f"{name}: needs at least 3 follow-ups (n=3 trials)")
        seed = embedder.embed(v.seed)
        for i, q in enumerate(v.followups, start=1):
            sim = cosine(seed, embedder.embed(q))
            if v.kind == "shared_memory":
                if sim >= queries.cache_threshold:
                    problems.append(
                        f"{name}[{i}]: similarity {sim:.2f} to the seed would HIT the cache"
                    )
                fact = fact_of_seed.get(name)
                if fact is None:
                    problems.append(f"{name}: no memory fact given to check against")
                else:
                    msim = cosine(embedder.embed(q), embedder.embed(fact))
                    if msim < queries.memory_threshold:
                        problems.append(
                            f"{name}[{i}]: memory similarity {msim:.2f} is below the threshold"
                        )
            elif sim < queries.cache_threshold + MARGIN:
                problems.append(
                    f"{name}[{i}]: similarity {sim:.2f} to the seed is not safely above the cache "
                    f"threshold {queries.cache_threshold}"
                )
    return problems
