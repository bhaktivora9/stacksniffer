import json
from pathlib import Path

from canonical_identifiers import (
    agent_context_fingerprint,
    agent_result_cache_key,
    agent_review_spec_key,
    assembled_analysis_key,
    classification_contract_id,
    classification_key,
    deterministic_analysis_key,
)


VECTOR_FILE = Path(__file__).parents[2] / "golden-vectors" / "derivations.json"


def derive(inputs):
    deterministic = deterministic_analysis_key(
        inputs["repository_key"], inputs["commit_sha"], inputs["deterministic_pipeline_version"])
    contract = classification_contract_id(
        inputs["classification_pipeline_version"], inputs["classification_model_profile"],
        inputs["prompt_version"], inputs["taxonomy_version"])
    classification = classification_key(inputs["analysis_id"], contract)
    assembled = assembled_analysis_key(deterministic, classification)
    review = agent_review_spec_key(
        inputs["repository_key"], inputs["commit_sha"], inputs["review_goal"],
        inputs["agent_version"], inputs["prompt_version"], inputs["model_profile"])
    context = agent_context_fingerprint(
        inputs["classification_result_id"], inputs["retrieval_index_version"],
        inputs["taxonomy_version"], inputs["graph_snapshot_version"])
    return {
        "deterministic_analysis_key": deterministic,
        "classification_contract_id": contract,
        "classification_key": classification,
        "assembled_analysis_key": assembled,
        "agent_review_spec_key": review,
        "agent_context_fingerprint": context,
        "agent_result_cache_key": agent_result_cache_key(review, context),
    }


def test_golden_vector():
    vector = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))["vectors"][0]
    assert derive(vector["inputs"]) == vector["outputs"]


def test_required_invariants():
    vector = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))["vectors"][0]
    inputs = vector["inputs"]
    baseline = derive(inputs)

    changed_taxonomy = dict(inputs, taxonomy_version="v1.2.1")
    changed_contract = classification_contract_id(
        changed_taxonomy["classification_pipeline_version"],
        changed_taxonomy["classification_model_profile"],
        changed_taxonomy["prompt_version"],
        changed_taxonomy["taxonomy_version"])
    assert baseline["deterministic_analysis_key"] == derive(changed_taxonomy)["deterministic_analysis_key"]
    assert baseline["classification_contract_id"] != changed_contract
    assert baseline["classification_key"] != classification_key(inputs["analysis_id"], changed_contract)
    assert baseline["assembled_analysis_key"] != assembled_analysis_key(
        baseline["deterministic_analysis_key"], classification_key(inputs["analysis_id"], changed_contract))

    assert baseline["agent_result_cache_key"] == agent_result_cache_key(
        baseline["agent_review_spec_key"], baseline["agent_context_fingerprint"])
