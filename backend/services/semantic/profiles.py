"""Versioned, fingerprinted chunk and embedding profiles.

A profile's configuration is serialized to canonical JSON (sorted keys, no whitespace, UTF-8)
and fingerprinted with SHA-256. PostgreSQL recomputes the fingerprint from the stored canonical
text, and a profile becomes immutable once a chunk (or vector) uses it, so any change to how
chunks or vectors are produced means a new profile version.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# The one physical dimension of semantic.embedding.embedding in R1 (vector(3072)).
R1_EMBEDDING_DIMENSION = 3072

# Declarations, FILE (code outside any included declaration), and whole configuration and
# documentation files, which are chunked as their FILE entity.
ENTITY_TYPES = frozenset({"CLASS", "INTERFACE", "FUNCTION", "METHOD", "FILE", "CONFIGURATION", "DOCUMENTATION"})
SYMBOL_BOUNDARY_POLICIES = frozenset({"innermost_declaration"})
DISTANCE_METRICS = frozenset({"COSINE", "L2", "INNER_PRODUCT"})
NORMALIZATION_POLICIES = frozenset({"NONE", "L2"})
# Document templates may name these; queries are embedded as the bare question.
TEMPLATE_FIELDS = ("path", "kind", "symbol", "text")
_TEMPLATE_FIELD = re.compile(r"\{([a-z_]+)\}")


class ProfileError(ValueError):
    """A profile that cannot be used as specified."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChunkProfile:
    profile_key: str
    profile_version: int
    chunking_strategy: str
    tokenizer: str
    max_tokens: int
    overlap_lines: int
    symbol_boundary_policy: str
    included_entity_types: tuple[str, ...]
    include_generated: bool
    include_vendored: bool
    code_version: str

    def __post_init__(self):
        object.__setattr__(self, "included_entity_types", tuple(sorted(set(self.included_entity_types))))
        if not self.profile_key.strip() or self.profile_version < 1:
            raise ProfileError("a chunk profile needs a key and a positive version")
        if self.max_tokens < 1 or not 0 <= self.overlap_lines:
            raise ProfileError("max_tokens must be positive and overlap_lines non-negative")
        if not self.included_entity_types or not set(self.included_entity_types) <= ENTITY_TYPES:
            raise ProfileError(f"included entity types must be a non-empty subset of {sorted(ENTITY_TYPES)}")
        if self.symbol_boundary_policy not in SYMBOL_BOUNDARY_POLICIES:
            raise ProfileError(f"unknown symbol-boundary policy {self.symbol_boundary_policy!r}")

    @property
    def overlap_policy(self) -> dict:
        return {"unit": "lines", "lines": self.overlap_lines}

    def configuration(self) -> dict:
        return {
            "profile_key": self.profile_key, "profile_version": self.profile_version,
            "chunking_strategy": self.chunking_strategy, "tokenizer": self.tokenizer,
            "max_tokens": self.max_tokens, "overlap_policy": self.overlap_policy,
            "symbol_boundary_policy": self.symbol_boundary_policy,
            "included_entity_types": list(self.included_entity_types),
            "include_generated": self.include_generated, "include_vendored": self.include_vendored,
            "code_version": self.code_version,
        }

    @property
    def canonical_configuration(self) -> str:
        return canonical_json(self.configuration())

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.canonical_configuration)


