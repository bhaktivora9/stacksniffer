"""The analysis pipeline run by the worker for each claimed attempt.

The worker hands over an analysis already in INGESTING. The pipeline must
advance it with ``transition_analysis`` and return SUCCEEDED, DEGRADED or
FAILED; raising marks the attempt FAILED with the exception as failure detail.
"""

from __future__ import annotations

from uuid import UUID


class PipelineNotImplemented(RuntimeError):
    """No ingestion stages exist yet, so an attempt cannot produce a result."""


async def run_analysis_pipeline(analysis_id: UUID, attempt_id: UUID) -> str:
    # Fail explicitly rather than leave analyses waiting on stages that do not exist.
    # Replace with materialization -> extraction -> indexing as those stages land.
    raise PipelineNotImplemented(
        "repository ingestion is not implemented yet; the analysis was accepted but cannot be processed"
    )
