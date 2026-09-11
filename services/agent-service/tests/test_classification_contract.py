from datetime import datetime, timezone

from agent_service.application.classification_workflow import ClassificationRunStatus, ClassificationWorkflow
from agent_service.domain.classification_contract import ClassificationContract, derive_classification_contract_id
from agent_service.main import app
from fastapi.testclient import TestClient


client = TestClient(app)


def contract(taxonomy: str = "v1.0.0", prompt: str = "prompt-v1") -> ClassificationContract:
    return ClassificationContract.create(
        "class-v1", "default", prompt, taxonomy,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_contract_id_is_deterministic_and_provenance_resolves():
    first = contract()
    second = contract()
    assert first == second
    assert first.classification_contract_id == derive_classification_contract_id(
        "class-v1", "default", "prompt-v1", "v1.0.0")
    assert first.to_dict()["created_at"] == "2026-01-01T00:00:00Z"


def test_taxonomy_or_prompt_change_rotates_contract():
    first = contract()
    assert first.classification_contract_id != contract("v1.0.1").classification_contract_id
    assert first.classification_contract_id != contract(prompt="prompt-v2").classification_contract_id


def test_superseded_run_retains_original_contract_provenance():
    workflow = ClassificationWorkflow(contract())
    run = workflow.start_run("run-1", "analysis-1")
    workflow.rotate_contract(contract("v1.0.1"))
    superseded = run.supersede()
    assert superseded.status is ClassificationRunStatus.SUPERSEDED
    assert superseded.classification_contract_id == run.classification_contract_id
    assert superseded.classification_contract_id != workflow.current_contract.classification_contract_id


def test_current_contract_query_resolves_full_provenance():
    response = client.get("/internal/v1/classification-contract/current")
    assert response.status_code == 200
    assert set(response.json()) == {
        "classification_contract_id", "classification_pipeline_version",
        "classification_model_profile", "prompt_version", "taxonomy_version", "created_at",
    }
