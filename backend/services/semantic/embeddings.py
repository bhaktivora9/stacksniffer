"""Embedding providers and strict vector validation.

A provider returns raw vectors. `validate_vector` accepts one only when it has exactly the
profile's dimension and finite values; anything else raises. Nothing is truncated, padded or
coerced. The profile's normalization policy is applied explicitly and recorded with the vector.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .profiles import EmbeddingProfile


class EmbeddingError(RuntimeError):
    """The provider's output cannot be stored under the profile."""


class EmbeddingDimensionError(EmbeddingError):
    pass


@dataclass(frozen=True)
class ProviderEmbedding:
    values: Sequence[float]
    metadata: dict = field(default_factory=dict)


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str], *, profile: EmbeddingProfile, task: str | None) -> list[ProviderEmbedding]:
        ...


def validate_vector(values: Sequence[float], profile: EmbeddingProfile) -> list[float]:
    if len(values) != profile.dimension:
        raise EmbeddingDimensionError(
            f"{profile.model_name} returned a {len(values)}-dimensional vector; profile "
            f"{profile.profile_key}/{profile.profile_version} requires exactly {profile.dimension}")
    vector = [float(v) for v in values]
    if not all(math.isfinite(v) for v in vector):
        raise EmbeddingError("the vector contains NaN or infinite values")
    if profile.normalization_policy == "L2":
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            raise EmbeddingError("a zero vector cannot be L2-normalized")
        vector = [v / norm for v in vector]
    return vector


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form; repr keeps full float precision."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


class GeminiEmbeddingProvider:
    """Google Gemini embeddings through the google-generativeai client already in the backend."""

    def __init__(self, api_key: str | None = None, client: Any = None):
        if client is None:
            import google.generativeai as client

            if api_key:
                client.configure(api_key=api_key)
        self._client = client

    def embed(self, texts, *, profile, task):
        if not texts:
            return []
        options = dict(profile.provider_options)
        response = self._client.embed_content(
            model=f"models/{profile.model_name}", content=list(texts), task_type=task, **options)
        vectors = response["embedding"]
        if texts and vectors and not isinstance(vectors[0], (list, tuple)):
            vectors = [vectors]  # a single text returns one flat vector
        if len(vectors) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} embeddings, received {len(vectors)}")
        metadata = {"provider": "google", "model": profile.model_name, "task_type": task, **options}
        return [ProviderEmbedding(vector, dict(metadata)) for vector in vectors]


class DeterministicFakeProvider:
    """Offline provider for tests and local runs: a stable pseudo-random vector per text."""

    def __init__(self, dimension: int | None = None):
        self.dimension = dimension
        self.calls: list[list[str]] = []

    def embed(self, texts, *, profile, task):
        self.calls.append(list(texts))
        dimension = self.dimension or profile.dimension
        results = []
        for text in texts:
            seed = hashlib.sha256(f"{task}:{text}".encode("utf-8")).digest()
            values = [((seed[i % 32] + i * 31) % 251) / 250.0 - 0.5 for i in range(dimension)]
            results.append(ProviderEmbedding(values, {"provider": "fake", "task_type": task}))
        return results
