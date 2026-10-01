"""Persist profiles, chunks and embeddings; every write is idempotent.

- Registering a profile returns the existing row when the same configuration (fingerprint)
  is already registered; the same key and version with a different configuration is refused.
- Indexing an analysis reuses a chunk whose (analysis, profile, stable key, content hash) exists.
- Embedding reads only persisted chunks: a new embedding profile never needs the repository
  snapshot or structural extraction. One vector is kept per chunk, profile and chunk content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from uuid import UUID

from .chunker import ChunkEntity, build_chunks
from .embeddings import EmbeddingProvider, validate_vector, vector_literal
from .profiles import ChunkProfile, EmbeddingProfile, canonical_json

INDEXABLE_STATUSES = ("PARSED", "PARTIAL", "UNSUPPORTED")


class ProfileConflict(RuntimeError):
    """A profile key and version already registered with a different configuration."""


class SourceMismatch(RuntimeError):
    """The source handed to the chunker is not the content the analysis recorded."""


# --- profiles ------------------------------------------------------------------------------------


def _register(conn: Any, table: str, columns: dict[str, Any], profile) -> UUID:
    existing = conn.fetch_one(
        f"SELECT id, profile_key, profile_version FROM semantic.{table} WHERE configuration_fingerprint = %s",
        profile.fingerprint)
    if existing is not None:
        return existing[0]
    clash = conn.fetch_scalar(
        f"SELECT configuration_fingerprint FROM semantic.{table} WHERE profile_key = %s AND profile_version = %s",
        profile.profile_key, profile.profile_version)
    if clash is not None:
        raise ProfileConflict(f"{table} {profile.profile_key}/{profile.profile_version} is already registered with "
                              f"a different configuration; publish a new version instead")
    names = list(columns) + ["canonical_configuration", "configuration", "configuration_fingerprint"]
    placeholders = ["%s"] * len(columns) + ["%s", "%s::jsonb", "%s"]
    canonical = profile.canonical_configuration
    row = conn.fetch_one(
        f"INSERT INTO semantic.{table} ({', '.join(names)}) VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (configuration_fingerprint) DO NOTHING RETURNING id",
        *columns.values(), canonical, canonical, profile.fingerprint)
    if row is not None:
        return row[0]
    return conn.fetch_scalar(f"SELECT id FROM semantic.{table} WHERE configuration_fingerprint = %s",
                             profile.fingerprint)  # registered concurrently


def register_chunk_profile(conn: Any, profile: ChunkProfile) -> UUID:
    return _register(conn, "chunk_profile", {
        "profile_key": profile.profile_key, "profile_version": profile.profile_version,
        "chunking_strategy": profile.chunking_strategy, "tokenizer": profile.tokenizer,
        "max_tokens": profile.max_tokens, "overlap_policy": canonical_json(profile.overlap_policy),
        "symbol_boundary_policy": profile.symbol_boundary_policy,
        "included_entity_types": list(profile.included_entity_types),
        "include_generated": profile.include_generated, "include_vendored": profile.include_vendored,
        "code_version": profile.code_version,
    }, profile)


def register_embedding_profile(conn: Any, profile: EmbeddingProfile) -> UUID:
    return _register(conn, "embedding_profile", {
        "profile_key": profile.profile_key, "profile_version": profile.profile_version,
        "provider": profile.provider, "model_name": profile.model_name, "model_revision": profile.model_revision,
        "purpose": profile.purpose, "dimension": profile.dimension, "distance_metric": profile.distance_metric,
        "normalization_policy": profile.normalization_policy, "document_task": profile.document_task,
        "query_task": profile.query_task, "text_template": profile.text_template,
        "pooling_strategy": profile.pooling_strategy, "code_version": profile.code_version,
    }, profile)


# --- chunks --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkingResult:
    files: int
    skipped_files: int
    chunks: int
    created: int

    @property
    def reused(self) -> int:
        return self.chunks - self.created


def index_analysis_chunks(conn: Any, analysis_id: UUID, profile: ChunkProfile,
                          read_source: Callable[[str], bytes]) -> ChunkingResult:
    """Chunk every eligible file of an analysis. ``read_source(path)`` returns the snapshot's bytes,
    which must hash to the content the analysis recorded."""
    profile_id = register_chunk_profile(conn, profile)
    version_id = conn.fetch_scalar("SELECT repository_version_id FROM core.analysis WHERE id = %s", str(analysis_id))
    files = conn.fetch_all(
        "SELECT id, path, content_hash, is_generated, is_vendored, parse_status FROM core.source_file "
        "WHERE analysis_id = %s ORDER BY path", str(analysis_id))
    entities: dict[Any, list[tuple]] = {}
    for file_id, entity_id, stable_key, entity_type, start_line, end_line in conn.fetch_all(
            "SELECT file_id, id, stable_key, entity_type, start_line, end_line FROM core.entity "
            "WHERE analysis_id = %s AND file_id IS NOT NULL AND start_line IS NOT NULL", str(analysis_id)):
        entities.setdefault(file_id, []).append((entity_id, stable_key, entity_type, start_line, end_line))

    indexed = skipped = total = created = 0
    for file_id, path, recorded_hash, generated, vendored, status in files:
        if status not in INDEXABLE_STATUSES or (generated and not profile.include_generated) \
                or (vendored and not profile.include_vendored):
            skipped += 1
            continue
        source = read_source(path)
        if hashlib.sha256(source).hexdigest() != recorded_hash:
            raise SourceMismatch(f"{path}: the source does not match the analyzed content")
        file_entities = entities.get(file_id, [])
        ids = {stable_key: entity_id for entity_id, stable_key, *_ in file_entities}
        chunks = build_chunks(path, source, (ChunkEntity(key, kind, start, end)
                                             for _, key, kind, start, end in file_entities), profile)
        indexed += 1
        for chunk in chunks:
            row = conn.fetch_one(
                "INSERT INTO semantic.chunk (analysis_id, repository_version_id, file_id, entity_id, chunk_profile_id, "
                "stable_chunk_key, content, content_hash, token_count, ordinal, start_line, end_line, start_byte, "
                "end_byte, is_generated, is_vendored) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (analysis_id, chunk_profile_id, stable_chunk_key, content_hash) DO NOTHING RETURNING id",
                str(analysis_id), str(version_id), str(file_id),
                str(ids[chunk.entity_key]) if chunk.entity_key else None, str(profile_id),
                chunk.stable_chunk_key, chunk.content, chunk.content_hash, chunk.token_count, chunk.ordinal,
                chunk.start_line, chunk.end_line, chunk.start_byte, chunk.end_byte, generated, vendored)
            total += 1
            created += row is not None
    return ChunkingResult(files=indexed, skipped_files=skipped, chunks=total, created=created)


# --- embeddings ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingResult:
    embedded: int
    already_current: int


def embed_analysis_chunks(conn: Any, analysis_id: UUID, chunk_profile: ChunkProfile,
                          embedding_profile: EmbeddingProfile, provider: EmbeddingProvider,
                          *, batch_size: int = 64) -> EmbeddingResult:
    """Embed an analysis's chunks under a profile, reading only persisted chunks."""
    chunk_profile_id = conn.fetch_scalar(
        "SELECT id FROM semantic.chunk_profile WHERE configuration_fingerprint = %s", chunk_profile.fingerprint)
    if chunk_profile_id is None:
        raise LookupError(f"chunk profile {chunk_profile.profile_key}/{chunk_profile.profile_version} has no chunks")
    profile_id = register_embedding_profile(conn, embedding_profile)
    pending = conn.fetch_all(
        "SELECT c.id, c.content, c.content_hash FROM semantic.chunk c "
        "WHERE c.analysis_id = %s AND c.chunk_profile_id = %s AND NOT EXISTS ("
        "  SELECT 1 FROM semantic.embedding e WHERE e.chunk_id = c.id AND e.embedding_profile_id = %s"
        "  AND e.chunk_content_hash = c.content_hash) "
        "ORDER BY c.ordinal, c.stable_chunk_key", str(analysis_id), str(chunk_profile_id), str(profile_id))
    current = conn.fetch_scalar(
        "SELECT count(*) FROM semantic.chunk c JOIN semantic.embedding e ON e.chunk_id = c.id "
        "AND e.chunk_content_hash = c.content_hash AND e.embedding_profile_id = %s "
        "WHERE c.analysis_id = %s AND c.chunk_profile_id = %s", str(profile_id), str(analysis_id),
        str(chunk_profile_id))
    embedded = 0
    for batch in _batches(pending, batch_size):
        results = provider.embed([embedding_profile.render(content) for _, content, _ in batch],
                                 profile=embedding_profile, task=embedding_profile.document_task)
        if len(results) != len(batch):
            raise RuntimeError(f"provider returned {len(results)} vectors for {len(batch)} chunks")
        # Validate the whole batch before writing any of it.
        vectors = [validate_vector(result.values, embedding_profile) for result in results]
        for (chunk_id, _, content_hash), vector, result in zip(batch, vectors, results):
            metadata = {**result.metadata, "normalization": embedding_profile.normalization_policy}
            embedded += conn.execute(
                "INSERT INTO semantic.embedding (chunk_id, embedding_profile_id, chunk_content_hash, embedding, "
                "response_metadata) VALUES (%s, %s, %s, %s::vector(3072), %s::jsonb) "
                "ON CONFLICT (chunk_id, embedding_profile_id, chunk_content_hash) DO NOTHING",
                str(chunk_id), str(profile_id), content_hash, vector_literal(vector), json.dumps(metadata))
    return EmbeddingResult(embedded=embedded, already_current=current)


def _batches(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
