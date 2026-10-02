"""Embedding providers and strict vector validation.

A provider returns raw vectors. `validate_vector` accepts one only when it has exactly the
profile's dimension and finite values; anything else raises. Nothing is truncated, padded or
coerced. The profile's normalization policy is applied explicitly and recorded with the vector.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

from google import genai

from .profiles import EmbeddingProfile


class EmbeddingError(RuntimeError):
    """The provider's output cannot be stored under the profile."""

    failure_code = "EMBEDDING_FAILED"


class EmbeddingDimensionError(EmbeddingError):
    failure_code = "EMBEDDING_DIMENSION_MISMATCH"


class EmbeddingProfileIncompatible(EmbeddingError):
    """The provider cannot produce vectors for the profile; nothing is requested or stored."""

    failure_code = "EMBEDDING_PROFILE_INCOMPATIBLE"


@dataclass(frozen=True)
class ProviderEmbedding:
    values: Sequence[float]
    metadata: dict = field(default_factory=dict)


class EmbeddingProvider(Protocol):
    """Provider-neutral interface: ``name`` must equal the profile's ``provider``."""

    name: str

    def embed(self, texts: Sequence[str], *, profile: EmbeddingProfile, task: str | None) -> list[ProviderEmbedding]:
        ...


def check_compatibility(provider: EmbeddingProvider, profile: EmbeddingProfile) -> None:
    """Refuse, before any request or write, a provider that is not the one the profile names."""
    name = getattr(provider, "name", None)
    if name != profile.provider:
        raise EmbeddingProfileIncompatible(
            f"profile {profile.profile_key}/{profile.profile_version} needs a {profile.provider!r} provider, "
            f"not {name!r}")


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


class EmbeddingRequestError(EmbeddingError):
    """The provider rejected the request (4xx other than rate limiting); retrying will not help."""

    failure_code = "EMBEDDING_REQUEST_REJECTED"


class EmbeddingRateLimited(EmbeddingError):
    """The provider's quota or rate limit was exhausted after the client's own retries."""

    failure_code = "EMBEDDING_RATE_LIMITED"


class EmbeddingProviderUnavailable(EmbeddingError):
    """A server error, network failure or timeout persisted through the client's retries."""

    failure_code = "EMBEDDING_PROVIDER_UNAVAILABLE"


# Gemini API limits (gemini-embedding-001): 2,048 input tokens per text, 100 texts per batch.
GEMINI_MAX_INPUT_TOKENS = 2048
GEMINI_MAX_BATCH = 100
# The chunker's conservative estimate (code averages ~4 UTF-8 bytes per token; we assume 3), so a
# text that passes cannot be silently truncated: the Developer API has no auto_truncate=False.
_MAX_INPUT_BYTES = GEMINI_MAX_INPUT_TOKENS * 3
_RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504)


class GeminiEmbeddingProvider:
    """Gemini embeddings through the google-genai SDK (`genai.Client`, `client.models.embed_content`).

    The profile's dimension is requested explicitly (`output_dimensionality`) and every returned
    vector is checked against it; provider defaults are never relied on. The client retries
    transient failures (timeouts, 408/429/5xx) with exponential backoff; what remains is raised as
    an EmbeddingError subclass whose message carries the status, never the input or credentials.
    Close the provider (or use it as a context manager) to release the client's HTTP connections.
    """

    name = "google"

    def __init__(self, api_key: str | None = None, *, client: Any = None, timeout_seconds: float = 60.0,
                 retry_attempts: int = 4):
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
                http_options=types.HttpOptions(
                    timeout=int(timeout_seconds * 1000),  # milliseconds
                    retry_options=types.HttpRetryOptions(
                        attempts=retry_attempts, initial_delay=1.0, max_delay=30.0, exp_base=2.0, jitter=1.0,
                        http_status_codes=list(_RETRYABLE_STATUS)),
                ))
        self._client = client

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def embed(self, texts, *, profile, task):
        texts = list(texts)
        for text in texts:
            if len(text.encode("utf-8")) > _MAX_INPUT_BYTES:
                raise EmbeddingRequestError(f"a text of {len(text.encode('utf-8'))} UTF-8 bytes may exceed "
                                            f"{GEMINI_MAX_INPUT_TOKENS} input tokens and would be truncated")
        results: list[ProviderEmbedding] = []
        for start in range(0, len(texts), GEMINI_MAX_BATCH):
            results.extend(self._embed_batch(texts[start:start + GEMINI_MAX_BATCH], profile, task))
        return results

    def _embed_batch(self, texts, profile, task) -> list[ProviderEmbedding]:
        from google.genai import errors, types

        config = types.EmbedContentConfig(task_type=task, output_dimensionality=profile.dimension)
        try:
            response = self._client.models.embed_content(model=profile.model_name, contents=texts, config=config)
        except errors.APIError as exc:
            raise _translate(exc) from exc
        except Exception as exc:  # network errors and timeouts from the HTTP layer
            if type(exc).__module__.startswith(("httpx", "httpcore", "requests", "urllib3")):
                raise EmbeddingProviderUnavailable(f"{type(exc).__name__} calling {profile.model_name}") from exc
            raise
        embeddings = list(response.embeddings or [])
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} embeddings, received {len(embeddings)}")
        metadata = {"provider": "google", "sdk": "google-genai", "model": profile.model_name, "task_type": task,
                    "output_dimensionality": profile.dimension}
        results = []
        for embedding in embeddings:
            values = list(embedding.values or [])
            if len(values) != profile.dimension:
                raise EmbeddingDimensionError(
                    f"{profile.model_name} returned a {len(values)}-dimensional vector although "
                    f"{profile.dimension} were requested")
            statistics = getattr(embedding, "statistics", None)
            if statistics is not None and getattr(statistics, "truncated", None):
                raise EmbeddingRequestError("the provider truncated an input; it would not represent the chunk")
            item = dict(metadata)
            if statistics is not None and getattr(statistics, "token_count", None) is not None:
                item["token_count"] = statistics.token_count
            results.append(ProviderEmbedding(values, item))
        return results


def _translate(exc: Any) -> EmbeddingError:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    detail = f"{code} {status or ''}".strip()
    if code == 429:
        return EmbeddingRateLimited(f"rate limited ({detail})")
    if isinstance(code, int) and 400 <= code < 500 and code != 408:
        return EmbeddingRequestError(f"request rejected ({detail})")
    return EmbeddingProviderUnavailable(f"provider unavailable ({detail})")


class OfflineEmbeddingProvider:
    """Deterministic offline provider (tests, local runs without a key): the same text and task
    always give the same 3,072-dimensional vector, derived from SHA-256. It carries no meaning;
    it exercises every path from chunk to stored vector without a network."""

    name = "offline"

    def __init__(self, dimension: int | None = None):
        self.dimension = dimension  # only to simulate a misbehaving provider in tests
        self.calls: list[list[str]] = []

    def embed(self, texts, *, profile, task):
        self.calls.append(list(texts))
        dimension = self.dimension or profile.dimension
        results = []
        for text in texts:
            seed = hashlib.sha256(f"{task}:{text}".encode("utf-8")).digest()
            values = [((seed[i % 32] + i * 31) % 251) / 250.0 - 0.5 for i in range(dimension)]
            results.append(ProviderEmbedding(values, {"provider": self.name, "model": profile.model_name,
                                                      "task_type": task}))
        return results

    def close(self) -> None:
        pass
