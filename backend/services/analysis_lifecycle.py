from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping
from uuid import UUID


class AnalysisState(str, Enum):
    """Values match the core.analysis status CHECK constraint."""

    QUEUED = "QUEUED"
    INGESTING = "INGESTING"
    EXTRACTING = "EXTRACTING"
    INDEXING = "INDEXING"
    PROJECTING = "PROJECTING"
    SUMMARIZING = "SUMMARIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class ReuseDecision(str, Enum):
    REUSE = "REUSE"
    RUN = "RUN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


ACTIVE_STATES = {
    AnalysisState.QUEUED,
    AnalysisState.INGESTING,
    AnalysisState.EXTRACTING,
    AnalysisState.INDEXING,
    AnalysisState.PROJECTING,
    AnalysisState.SUMMARIZING,
    AnalysisState.DEGRADED,
}

TERMINAL_STATES = {
    AnalysisState.READY,
    AnalysisState.FAILED,
}

VALID_TRANSITIONS = {
    AnalysisState.QUEUED: {AnalysisState.INGESTING},
    AnalysisState.INGESTING: {AnalysisState.EXTRACTING},
    AnalysisState.EXTRACTING: {AnalysisState.INDEXING},
    AnalysisState.INDEXING: {AnalysisState.READY, AnalysisState.PROJECTING},
    AnalysisState.PROJECTING: {AnalysisState.SUMMARIZING},
    AnalysisState.SUMMARIZING: {AnalysisState.READY},
    AnalysisState.DEGRADED: {AnalysisState.READY, AnalysisState.FAILED},
}


@dataclass(frozen=True)
class ReusePlan:
    download: ReuseDecision
    extract: ReuseDecision
    embed: ReuseDecision
    retrieve: ReuseDecision
    summarize: ReuseDecision
    evaluate: ReuseDecision

    def as_dict(self) -> dict[str, str]:
        return {
            "download": self.download.value,
            "extract": self.extract.value,
            "embed": self.embed.value,
            "retrieve": self.retrieve.value,
            "summarize": self.summarize.value,
            "evaluate": self.evaluate.value,
        }


class AnalysisTransitionError(Exception):
    """Raised when a state transition is invalid or a race condition occurs."""


class RetryAttemptError(Exception):
    """Raised when an analysis cannot be requeued for another attempt."""


# States after which the worker has nothing left to do for the current attempt.
FINISHED_STATES = {AnalysisState.READY, AnalysisState.DEGRADED, AnalysisState.FAILED}


def _coalesce(mapping: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key, default)
    return getattr(mapping, key, default)


def transition_allowed(current: AnalysisState, target: AnalysisState) -> bool:
    if current in TERMINAL_STATES:
        return False

    if target in {AnalysisState.DEGRADED, AnalysisState.FAILED}:
        return current in ACTIVE_STATES

    if current == AnalysisState.DEGRADED:
        return target in {AnalysisState.READY, AnalysisState.FAILED}

    return target in VALID_TRANSITIONS.get(current, set())


async def transition_analysis(
    conn: Any,
    analysis_id: UUID,
    expected_state: AnalysisState,
    target_state: AnalysisState,
) -> bool:
    if not transition_allowed(expected_state, target_state):
        raise AnalysisTransitionError(
            f"invalid transition: {expected_state.value} -> {target_state.value}"
        )

    result = conn.execute(
        """
        UPDATE core.analysis
        SET status = %s,
            completed_at = CASE WHEN %s THEN NOW() ELSE completed_at END
        WHERE id = %s AND status = %s
        """,
        target_state.value,
        target_state in FINISHED_STATES,
        str(analysis_id),
        expected_state.value,
    )

    rowcount = getattr(result, "rowcount", result)
    if rowcount == 0:
        raise AnalysisTransitionError(
            f"transition rejected: analysis {analysis_id} is not in {expected_state.value}"
        )
    return True


async def requeue_failed_analysis(
    conn: Any,
    analysis_id: UUID,
    *,
    requested_by: str | None = None,
    reason: str | None = None,
) -> None:
    """Return a FAILED analysis to QUEUED so the worker starts a new attempt.

    The next attempt row is created by the worker when it claims the analysis;
    the retry request itself is recorded in the analysis metadata.
    """
    result = conn.execute(
        """
        UPDATE core.analysis
        SET status = 'QUEUED',
            completed_at = NULL,
            metadata = metadata || jsonb_build_object('last_retry', %s::jsonb)
        WHERE id = %s AND status = 'FAILED'
        """,
        json.dumps({"requested_by": requested_by, "reason": reason or "retry"}),
        str(analysis_id),
    )
    if getattr(result, "rowcount", result) != 1:
        raise RetryAttemptError(f"analysis {analysis_id} is not FAILED and cannot be requeued")


