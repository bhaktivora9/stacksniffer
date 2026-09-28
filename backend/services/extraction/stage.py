"""The structural pipeline stage: extract canonical IR, record metrics, hand off the result."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any
from uuid import UUID

from ..analysis_lifecycle import AWAITING_STAGE, AnalysisState, transition_analysis
from .contracts import ExtractionContext, ExtractionResult
from .extractor import ExtractionStore, RepositoryExtractor
from .registry import AnalyzerRegistry
from .store import PostgresExtractionStore, record_extraction_metrics

logger = logging.getLogger(__name__)

# Receives the analysis in INDEXING; returns SUCCEEDED, DEGRADED, FAILED or AWAITING_STAGE.
# Without one, an extracted analysis is parked in AWAITING_STAGE (awaiting INDEXING) and
# resumed by the worker once a pipeline with an indexing stage is deployed.
NextStage = Callable[[ExtractionResult], Awaitable[str]]


class ExtractionMetricsMissing(RuntimeError):
    """A resume was requested but the extraction it would resume from was never recorded."""


class StructuralExtractionStage:
    """``StructuralStage`` for ``AnalysisPipeline``; the snapshot is released after it returns."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        registry: AnalyzerRegistry | None = None,
        *,
        next_stage: NextStage | None = None,
        batch_size: int = 100,
        store_factory: Callable[[UUID, UUID], ExtractionStore] | None = None,
    ):
        self._connection_factory = connection_factory
        self._extractor = RepositoryExtractor(registry or AnalyzerRegistry.default(), batch_size=batch_size)
        self._next_stage = next_stage
        self._store_factory = store_factory or (
            lambda analysis_id, attempt_id: PostgresExtractionStore(connection_factory, analysis_id, attempt_id))

    async def __call__(self, context: ExtractionContext) -> str:
        store = self._store_factory(context.analysis_id, context.attempt_id)
        # The thread outlives a task cancellation, so signal it and wait before the snapshot goes away.
        work = asyncio.ensure_future(asyncio.to_thread(self._extractor.extract, context, store))
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            context.cancellation_token.cancel("task_cancelled")
            with suppress(BaseException):
                await work
            raise
        context.cancellation_token.raise_if_cancelled()
        await self._finish_extraction(result, advance=self._next_stage is not None)
        logger.info(
            "Extracted analysis %s attempt %s: files=%d statuses=%s entities=%d relationships=%d errors=%d duration_ms=%d",
            result.analysis_id, result.attempt_id, result.files_total, dict(result.files_by_status),
            result.entities, result.relationships, result.errors, round(result.duration_seconds * 1000),
        )
        if self._next_stage is None:
            logger.info("Analysis %s extracted; no indexing stage is deployed, parking it", result.analysis_id)
            return AWAITING_STAGE
        return await self._next_stage(result)

    @property
    def resumable_stages(self) -> tuple[str, ...]:
        """Stages this stage can continue a parked analysis at."""
        return (AnalysisState.INDEXING.value,) if self._next_stage is not None else ()

    async def resume(self, analysis_id: UUID, attempt_id: UUID) -> str:
        """Continue a parked analysis after its completed extraction: no acquisition, no re-extraction.

        The worker has already moved it AWAITING_STAGE -> INDEXING under a new attempt;
        the result is rebuilt from the metrics the extracting attempt recorded.
        """
        if self._next_stage is None:
            raise ExtractionMetricsMissing("no indexing stage is registered to resume into")
        conn = self._connection_factory()
        try:
            # The latest attempt that actually extracted; later resumed attempts record no extraction.
            metrics = conn.fetch_scalar(
                """
                SELECT extraction.value
                FROM core.analysis AS a
                CROSS JOIN LATERAL jsonb_each(COALESCE(a.metadata->'extractions', '{}'::jsonb)) AS extraction
                JOIN core.analysis_attempt AS aa ON aa.id::text = extraction.key
                WHERE a.id = %s
                ORDER BY aa.attempt_number DESC
                LIMIT 1
                """,
                str(analysis_id),
            )
            if metrics is None:
                raise ExtractionMetricsMissing(f"analysis {analysis_id} has no recorded extraction to resume from")
            result = ExtractionResult.from_metrics(analysis_id, attempt_id, metrics)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info("Analysis %s attempt %s resumed after extraction", analysis_id, attempt_id)
        return await self._next_stage(result)

    async def _finish_extraction(self, result: ExtractionResult, *, advance: bool) -> None:
        conn = self._connection_factory()
        try:
            record_extraction_metrics(conn, result)
            if advance:
                await transition_analysis(conn, result.analysis_id, AnalysisState.EXTRACTING, AnalysisState.INDEXING)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
