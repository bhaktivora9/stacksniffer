"""The PROJECTING and SUMMARIZING pipeline stages, run after indexing.

It continues from wherever the analysis is: INDEXING (straight after indexing), PROJECTING or
SUMMARIZING (a resumed attempt), so a retry never repeats a finished step. Without a text
provider the analysis is projected and its communities detected, then it parks awaiting
SUMMARIZING until one is configured. With one, it ends SUCCEEDED (the worker moves it to READY).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from ..analysis_lifecycle import AWAITING_STAGE, AnalysisState, transition_analysis
from ..extraction.contracts import ExtractionResult
from .communities import detect_communities
from .llm import TextProvider
from .projection import project_analysis
from .summaries import summarize_communities

logger = logging.getLogger(__name__)


class GraphRAGStage:
    def __init__(self, connection_factory: Callable[[], Any], provider: TextProvider | None, *,
                 summary_workers: int = 4):
        self._connection_factory = connection_factory
        self._provider = provider
        self._summary_workers = summary_workers

    @property
    def resumable_stages(self) -> tuple[str, ...]:
        stages = (AnalysisState.PROJECTING.value,)
        return stages + ((AnalysisState.SUMMARIZING.value,) if self._provider is not None else ())

    async def __call__(self, result: ExtractionResult) -> str:
        return await asyncio.to_thread(self._run, result.analysis_id, result.attempt_id)

    def _run(self, analysis_id: UUID, attempt_id: UUID) -> str:
        started = time.perf_counter()
        status = self._status(analysis_id)
        if status == AnalysisState.INDEXING:
            self._transition(analysis_id, AnalysisState.INDEXING, AnalysisState.PROJECTING)
            status = AnalysisState.PROJECTING

        conn = self._connection_factory()
        try:
            snapshot, graph = project_analysis(conn, analysis_id)
            run = detect_communities(conn, snapshot, graph)
            repository = conn.fetch_one(
                "SELECT r.canonical_key, v.commit_sha FROM core.analysis a JOIN core.repository_version v "
                "ON v.id = a.repository_version_id JOIN core.repository r ON r.id = v.repository_id WHERE a.id = %s",
                str(analysis_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info("Projected analysis %s: %d nodes, %d edges (%d derived, %d unresolved), %d communities",
                    analysis_id, len(graph.nodes), len(graph.edges), graph.derived_edges, graph.unresolved,
                    len(run.communities))

        if self._provider is None:
            if status == AnalysisState.PROJECTING:
                logger.info("Analysis %s projected; no text provider is configured, parking it", analysis_id)
                return AWAITING_STAGE
            raise RuntimeError("a SUMMARIZING analysis needs a text provider")
        if status == AnalysisState.PROJECTING:
            self._transition(analysis_id, AnalysisState.PROJECTING, AnalysisState.SUMMARIZING)

        summaries = summarize_communities(self._connection_factory, run, graph, self._provider,
                                          repository=f"{repository[0]}@{repository[1][:12]}",
                                          workers=self._summary_workers)
        logger.info("Summarized analysis %s attempt %s: ready=%d reused=%d unsupported=%d tokens=%d/%d in %dms",
                    analysis_id, attempt_id, summaries.ready, summaries.reused, summaries.unsupported,
                    summaries.input_tokens, summaries.output_tokens, round((time.perf_counter() - started) * 1000))
        return "SUCCEEDED"

    def _status(self, analysis_id: UUID) -> AnalysisState:
        conn = self._connection_factory()
        try:
            return AnalysisState(conn.fetch_scalar("SELECT status FROM core.analysis WHERE id = %s", str(analysis_id)))
        finally:
            conn.rollback()
            conn.close()

    def _transition(self, analysis_id: UUID, current: AnalysisState, target: AnalysisState) -> None:
        conn = self._connection_factory()
        try:
            asyncio.run(transition_analysis(conn, analysis_id, current, target))
            conn.execute("UPDATE core.analysis SET last_completed_stage = %s WHERE id = %s", current.value,
                         str(analysis_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
