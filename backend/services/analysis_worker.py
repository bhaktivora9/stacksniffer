"""Durable worker primitives for queued analyses."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable
from uuid import UUID

try:
    from services.analysis_lifecycle import (
        FINISHED_STATES,
        AnalysisState,
        AnalysisTransitionError,
        requeue_failed_analysis,
        transition_allowed,
        transition_analysis,
    )
    from services.analysis_queue import claim_queued_analysis, finish_attempt
except ModuleNotFoundError:
    from backend.services.analysis_lifecycle import (
        FINISHED_STATES,
        AnalysisState,
        AnalysisTransitionError,
        requeue_failed_analysis,
        transition_allowed,
        transition_analysis,
    )
    from backend.services.analysis_queue import claim_queued_analysis, finish_attempt

logger = logging.getLogger(__name__)


class AnalysisWorkerError(RuntimeError):
    """Raised when a worker cannot advance an attempt safely."""


AttemptProcessor = Callable[[UUID, UUID], Awaitable[str]]

_OUTCOME_STATES = {
    "SUCCEEDED": AnalysisState.READY,
    "DEGRADED": AnalysisState.DEGRADED,
    "FAILED": AnalysisState.FAILED,
}


def claim_analysis_transactionally(connection_factory: Callable[[], Any]) -> tuple[UUID, UUID] | None:
    """Claim one QUEUED analysis and commit the claim before returning it."""
    conn = connection_factory()
    try:
        claimed = claim_queued_analysis(conn)
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def complete_attempt(
    connection_factory: Callable[[], Any],
    *,
    analysis_id: UUID,
    attempt_id: UUID,
    outcome: str,
    failure_code: str | None = None,
    failure_detail: dict[str, Any] | None = None,
) -> None:
    """Finish an attempt and move its analysis from wherever the pipeline left it."""
    target_state = _OUTCOME_STATES.get(outcome)
    if target_state is None:
        raise ValueError(f"invalid terminal attempt outcome: {outcome}")

    conn = connection_factory()
    try:
        current = conn.fetch_scalar(
            "SELECT status FROM core.analysis WHERE id = %s FOR UPDATE",
            str(analysis_id),
        )
        if current is None:
            raise AnalysisTransitionError(f"analysis {analysis_id} does not exist")
        current_state = AnalysisState(current)
        if not transition_allowed(current_state, target_state):
            raise AnalysisTransitionError(
                f"attempt outcome {outcome} is not reachable from {current_state.value}"
            )

        finish_attempt(
            conn,
            attempt_id,
            outcome,
            failure_stage=current_state.value if outcome == "FAILED" else None,
            failure_code=failure_code,
            failure_detail=failure_detail,
        )
        await transition_analysis(conn, analysis_id, current_state, target_state)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def recover_stale_attempts(
    connection_factory: Callable[[], Any],
    *,
    stale_after_seconds: float,
    max_attempts: int,
) -> list[UUID]:
    """Fail attempts left RUNNING by a worker that died, and requeue their analyses.

    A deploy or crash mid-attempt otherwise leaves the analysis in an active
    state forever. Each stale attempt is marked FAILED with WORKER_LOST; its
    analysis goes to FAILED and, while attempts remain, back to QUEUED so a
    worker retries it as a new attempt. Returns the requeued analysis ids.
    """
    conn = connection_factory()
    requeued: list[UUID] = []
    try:
        rows = conn.fetch_all(
            """
            SELECT aa.id, aa.analysis_id, aa.attempt_number, a.status
            FROM core.analysis_attempt AS aa
            JOIN core.analysis AS a ON a.id = aa.analysis_id
            WHERE aa.status = 'RUNNING'
              AND aa.started_at < NOW() - make_interval(secs => %s)
            FOR UPDATE OF aa, a SKIP LOCKED
            """,
            float(stale_after_seconds),
        )
        for attempt_id, analysis_id, attempt_number, status in rows or []:
            analysis_id = UUID(str(analysis_id))
            state = AnalysisState(status)
            finish_attempt(
                conn,
                UUID(str(attempt_id)),
                "FAILED",
                failure_stage=state.value,
                failure_code="WORKER_LOST",
                failure_detail={"message": f"no progress for {int(stale_after_seconds)}s; worker presumed lost"},
            )
            # An analysis that already finished keeps its result; only the dangling attempt is closed.
            action = "attempt closed"
            if state not in FINISHED_STATES:
                await transition_analysis(conn, analysis_id, state, AnalysisState.FAILED)
                action = "left FAILED"
                if attempt_number < max_attempts:
                    await requeue_failed_analysis(
                        conn, analysis_id, requested_by="worker-recovery", reason="worker_lost"
                    )
                    requeued.append(analysis_id)
                    action = "requeued"
            logger.warning(
                "Recovered stale attempt %s of analysis %s (was %s): %s",
                attempt_id, analysis_id, state.value, action,
            )
        conn.commit()
        return requeued
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _wait(stop_event: asyncio.Event | None, seconds: float) -> None:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_worker_loop(
    connection_factory: Callable[[], Any],
    process_attempt: AttemptProcessor,
    *,
    stop_event: asyncio.Event | None = None,
    poll_seconds: float = 1.0,
    stale_attempt_seconds: float | None = None,
    recovery_interval_seconds: float = 60.0,
    max_attempts: int = 3,
) -> None:
    """Consume queued analyses until stopped.

    ``process_attempt`` owns the ingestion/extraction pipeline. It receives an
    analysis already in ``INGESTING``, must advance it through the lifecycle
    with ``transition_analysis``, and return ``SUCCEEDED``, ``DEGRADED`` or
    ``FAILED``. Claims are committed before it runs, so two workers never own
    the same analysis. With ``stale_attempt_seconds`` set, attempts RUNNING
    longer than that are periodically recovered (see recover_stale_attempts).
    """
    last_recovery: float | None = None
    while stop_event is None or not stop_event.is_set():
        if stale_attempt_seconds and (
            last_recovery is None or time.monotonic() - last_recovery >= recovery_interval_seconds
        ):
            await recover_stale_attempts(
                connection_factory, stale_after_seconds=stale_attempt_seconds, max_attempts=max_attempts
            )
            last_recovery = time.monotonic()

        claimed = await asyncio.to_thread(claim_analysis_transactionally, connection_factory)
        if claimed is None:
            await _wait(stop_event, poll_seconds)
            continue

        analysis_id, attempt_id = claimed
        logger.info("Claimed analysis %s (attempt %s)", analysis_id, attempt_id)
        try:
            outcome = await process_attempt(analysis_id, attempt_id)
            await complete_attempt(
                connection_factory,
                analysis_id=analysis_id,
                attempt_id=attempt_id,
                outcome=outcome,
            )
            logger.info("Analysis %s attempt %s finished: %s", analysis_id, attempt_id, outcome)
        except Exception as exc:
            logger.warning("Analysis %s attempt %s failed: %s: %s", analysis_id, attempt_id, type(exc).__name__, exc)
            try:
                await complete_attempt(
                    connection_factory,
                    analysis_id=analysis_id,
                    attempt_id=attempt_id,
                    outcome="FAILED",
                    failure_code=type(exc).__name__,
                    failure_detail={"message": str(exc)},
                )
            except Exception as completion_error:
                raise AnalysisWorkerError(
                    f"attempt {attempt_id} failed and could not be finalized: {completion_error}"
                ) from exc


async def supervise_worker(
    connection_factory: Callable[[], Any],
    process_attempt: AttemptProcessor,
    *,
    stop_event: asyncio.Event,
    restart_delay_seconds: float = 5.0,
    **loop_options: Any,
) -> None:
    """Keep the worker loop alive for the life of the process.

    Database outages or a failed finalization raise out of run_worker_loop;
    without a supervisor that would silently stop all processing and leave
    new analyses QUEUED forever.
    """
    logger.info("Analysis worker started")
    while not stop_event.is_set():
        try:
            await run_worker_loop(connection_factory, process_attempt, stop_event=stop_event, **loop_options)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Analysis worker loop crashed; restarting in %.0fs", restart_delay_seconds)
            await _wait(stop_event, restart_delay_seconds)
    logger.info("Analysis worker stopped")