def plan_analysis_work(repository_version: Any, requested_profiles: Mapping[str, Any] | None = None) -> ReusePlan:
    """Return a deterministic reuse plan for the repository analysis pipeline.

    The planner intentionally treats structural pipeline/version changes as invalidating
    downstream stages without re-running repository download if the exact commit is
    already available locally or in object storage. The structure below matches the
    spec's reuse table and keeps the decision explicit.
    """
    requested_profiles = requested_profiles or {}

    current_state = _coalesce(repository_version, "current_profile_state", {})
    if not isinstance(current_state, Mapping):
        current_state = {}

    commit_sha = _coalesce(repository_version, "commit_sha")
    structural_pipeline_version = _coalesce(repository_version, "structural_pipeline_version")
    checkout_available = bool(_coalesce(repository_version, "cached_checkout_available", False))

    current_commit = _coalesce(current_state, "commit_sha")
    current_structural_version = _coalesce(current_state, "structural_pipeline_version")

    if commit_sha is None or current_commit is None:
        return ReusePlan(
            download=ReuseDecision.RUN,
            extract=ReuseDecision.RUN,
            embed=ReuseDecision.RUN,
            retrieve=ReuseDecision.RUN,
            summarize=ReuseDecision.NOT_APPLICABLE,
            evaluate=ReuseDecision.RUN,
        )

    if commit_sha != current_commit:
        return ReusePlan(
            download=ReuseDecision.RUN,
            extract=ReuseDecision.RUN,
            embed=ReuseDecision.RUN,
            retrieve=ReuseDecision.RUN,
            summarize=ReuseDecision.NOT_APPLICABLE,
            evaluate=ReuseDecision.RUN,
        )

    if structural_pipeline_version != current_structural_version:
        return ReusePlan(
            download=ReuseDecision.REUSE if checkout_available else ReuseDecision.RUN,
            extract=ReuseDecision.RUN,
            embed=ReuseDecision.RUN,
            retrieve=ReuseDecision.RUN,
            summarize=ReuseDecision.NOT_APPLICABLE,
            evaluate=ReuseDecision.RUN,
        )

    if "embedding_profile" in requested_profiles and requested_profiles["embedding_profile"] != current_state.get("embedding_profile"):
        return ReusePlan(
            download=ReuseDecision.REUSE,
            extract=ReuseDecision.REUSE,
            embed=ReuseDecision.RUN,
            retrieve=ReuseDecision.RUN,
            summarize=ReuseDecision.NOT_APPLICABLE,
            evaluate=ReuseDecision.RUN,
        )

    if "chunk_schema_version" in requested_profiles and requested_profiles["chunk_schema_version"] != current_state.get("chunk_schema_version"):
        return ReusePlan(
            download=ReuseDecision.REUSE,
            extract=ReuseDecision.REUSE,
            embed=ReuseDecision.RUN,
            retrieve=ReuseDecision.RUN,
            summarize=ReuseDecision.NOT_APPLICABLE,
            evaluate=ReuseDecision.RUN,
        )

    if "retriever_version" in requested_profiles and requested_profiles["retriever_version"] != current_state.get("retriever_version"):
        return ReusePlan(
            download=ReuseDecision.REUSE,
            extract=ReuseDecision.REUSE,
            embed=ReuseDecision.REUSE,
            retrieve=ReuseDecision.RUN,
            summarize=ReuseDecision.NOT_APPLICABLE,
            evaluate=ReuseDecision.RUN,
        )

    if "prompt_profile" in requested_profiles and requested_profiles["prompt_profile"] != current_state.get("prompt_profile"):
        return ReusePlan(
            download=ReuseDecision.REUSE,
            extract=ReuseDecision.REUSE,
            embed=ReuseDecision.REUSE,
            retrieve=ReuseDecision.REUSE,
            summarize=ReuseDecision.RUN,
            evaluate=ReuseDecision.RUN,
        )

    return ReusePlan(
        download=ReuseDecision.REUSE,
        extract=ReuseDecision.REUSE,
        embed=ReuseDecision.REUSE,
        retrieve=ReuseDecision.REUSE,
        summarize=ReuseDecision.REUSE,
        evaluate=ReuseDecision.REUSE,
    )
