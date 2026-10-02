"""Persist profiles, chunks and embeddings; every write is idempotent.

- Registering a profile returns the existing row when the same configuration (fingerprint)
  is already registered; the same key and version with a different configuration is refused.
- Indexing an analysis reuses a chunk whose (analysis, profile, stable key, content hash) exists,
  and records why every other file produced no chunks (``semantic.chunk_exclusion``).
- Embedding reads only persisted chunks: a new embedding profile never needs the repository
  snapshot or structural extraction. One vector is kept per chunk, profile and chunk content,
  and each batch can be committed on its own, so a run that fails part-way resumes where it stopped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from uuid import UUID

from .chunker import CODE, ChunkEntity, build_chunks, file_kind
from .embeddings import EmbeddingError, EmbeddingProvider, check_compatibility, validate_vector, vector_literal
from .profiles import ChunkProfile, EmbeddingProfile, canonical_json
from .source_content import classify_source

INDEXABLE_STATUSES = ("PARSED", "PARTIAL", "UNSUPPORTED")
_FILE_BATCH = 100


class ProfileConflict(RuntimeError):
    """A profile key and version already registered with a different configuration."""


class SourceMismatch(RuntimeError):
    """The source handed to the chunker is not the content the analysis recorded."""

    failure_code = "SOURCE_CONTENT_MISMATCH"


class SourceContentUnavailable(RuntimeError):
    """The analysis was extracted before source retention; it must be analyzed again to be indexed."""

    failure_code = "SOURCE_CONTENT_UNAVAILABLE"


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
    exclusions: dict[str, int] = field(default_factory=dict)  # reason -> files

    @property
    def reused(self) -> int:
        return self.chunks - self.created


def _exclusion_reason(profile: ChunkProfile, path: str, language: str | None, generated: bool, vendored: bool,
                      parse_status: str, content_status: str | None) -> str | None:
    """Why a file is not chunked under the profile, before reading its content; None if it may be."""
    if generated and not profile.include_generated:
        return "GENERATED"
    if vendored and not profile.include_vendored:
        return "VENDORED"
    if parse_status not in INDEXABLE_STATUSES:
        return "NOT_EXTRACTED"
    if content_status in ("SECRET", "BINARY", "OVERSIZED"):
        return content_status
    kind = file_kind(path, language)
    if kind != CODE and kind not in profile.included_entity_types:
        return "KIND_NOT_INCLUDED"
    return None


def index_analysis_chunks(conn: Any, analysis_id: UUID, profile: ChunkProfile,
                          read_source: Callable[[str], bytes] | None = None) -> ChunkingResult:
    """Chunk every eligible file of an analysis from its retained source.

    ``read_source(path)`` replaces the retained source (tests and tools); its bytes must hash to
    the content the analysis recorded and pass the same retention policy.
    """
    profile_id = register_chunk_profile(conn, profile)
    version_id = conn.fetch_scalar("SELECT repository_version_id FROM core.analysis WHERE id = %s", str(analysis_id))
    files = conn.fetch_all(
        "SELECT id, path, language, content_hash, is_generated, is_vendored, parse_status, content_status "
        "FROM core.source_file WHERE analysis_id = %s ORDER BY path", str(analysis_id))
    if read_source is None and files and all(row[7] is None for row in files):
        raise SourceContentUnavailable(f"analysis {analysis_id} has no retained source; analyze it again")
    entities: dict[Any, list[tuple]] = {}
    for file_id, entity_id, stable_key, entity_type, start_line, end_line in conn.fetch_all(
            "SELECT file_id, id, stable_key, entity_type, start_line, end_line FROM core.entity "
            "WHERE analysis_id = %s AND file_id IS NOT NULL AND start_line IS NOT NULL", str(analysis_id)):
        entities.setdefault(file_id, []).append((entity_id, stable_key, entity_type, start_line, end_line))

    indexed = total = created = 0
    excluded: list[tuple[Any, str]] = []
    for start in range(0, len(files), _FILE_BATCH):
        group = files[start:start + _FILE_BATCH]
        retained = {} if read_source is not None else _retained_content(
            conn, [row[3] for row in group if row[7] == "RETAINED"])
        for file_id, path, language, recorded_hash, generated, vendored, status, content_status in group:
            reason = _exclusion_reason(profile, path, language, generated, vendored, status, content_status)
            source = None
            if reason is None:
                if read_source is not None:
                    source = read_source(path)
                    if hashlib.sha256(source).hexdigest() != recorded_hash:
                        raise SourceMismatch(f"{path}: the source does not match the analyzed content")
                    assessed = classify_source(path, source)
                    reason = None if assessed == "RETAINED" else assessed
                else:
                    source = retained.get(recorded_hash)
                    reason = None if source is not None else "CONTENT_NOT_RETAINED"
            if reason is not None:
                excluded.append((file_id, reason))
                continue
            file_entities = entities.get(file_id, [])
            ids = {stable_key: entity_id for entity_id, stable_key, *_ in file_entities}
            chunks = build_chunks(path, source, (ChunkEntity(key, kind, first, last)
                                                 for _, key, kind, first, last in file_entities),
                                  profile, language=language)
            if not chunks:
                excluded.append((file_id, "NO_INDEXABLE_CONTENT"))
                continue
            missing = {chunk.entity_key for chunk in chunks} - ids.keys()
            if missing:
                raise LookupError(f"{path}: chunks name entities the analysis does not have: {sorted(missing)[:3]}")
            indexed += 1
            total += len(chunks)
            created += _insert_chunks(conn, analysis_id, version_id, file_id, profile_id, generated, vendored,
                                      chunks, ids)
    _record_exclusions(conn, analysis_id, profile_id, excluded)
    reasons: dict[str, int] = {}
    for _, reason in excluded:
        reasons[reason] = reasons.get(reason, 0) + 1
    return ChunkingResult(files=indexed, skipped_files=len(excluded), chunks=total, created=created,
                          exclusions=dict(sorted(reasons.items())))


def _retained_content(conn: Any, hashes: list[str]) -> dict[str, bytes]:
    if not hashes:
        return {}
    return {content_hash: bytes(content) for content_hash, content in conn.fetch_all(
        "SELECT content_hash, content FROM core.source_content WHERE content_hash = ANY(%s::text[])",
        sorted(set(hashes)))}


def _insert_chunks(conn, analysis_id, version_id, file_id, profile_id, generated, vendored, chunks, ids) -> int:
    rows = [(str(ids[c.entity_key]), c.stable_chunk_key, c.chunk_kind, c.content, c.content_hash, c.token_count,
             c.ordinal, c.start_line, c.end_line, c.start_byte, c.end_byte,
             canonical_json({"section": c.section} if c.section else {})) for c in chunks]
    columns = [list(column) for column in zip(*rows)]
    return conn.execute(
        "INSERT INTO semantic.chunk (analysis_id, repository_version_id, file_id, entity_id, chunk_profile_id, "
        "stable_chunk_key, chunk_kind, content, content_hash, token_count, ordinal, start_line, end_line, "
        "start_byte, end_byte, is_generated, is_vendored, metadata) "
        "SELECT %s, %s, %s, t.entity_id, %s, t.stable_chunk_key, t.chunk_kind, t.content, t.content_hash, "
        "t.token_count, t.ordinal, t.start_line, t.end_line, t.start_byte, t.end_byte, %s, %s, t.metadata::jsonb "
        "FROM unnest(%s::uuid[], %s::text[], %s::text[], %s::text[], %s::text[], %s::int[], %s::int[], %s::int[], "
        "%s::int[], %s::int[], %s::int[], %s::text[]) AS t(entity_id, stable_chunk_key, chunk_kind, content, "
        "content_hash, token_count, ordinal, start_line, end_line, start_byte, end_byte, metadata) "
        "ON CONFLICT (analysis_id, chunk_profile_id, stable_chunk_key, content_hash) DO NOTHING",
        str(analysis_id), str(version_id), str(file_id), str(profile_id), generated, vendored, *columns)


def _record_exclusions(conn, analysis_id, profile_id, excluded: list[tuple[Any, str]]) -> None:
    if not excluded:
        return
    conn.execute(
        "INSERT INTO semantic.chunk_exclusion (analysis_id, chunk_profile_id, file_id, reason) "
        "SELECT %s, %s, t.file_id, t.reason FROM unnest(%s::uuid[], %s::text[]) AS t(file_id, reason) "
        "ON CONFLICT (analysis_id, chunk_profile_id, file_id) DO UPDATE SET reason = EXCLUDED.reason",
        str(analysis_id), str(profile_id), [str(file_id) for file_id, _ in excluded],
        [reason for _, reason in excluded])


# --- embeddings ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingResult:
    embedded: int
    already_current: int
    batches: int = 0


def embed_analysis_chunks(conn: Any, analysis_id: UUID, chunk_profile: ChunkProfile,
                          embedding_profile: EmbeddingProfile, provider: EmbeddingProvider,
                          *, batch_size: int = 64, commit_each_batch: bool = False,
                          stop: Any = None) -> EmbeddingResult:
    """Embed an analysis's chunks under a profile, reading only persisted chunks.

    The provider must be the profile's (checked before anything is requested or written), and a
    whole batch is validated before any of it is stored. With ``commit_each_batch`` every stored
    batch is committed, so after a provider failure a re-run embeds only what is still missing.
    """
    check_compatibility(provider, embedding_profile)
    chunk_profile_id = conn.fetch_scalar(
        "SELECT id FROM semantic.chunk_profile WHERE configuration_fingerprint = %s", chunk_profile.fingerprint)
    if chunk_profile_id is None:
        raise LookupError(f"chunk profile {chunk_profile.profile_key}/{chunk_profile.profile_version} has no chunks")
    profile_id = register_embedding_profile(conn, embedding_profile)
    if commit_each_batch:
        conn.commit()
    current = conn.fetch_scalar(
        "SELECT count(*) FROM semantic.chunk c JOIN semantic.embedding e ON e.chunk_id = c.id "
        "AND e.chunk_content_hash = c.content_hash AND e.embedding_profile_id = %s "
        "WHERE c.analysis_id = %s AND c.chunk_profile_id = %s", str(profile_id), str(analysis_id),
        str(chunk_profile_id))
    # Identifiers only: the chunk's path, kind and symbol (a documentation section's heading).
    pending = conn.fetch_all(
        "SELECT c.id, c.content, c.content_hash, c.chunk_kind, f.path, "
        "COALESCE(c.metadata->>'section', en.qualified_name, en.name, f.path) "
        "FROM semantic.chunk c JOIN core.source_file f ON f.id = c.file_id "
        "LEFT JOIN core.entity en ON en.id = c.entity_id "
        "WHERE c.analysis_id = %s AND c.chunk_profile_id = %s AND NOT EXISTS ("
        "  SELECT 1 FROM semantic.embedding e WHERE e.chunk_id = c.id AND e.embedding_profile_id = %s"
        "  AND e.chunk_content_hash = c.content_hash) "
        "ORDER BY c.ordinal, c.stable_chunk_key, c.id", str(analysis_id), str(chunk_profile_id), str(profile_id))
    embedded = batches = 0
    for batch in _batches(pending, batch_size):
        if stop is not None and stop.is_set():  # a threading.Event; stored batches stay
            break
        texts = [embedding_profile.render(content, path=path, kind=kind, symbol=symbol)
                 for _, content, _, kind, path, symbol in batch]
        results = provider.embed(texts, profile=embedding_profile, task=embedding_profile.document_task)
        if len(results) != len(batch):
            raise EmbeddingError(f"provider returned {len(results)} vectors for {len(batch)} chunks")
        # Validate the whole batch before writing any of it.
        vectors = [validate_vector(result.values, embedding_profile) for result in results]
        for (chunk_id, _, content_hash, *_), vector, result in zip(batch, vectors, results):
            metadata = {**result.metadata, "normalization": embedding_profile.normalization_policy}
            embedded += conn.execute(
                "INSERT INTO semantic.embedding (chunk_id, embedding_profile_id, chunk_content_hash, embedding, "
                "response_metadata) VALUES (%s, %s, %s, %s::vector(3072), %s::jsonb) "
                "ON CONFLICT (chunk_id, embedding_profile_id, chunk_content_hash) DO NOTHING",
                str(chunk_id), str(profile_id), content_hash, vector_literal(vector), json.dumps(metadata))
        if commit_each_batch:
            conn.commit()
        batches += 1
    return EmbeddingResult(embedded=embedded, already_current=current, batches=batches)


def _batches(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
