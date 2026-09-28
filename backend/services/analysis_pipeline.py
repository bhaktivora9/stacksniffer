"""The analysis pipeline run by the worker for each claimed attempt.

The worker hands over an analysis already in INGESTING. The pipeline must
advance it with ``transition_analysis`` and return SUCCEEDED, DEGRADED or
FAILED; raising marks the attempt FAILED with the exception as failure detail
(typed errors supply their own ``failure_code`` and ``failure_detail``).

Each attempt acquires the repository exactly once. The snapshot lives only for
the duration of the attempt and is handed to the structural stage; everything
after structural extraction works from persisted output, so semantic, prompt,
retriever or evaluation profile changes never reacquire the repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

try:
    from services.analysis_lifecycle import AnalysisState, transition_analysis
    from services.analysis_persistence import run_in_transaction
    from services.analysis_queue import AttemptOwnershipLost, attempt_is_owned
    from services.extraction.contracts import (
        CancellationToken,
        ExtractionCancelled,
        ExtractionContext,
    )
    from services.repository_acquisition import (
        AcquisitionCancelled,
        AcquisitionRequest,
        RepositoryAcquirer,
        RepositorySnapshot,
    )
except ModuleNotFoundError:
    from backend.services.analysis_lifecycle import AnalysisState, transition_analysis
    from backend.services.analysis_persistence import run_in_transaction
    from backend.services.analysis_queue import AttemptOwnershipLost, attempt_is_owned
    from backend.services.extraction.contracts import (
        CancellationToken,
        ExtractionCancelled,
        ExtractionContext,
    )
    from backend.services.repository_acquisition import (
        AcquisitionCancelled,
        AcquisitionRequest,
        RepositoryAcquirer,
        RepositorySnapshot,
    )

logger = logging.getLogger(__name__)

_OWNERSHIP_LOST = "ownership_lost"


class PipelineNotImplemented(RuntimeError):
    """A pipeline stage does not exist yet, so an attempt cannot produce a result."""


class AnalysisTargetMissing(RuntimeError):
    """The claimed analysis no longer resolves to a repository version."""


@dataclass(frozen=True)
class AnalysisTarget:
    analysis_id: UUID
    attempt_id: UUID
    canonical_repository_key: str
    commit_sha: str
    # The status the attempt was claimed into: INGESTING for a new run, the awaited stage for a resume.
    status: str = "INGESTING"


# Receives the analysis in EXTRACTING; returns SUCCEEDED, DEGRADED or FAILED.
StructuralStage = Callable[[ExtractionContext], Awaitable[str]]


async def structural_extraction_not_implemented(context: ExtractionContext) -> str:
    # Fail explicitly rather than leave analyses waiting on a stage that does not exist.
    # SS-BE-201 replaces this with the Tree-sitter analyzer framework.
    raise PipelineNotImplemented(
        "structural extraction is not implemented yet; the repository snapshot was acquired and discarded"
    )


def load_analysis_target(conn: Any, analysis_id: UUID, attempt_id: UUID) -> AnalysisTarget:
    row = conn.fetch_one(
        """
        SELECT r.canonical_key, rv.commit_sha, a.status
        FROM core.analysis AS a
        JOIN core.repository_version AS rv ON rv.id = a.repository_version_id
        JOIN core.repository AS r ON r.id = rv.repository_id
        WHERE a.id = %s
        """,
        str(analysis_id),
    )
    if row is None:
        raise AnalysisTargetMissing(f"analysis {analysis_id} has no repository version")
    if isinstance(row, (tuple, list)):
        key, sha, status = row[0], row[1], (row[2] if len(row) > 2 else None)
    else:
        key, sha, status = row["canonical_key"], row["commit_sha"], row.get("status")
    return AnalysisTarget(analysis_id, attempt_id, str(key), str(sha), str(status or "INGESTING"))


def record_acquisition(conn: Any, snapshot: RepositorySnapshot) -> None:
    """Keep acquisition evidence per attempt, so a retry never overwrites an earlier one."""
    conn.execute(
        """
        UPDATE core.analysis
        SET metadata = metadata || jsonb_build_object(
            'acquisitions',
            COALESCE(metadata->'acquisitions', '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb)
        )
        WHERE id = %s
        """,
        str(snapshot.attempt_id),
        json.dumps(snapshot.provenance()),
        str(snapshot.analysis_id),
    )


class AnalysisPipeline:
    """Callable ``AttemptProcessor``: acquisition, then the structural stage."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        acquirer: RepositoryAcquirer,
        *,
        structural_stage: StructuralStage = structural_extraction_not_implemented,
        ownership_check_seconds: float = 5.0,
    ):
        self._connection_factory = connection_factory
        self._acquirer = acquirer
        self._structural_stage = structural_stage
        self._ownership_check_seconds = ownership_check_seconds

    async def __call__(self, analysis_id: UUID, attempt_id: UUID) -> str:
        target = await asyncio.to_thread(self._read, load_analysis_target, analysis_id, attempt_id)
        if target.status != AnalysisState.INGESTING.value:
            # Claimed out of AWAITING_STAGE into the awaited stage: the structural output for this
            # analysis identity is already persisted, so neither acquisition nor extraction repeats.
            return await self._structural_stage.resume(analysis_id, attempt_id)
        request = AcquisitionRequest(
            canonical_repository_key=target.canonical_repository_key,
            commit_sha=target.commit_sha,
            analysis_id=analysis_id,
            attempt_id=attempt_id,
        )
        token = CancellationToken()
        watcher = asyncio.create_task(self._watch_ownership(target, token))
        try:
            async with self._acquirer.acquire(request, cancel_event=token.event) as snapshot:
                await self._enter_extraction(snapshot)
                return await self._structural_stage(ExtractionContext(
                    analysis_id=analysis_id,
                    attempt_id=attempt_id,
                    checkout_path=snapshot.root,
                    commit_sha=snapshot.commit_sha,
                    cancellation_token=token,
                    files=snapshot.files,
                ))
        except (AcquisitionCancelled, ExtractionCancelled) as exc:
            if token.reason == _OWNERSHIP_LOST:
                raise AttemptOwnershipLost(f"attempt {attempt_id} is no longer owned by this worker") from exc
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    @property
    def resumable_stages(self) -> tuple[str, ...]:
        """Awaited stages this pipeline can continue; the worker only resumes analyses waiting for these."""
        return tuple(getattr(self._structural_stage, "resumable_stages", ()))

    def _read(self, operation: Callable[..., Any], *args: Any) -> Any:
        return run_in_transaction(self._connection_factory, lambda conn: operation(conn, *args))

    async def _enter_extraction(self, snapshot: RepositorySnapshot) -> None:
        conn = self._connection_factory()
        try:
            record_acquisition(conn, snapshot)
            await transition_analysis(conn, snapshot.analysis_id, AnalysisState.INGESTING, AnalysisState.EXTRACTING)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def _watch_ownership(self, target: AnalysisTarget, token: CancellationToken) -> None:
        """Cancel acquisition or extraction once stale recovery (or anything else) closes this attempt."""
        while True:
            await asyncio.sleep(self._ownership_check_seconds)
            try:
                owned = await asyncio.to_thread(
                    self._read, attempt_is_owned, target.analysis_id, target.attempt_id
                )
            except Exception:
                logger.warning("Ownership check failed for attempt %s; will retry", target.attempt_id, exc_info=True)
                continue
            if not owned:
                logger.warning(
                    "Analysis %s attempt %s lost ownership; cancelling", target.analysis_id, target.attempt_id
                )
                token.cancel(_OWNERSHIP_LOST)
                return
