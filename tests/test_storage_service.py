import asyncio
from datetime import timedelta

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


def test_upsert_repo_analysis_twice_keeps_one_doc():
    reset_memory()
    repo_key = "github:vercel/next.js"

    run(storage_service.upsert_repo_analysis(
        repo_key, {"domain": "frontend"}, [0.1], "sha1", "v1"
    ))
    run(storage_service.upsert_repo_analysis(
        repo_key, {"domain": "web_framework"}, [0.2], "sha2", "v1"
    ))

    docs = storage_service._memory_store["analyses_result"]
    assert list(docs) == [repo_key]
    assert docs[repo_key]["commit_sha"] == "sha2"
    assert docs[repo_key]["stack"]["domain"] == "web_framework"
    assert docs[repo_key]["stack_embedding"] == [0.2]


def test_is_fresh_truth_table():
    doc = {"commit_sha": "sha1", "pipeline_version": "v1"}

    assert storage_service.is_fresh(doc, "sha1", "v1") is True
    assert storage_service.is_fresh(doc, "sha2", "v1") is False
    assert storage_service.is_fresh(doc, "sha1", "v2") is False
    assert storage_service.is_fresh(doc, "sha2", "v2") is False
    assert storage_service.is_fresh(None, "sha1", "v1") is False


def test_claim_refresh_second_caller_gets_false():
    reset_memory()
    repo_key = "github:vercel/next.js"

    first = run(storage_service.claim_refresh(repo_key, "sha1", "v1"))
    second = run(storage_service.claim_refresh(repo_key, "sha1", "v1"))

    assert first is True
    assert second is False


def test_claim_refresh_expired_lease_is_reclaimable():
    reset_memory()
    repo_key = "github:vercel/next.js"

    assert run(storage_service.claim_refresh(repo_key, "sha1", "v1")) is True
    refresh = storage_service._memory_store["analyses_result"][repo_key]["refresh"]
    refresh["lease_expires_at"] = storage_service._now() - timedelta(seconds=1)

    assert run(storage_service.claim_refresh(repo_key, "sha2", "v1")) is True
    assert (
        storage_service._memory_store["analyses_result"][repo_key]["refresh"]["target_sha"]
        == "sha2"
    )


def test_apply_corrections_touched_only_when_value_changes():
    reset_memory()
    repo_key = "github:vercel/next.js"
    stack = {"domain": "framework", "stack_pattern": "Custom"}

    run(storage_service.upsert_correction(repo_key, "domain", "framework"))
    corrected, touched = run(storage_service.apply_corrections(repo_key, stack))
    assert corrected == stack
    assert touched is False

    run(storage_service.upsert_correction(repo_key, "stack_pattern", "SSR Framework"))
    corrected, touched = run(storage_service.apply_corrections(repo_key, stack))
    assert corrected["stack_pattern"] == "SSR Framework"
    assert touched is True


def test_get_labeled_training_data_uses_feedback_snapshot_only():
    reset_memory()
    repo_key = "github:vercel/next.js"
    rated_output = {
        "domain": "frontend",
        "primary_language": "TypeScript",
        "complexity_score": 7,
        "ai_calls_made": 1,
    }

    run(storage_service.upsert_repo_analysis(
        repo_key, {"domain": "mutated"}, [9.9], "sha1", "v1"
    ))
    run(storage_service.store_feedback(
        repo_key,
        "sha1",
        "v1",
        rated_output,
        [0.1, 0.2],
        {"domain_correct": False, "correct_domain": "web_framework"},
    ))
    storage_service._memory_store["analyses_result"].clear()

    rows = run(storage_service.get_labeled_training_data())

    assert len(rows) == 1
    assert rows[0]["detected_domain"] == "frontend"
    assert rows[0]["correct_domain"] == "web_framework"
    assert rows[0]["stack_embedding"] == [0.1, 0.2]