@dataclass(frozen=True)
class EmbeddingProfile:
    profile_key: str
    profile_version: int
    provider: str
    model_name: str
    model_revision: str
    dimension: int
    distance_metric: str
    normalization_policy: str
    code_version: str
    document_task: str | None = None
    query_task: str | None = None
    text_template: str = "{text}"
    pooling_strategy: str | None = None
    purpose: str = "RETRIEVAL"
    provider_options: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.profile_key.strip() or self.profile_version < 1:
            raise ProfileError("an embedding profile needs a key and a positive version")
        if not self.model_revision.strip():
            raise ProfileError("an embedding profile needs an immutable model revision")
        if self.dimension != R1_EMBEDDING_DIMENSION:
            raise ProfileError(f"R1 stores {R1_EMBEDDING_DIMENSION}-dimensional vectors; this profile declares "
                               f"{self.dimension}. Vectors are never truncated, padded or coerced.")
        if self.distance_metric not in DISTANCE_METRICS or self.normalization_policy not in NORMALIZATION_POLICIES:
            raise ProfileError("unknown distance metric or normalization policy")
        if "{text}" not in self.text_template:
            raise ProfileError("the text template must contain {text}")
        unknown = set(_TEMPLATE_FIELD.findall(self.text_template)) - set(TEMPLATE_FIELDS)
        if unknown:
            raise ProfileError(f"unknown template fields {sorted(unknown)}; allowed: {list(TEMPLATE_FIELDS)}")

    def configuration(self) -> dict:
        return {
            "profile_key": self.profile_key, "profile_version": self.profile_version,
            "provider": self.provider, "model_name": self.model_name, "model_revision": self.model_revision,
            "dimension": self.dimension, "distance_metric": self.distance_metric,
            "normalization_policy": self.normalization_policy, "document_task": self.document_task,
            "query_task": self.query_task, "text_template": self.text_template,
            "pooling_strategy": self.pooling_strategy, "purpose": self.purpose, "code_version": self.code_version,
            "provider_options": self.provider_options,
        }

    @property
    def canonical_configuration(self) -> str:
        return canonical_json(self.configuration())

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.canonical_configuration)

    def render(self, text: str, *, path: str = "", kind: str = "", symbol: str = "") -> str:
        """The document text sent to the provider: the chunk plus its minimal identifiers."""
        fields = {"path": path, "kind": kind, "symbol": symbol, "text": text}
        return _TEMPLATE_FIELD.sub(lambda m: fields[m.group(1)], self.text_template)


DEFAULT_CHUNK_PROFILE = ChunkProfile(
    profile_key="symbol-windows",
    profile_version=1,
    chunking_strategy="symbol_windows",
    # Conservative estimate (code averages ~4 UTF-8 bytes per Gemini token), so a chunk never
    # exceeds the model's input limit; the exact tokenizer is not available offline.
    tokenizer="utf8_bytes_div_3_ceil",
    max_tokens=512,
    overlap_lines=3,
    symbol_boundary_policy="innermost_declaration",
    included_entity_types=("CLASS", "INTERFACE", "FUNCTION", "METHOD", "FILE", "CONFIGURATION", "DOCUMENTATION"),
    include_generated=False,
    include_vendored=False,
    code_version="stacksniffer-chunker/2",
)

# Identifiers only: enough to tell two similar snippets apart, never more source text.
DOCUMENT_TEMPLATE = "path: {path}\nkind: {kind}\nsymbol: {symbol}\n\n{text}"

DEFAULT_EMBEDDING_PROFILE = EmbeddingProfile(
    profile_key="gemini-embedding-001-3072",
    profile_version=1,
    provider="google",
    model_name="gemini-embedding-001",
    # Gemini's embedding models are versioned by name; "-001" is the immutable revision.
    model_revision="gemini-embedding-001",
    dimension=R1_EMBEDDING_DIMENSION,
    distance_metric="COSINE",
    normalization_policy="L2",
    document_task="RETRIEVAL_DOCUMENT",
    query_task="RETRIEVAL_QUERY",
    text_template=DOCUMENT_TEMPLATE,
    code_version="stacksniffer-embedder/1",
    provider_options={"output_dimensionality": R1_EMBEDDING_DIMENSION},
)

# The deterministic offline provider (tests and local runs without a key); never mixed with
# Gemini vectors because profiles, not providers, key every stored vector.
OFFLINE_EMBEDDING_PROFILE = EmbeddingProfile(
    profile_key="offline-sha256-3072",
    profile_version=1,
    provider="offline",
    model_name="stacksniffer-offline-hash",
    model_revision="sha256-v1",
    dimension=R1_EMBEDDING_DIMENSION,
    distance_metric="COSINE",
    normalization_policy="L2",
    document_task="RETRIEVAL_DOCUMENT",
    query_task="RETRIEVAL_QUERY",
    text_template=DOCUMENT_TEMPLATE,
    code_version="stacksniffer-embedder/1",
)
