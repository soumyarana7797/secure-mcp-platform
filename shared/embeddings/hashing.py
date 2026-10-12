"""A dependency-free, deterministic embedder (feature hashing of words and word pairs).

Not a neural model: similarity is lexical (shared words and phrases). It needs no download, is
reproducible in tests, and is good enough for a demo. Short queries hashed into few buckets
produce random collisions, so it needs WIDE vectors (4096) to keep noise below real matches;
a neural model would not. Swap in a real model by implementing the
`Embedder` protocol; the stored `id` keeps old and new vectors from being mixed.
"""

import hashlib
import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it of on or that the this to was "
    "what when where which who will with you your our we can do does".split()
)


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0  # signed hashing reduces collisions


class HashingEmbedder:
    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim
        self.id = f"hashing-v1-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOPWORDS]
        features = words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
        counts: dict[str, int] = {}
        for feature in features:
            counts[feature] = counts.get(feature, 0) + 1
        vector = [0.0] * self.dim
        for feature, count in counts.items():
            index, sign = _bucket(feature, self.dim)
            vector[index] += sign * (1.0 + math.log(count))  # sub-linear term frequency
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector
