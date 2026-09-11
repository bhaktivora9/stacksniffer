from fastapi import APIRouter

from agent_service.application.classification_workflow import ClassificationWorkflow
from agent_service.domain.classification_contract import ClassificationContract

router = APIRouter()

_current_workflow = ClassificationWorkflow(
    ClassificationContract.create("class-v1", "default", "prompt-v1", "v1.0.0")
)


@router.get("/internal/v1/classification-contract/current")
def current_classification_contract() -> dict[str, str]:
    return _current_workflow.current_contract.to_dict()
