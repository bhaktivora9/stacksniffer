import asyncio

import pytest

import backend.services.learning_service as learning_service
import backend.services.storage_service as storage_service


def run(coro):
    return asyncio.run(coro)


def reset_memory():
    storage_service._db = None
    storage_service._memory_store.clear()
    storage_service._memory_store.update({
        "analyses_result": {},
        "analyses_request": {},
        "corrections": {},
        "feedback": {},
        "analysis_events": [],
        "stack_feedback": {},
        "insights_feedback": [],
        "quality_criteria": {},
        "software_types": {},
        "dep_technology_roles": [],
        "dep_technology_role_feedback": [],
        "taxonomy_software_types": {},
        "taxonomy_technology_roles": {},
        "software_type_disagreements": {},
        "review_items": {},
        "correction_events": [],
        "software_type_corrections": {},
        "learned_technology_mappings": {},
    })


def rated_output(keyword="fastapi", tech="FastAPI"):
    return {
        "software_type": "library",
        "pattern_matches": [
            {
                "tech": tech,
                "technology_role": "frameworks",
                "matched_file": "pyproject.toml",
                "matched_keyword": keyword,
                "confidence": 0.95,
            },
            {
                "tech": "Jest",
                "technology_role": "testing",
                "matched_file": ".github/workflows/test.yml",
                "matched_keyword": "junit",
                "confidence": 0.95,
            },
        ],
    }


def add_feedback(repo_key, software_type_correct=True, wrong_techs=None, output=None):
    run(storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha="sha-rated",
        pipeline_version="v1",
        rated_output=output or rated_output(),
        rated_embedding=[0.1, 0.2],
        feedback={
            "software_type_correct": software_type_correct,
            "techs_wrong": wrong_techs or [],
        },
    ))


def test_pattern_accuracy_uses_feedback_when_analyses_result_empty():
    reset_memory()
    repo_key = "github:tiangolo/fastapi"
    for _ in range(3):
        add_feedback(repo_key)
    storage_service._memory_store["analyses_result"].clear()

    accuracy = run(learning_service.compute_pattern_accuracy_from_corpus())

    assert accuracy["fastapi"]["fires"] == 3
    assert accuracy["fastapi"]["correct"] == 3
    assert accuracy["fastapi"]["accuracy"] == 1.0
    assert "junit" not in accuracy


def test_pattern_accuracy_uses_rated_output_not_reanalyzed_live_stack():
    reset_memory()
    repo_key = "github:tiangolo/fastapi"
    for _ in range(3):
        add_feedback(repo_key, output=rated_output(keyword="fastapi", tech="FastAPI"))

    run(storage_service.upsert_repo_analysis(
        repo_key=repo_key,
        stack={
            "software_type": "web_app",
            "pattern_matches": [
                {
                    "tech": "React",
                    "technology_role": "frameworks",
                    "matched_file": "package.json",
                    "matched_keyword": "react",
                    "confidence": 0.95,
                }
            ],
        },
        embedding=[9.9],
        commit_sha="sha-new",
        pipeline_version="v2",
    ))

    accuracy = run(learning_service.compute_pattern_accuracy_from_corpus())

    assert "fastapi" in accuracy
    assert "react" not in accuracy


def test_learning_stats_reports_layer0_shadow_agreement_rate():
    reset_memory()
    for index, repo_key in enumerate([
        "github:example/agree-one",
        "github:example/agree-two",
        "github:example/disagree",
        "github:example/low-confidence-agree",
    ], start=1):
        confidence = 0.42 if "low-confidence" in repo_key else 0.91
        run(storage_service.upsert_repo_analysis(
            repo_key,
            {
                "software_type": "library" if "disagree" not in repo_key else "web_app",
                "layer0_prediction": {
                    "software_type": "library",
                    "confidence": confidence,
                    "advisory": True,
                },
            },
            [0.1, 0.2],
            f"sha{index}",
            "v1",
        ))

    run(storage_service.record_software_type_disagreement(
        "github:example/disagree",
        "sha3",
        "v1",
        {
            "layer0": {"software_type": "library", "confidence": 0.91, "advisory": True},
            "gemini": {"software_type": "web_app", "confidence": 0.88},
            "selected_software_type": "web_app",
            "selected_confidence": 0.88,
            "guard_corrected": False,
            "training_status": "pending_human_validation",
        },
    ))
    run(storage_service.record_software_type_disagreement(
        "github:example/disagree",
        "old-sha",
        "v1",
        {
            "layer0": {"software_type": "library", "confidence": 0.99, "advisory": True},
            "gemini": {"software_type": "deployable_service", "confidence": 0.80},
            "selected_software_type": "deployable_service",
            "selected_confidence": 0.80,
            "training_status": "pending_human_validation",
        },
    ))
    run(storage_service.record_software_type_disagreement(
        "github:example/not-current",
        "sha-missing",
        "v1",
        {
            "layer0": {"software_type": "library", "confidence": 0.99, "advisory": True},
            "gemini": {"software_type": "web_app", "confidence": 0.80},
            "selected_software_type": "web_app",
            "selected_confidence": 0.80,
            "training_status": "pending_human_validation",
        },
    ))

    stats = run(learning_service.get_learning_stats())

    assert stats["layer0_predictions_total"] == 4
    assert stats["layer0_disagreements"] == 1
    assert stats["layer0_agreements"] == 3
    assert stats["layer0_agreement_rate"] == pytest.approx(3 / 4)
    assert stats["layer0_confidence_bands"]["high"]["total"] == 3
    assert stats["layer0_confidence_bands"]["high"]["disagreements"] == 1
    assert stats["layer0_confidence_bands"]["high"]["agreement_rate"] == pytest.approx(2 / 3)
    assert stats["layer0_confidence_bands"]["low"]["agreement_rate"] == 1
    assert stats["layer0"]["mode"] == "advisory_shadow"
    assert stats["layer0_disagreement_examples"][0]["repo_key"] == "github:example/disagree"
    assert stats["layer0_disagreement_examples"][0]["commit_sha"] == "sha3"
