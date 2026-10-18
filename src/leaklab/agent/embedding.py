"""A deterministic local embedding for the benchmark: the same text always gives the same vector.

HashEmbedder is the hashing trick over the content words of a text: lower-case word and number
tokens, a small stopword list removed, each token hashed to one of `dims` positions with a sign,
the vector scaled to length 1. Two texts that share most of their content words have a cosine
similarity near 1; texts that share none are near 0 (a little noise from hash collisions). It is
NOT a semantic model: it is chosen so that the benchmark needs no model download, no network and
no randomness, and so that the query set can be designed (and tested) to hit or miss the cache.
The Embedder protocol is the plug-in point for a real embedding model.
"""

import hashlib
import math
import re
from typing import Protocol

DIMS = 256
STOPWORDS = frozenset(
    "a an and are about any as at be by for from has have how i in is it its me of on or "
    "our that the this to was were what when where which who why with you your does do did "
    "can could would should tell please".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dims: int

    def embed(self, text: str) -> list[float]: ...


def tokens(text: str) -> list[str]:
    """The content words of `text` (lower case, stopwords removed)."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


class HashEmbedder:
    def __init__(self, dims: int = DIMS) -> None:
        self.dims = dims

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for token in tokens(text):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dims
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors (0.0 if either is all zeros)."""
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0
