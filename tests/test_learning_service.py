import asyncio

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
        "domains": {},
        "dep_categories": [],
        "dep_category_feedback": [],
    })


def rated_output(keyword="fastapi", tech="FastAPI"):
    return {
        "domain": "library",
        "pattern_matches": [
            {
                "tech": tech,
                "category": "frameworks",
                "matched_file": "pyproject.toml",
                "matched_keyword": keyword,
                "confidence": 0.95,
            },
            {
                "tech": "Jest",
                "category": "testing",
                "matched_file": ".github/workflows/test.yml",
                "matched_keyword": "junit",
                "confidence": 0.95,
            },
        ],
    }


def add_feedback(repo_key, domain_correct=True, wrong_techs=None, output=None):
    run(storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha="sha-rated",
        pipeline_version="v1",
        rated_output=output or rated_output(),
        rated_embedding=[0.1, 0.2],
        feedback={
            "domain_correct": domain_correct,
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
            "domain": "web_app",
            "pattern_matches": [
                {
                    "tech": "React",
                    "category": "frameworks",
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
