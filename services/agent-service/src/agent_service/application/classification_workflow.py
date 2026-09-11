"""Agent-owned classification contract and run state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from agent_service.domain.classification_contract import ClassificationContract


class ClassificationRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class ClassificationRun:
    classification_run_id: str
    analysis_id: str
    classification_contract_id: str
    status: ClassificationRunStatus = ClassificationRunStatus.QUEUED

    def supersede(self) -> "ClassificationRun":
        return replace(self, status=ClassificationRunStatus.SUPERSEDED)


class ClassificationWorkflow:
    def __init__(self, current_contract: ClassificationContract) -> None:
        self._current_contract = current_contract

    @property
    def current_contract(self) -> ClassificationContract:
        return self._current_contract

    def rotate_contract(self, contract: ClassificationContract) -> ClassificationContract:
        if not contract.supersedes(self._current_contract):
            raise ValueError("classification contract must change when rotating")
        self._current_contract = contract
        return contract

    def start_run(self, classification_run_id: str, analysis_id: str) -> ClassificationRun:
        return ClassificationRun(
            classification_run_id=classification_run_id,
            analysis_id=analysis_id,
            classification_contract_id=self._current_contract.classification_contract_id,
        )
