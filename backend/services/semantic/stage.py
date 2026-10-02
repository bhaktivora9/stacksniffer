"""The INDEXING pipeline stage: chunk an extracted analysis and embed its chunks.

It runs as ``StructuralExtractionStage``'s next stage, both right after extraction and when the
worker resumes a parked analysis under a new attempt. It reads only what extraction persisted
(the canonical IR and the retained source), never the repository, so a resume needs no
acquisition. Every step is idempotent: identical content and profiles reuse their chunks and
vectors, and embedding commits batch by batch, so a retry after a provider failure embeds only
what is still missing. When it is done the analysis parks awaiting PROJECTING.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from uuid import UUID

from ..analysis_lifecycle import AWAITING_STAGE
from ..extraction.contracts import ExtractionResult
from .embeddings import EmbeddingProvider, check_compatibility
from .profiles import DEFAULT_CHUNK_PROFILE, DEFAULT_EMBEDDING_PROFILE, ChunkProfile, EmbeddingProfile
from .store import embed_analysis_chunks, index_analysis_chunks

logger = logging.getLogger(__name__)


class IndexingCancelled(RuntimeError):
    failure_code = "INDEXING_CANCELLED"


class SemanticIndexingStage:
    """``NextStage`` for ``StructuralExtractionStage``; returns AWAITING_STAGE (awaiting PROJECTING)."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        provider: EmbeddingProvider,
        *,
        chunk_profile: ChunkProfile = DEFAULT_CHUNK_PROFILE,
        embedding_profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE,
        batch_size: int = 64,
    ):
        check_compatibility(provider, embedding_profile)  # a misconfiguration fails at startup
        self._connection_factory = connection_factory
        self._provider = provider
        self._chunk_profile = chunk_profile
        self._embedding_profile = embedding_profile
        self._batch_size = batch_size

    @property
    def embedding_profile(self) -> EmbeddingProfile:
        return self._embedding_profile

    async def __call__(self, result: ExtractionResult) -> str:
        stop = threading.Event()
        # The thread outlives a task cancellation, so signal it and wait for its current batch.
        work = asyncio.ensure_future(asyncio.to_thread(self._index, result.analysis_id, result.attempt_id, stop))
        try:
            metrics = await asyncio.shield(work)
        except asyncio.CancelledError:
            stop.set()
            with suppress(BaseException):
                await work
            raise
        logger.info("Indexed analysis %s attempt %s: %s", result.analysis_id, result.attempt_id, metrics)
        return AWAITING_STAGE

    def _index(self, analysis_id: UUID, attempt_id: UUID, stop: threading.Event) -> dict[str, Any]:
        started = time.perf_counter()
        chunking = self._in_transaction(
            lambda conn: index_analysis_chunks(conn, analysis_id, self._chunk_profile))
        if stop.is_set():
            raise IndexingCancelled(f"indexing of analysis {analysis_id} was cancelled")
        embedding = self._in_transaction(lambda conn: embed_analysis_chunks(
            conn, analysis_id, self._chunk_profile, self._embedding_profile, self._provider,
            batch_size=self._batch_size, commit_each_batch=True, stop=stop))
        if stop.is_set():
            raise IndexingCancelled(f"indexing of analysis {analysis_id} was cancelled")
        metrics = {
            "chunk_profile": f"{self._chunk_profile.profile_key}/{self._chunk_profile.profile_version}",
            "chunk_profile_fingerprint": self._chunk_profile.fingerprint,
            "embedding_profile": f"{self._embedding_profile.profile_key}/{self._embedding_profile.profile_version}",
            "embedding_profile_fingerprint": self._embedding_profile.fingerprint,
            "files_indexed": chunking.files, "files_excluded": chunking.skipped_files,
            "exclusions": chunking.exclusions, "chunks": chunking.chunks, "chunks_created": chunking.created,
            "chunks_reused": chunking.reused, "vectors_created": embedding.embedded,
            "vectors_reused": embedding.already_current, "embedding_batches": embedding.batches,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
        self._in_transaction(lambda conn: record_indexing_metrics(conn, analysis_id, attempt_id, metrics))
        return metrics

    def _in_transaction(self, work: Callable[[Any], Any]) -> Any:
        conn = self._connection_factory()
        try:
            value = work(conn)
            conn.commit()
            return value
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def record_indexing_metrics(conn: Any, analysis_id: UUID, attempt_id: UUID, metrics: dict[str, Any]) -> None:
    """Keep metrics per attempt, like extraction's, so a retry never overwrites an earlier attempt's."""
    conn.execute(
        """
        UPDATE core.analysis
        SET metadata = metadata || jsonb_build_object(
            'indexing',
            COALESCE(metadata->'indexing', '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb)
        )
        WHERE id = %s
        """,
        str(attempt_id), json.dumps(metrics), str(analysis_id),
    )
