"""Text -> vector. Implementations are interchangeable behind this protocol."""

from typing import Protocol


class Embedder(Protocol):
    id: str  # stored with every chunk so vectors from different models are never compared
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalised vector per text."""
        ...
