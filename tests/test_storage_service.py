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
        "software_types": {},
        "dep_technology_roles": [],
        "dep_technology_role_feedback": [],
    })


def test_upsert_repo_analysis_twice_keeps_one_doc():
    reset_memory()
    repo_key = "github:vercel/next.js"

    run(storage_service.upsert_repo_analysis(
        repo_key, {"software_type": "frontend"}, [0.1], "sha1", "v1"
    ))
    run(storage_service.upsert_repo_analysis(
        repo_key, {"software_type": "web_framework"}, [0.2], "sha2", "v1"
    ))

    docs = storage_service._memory_store["analyses_result"]
    assert list(docs) == [repo_key]
    assert docs[repo_key]["commit_sha"] == "sha2"
    assert docs[repo_key]["stack"]["software_type"] == "web_framework"
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
    stack = {"software_type": "framework", "stack_pattern": "Custom"}

    run(storage_service.upsert_correction(repo_key, "software_type", "framework"))
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
        "software_type": "frontend",
        "primary_language": "TypeScript",
        "complexity_score": 7,
        "ai_calls_made": 1,
    }

    run(storage_service.upsert_repo_analysis(
        repo_key, {"software_type": "mutated"}, [9.9], "sha1", "v1"
    ))
    run(storage_service.store_feedback(
        repo_key,
        "sha1",
        "v1",
        rated_output,
        [0.1, 0.2],
        {"software_type_correct": False, "correct_software_type": "web_framework"},
    ))
    storage_service._memory_store["analyses_result"].clear()

    rows = run(storage_service.get_labeled_training_data())

    assert len(rows) == 1
    assert rows[0]["detected_software_type"] == "frontend"
    assert rows[0]["correct_software_type"] == "web_framework"
    assert rows[0]["stack_embedding"] == [0.1, 0.2]


def test_emergent_software_type_lifecycle_memory_fallback():
    reset_memory()
    run(storage_service.seed_builtin_software_types())
    run(storage_service.record_emergent_software_type("observability", "github:grafana/grafana"))
    pending = run(storage_service.get_pending_taxonomy())
    assert pending["software_types"][0]["_id"] == "observability"
    assert pending["software_types"][0]["seen_count"] == 1

    run(storage_service.apply_taxonomy_action("software_type", "observability", "promote"))
    assert "observability" in run(storage_service.get_valid_software_types())


def test_software_type_similarity_excludes_stale_pipeline_labels():
    reset_memory()
    run(storage_service.upsert_repo_analysis(
        "github:example/stale",
        {"software_type": "library"},
        [0.1],
        "sha1",
        "old-pipeline",
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/current",
        {"software_type": "library"},
        [0.2],
        "sha2",
        storage_service.PIPELINE_VERSION,
    ))

    results = run(storage_service.find_similar_by_software_type("library", limit=10))

    assert [row["repo_key"] for row in results] == ["github:example/current"]


def test_layer0_disagreement_is_upserted_as_pending_training_signal():
    reset_memory()
    disagreement = {
        "layer0": {"software_type": "library", "confidence": 0.93, "advisory": True},
        "gemini": {"software_type": "web_app", "confidence": 0.88},
        "selected_software_type": "web_app",
        "guard_corrected": False,
        "training_status": "pending_human_validation",
    }

    run(storage_service.record_software_type_disagreement(
        "github:example/app", "sha1", "v1", disagreement
    ))
    run(storage_service.record_software_type_disagreement(
        "github:example/app", "sha1", "v1", disagreement
    ))

    rows = run(storage_service.get_software_type_disagreements())
    assert len(rows) == 1
    assert rows[0]["layer0"]["software_type"] == "library"
    assert rows[0]["selected_software_type"] == "web_app"
    assert rows[0]["training_status"] == "pending_human_validation"


def test_disagreement_storage_accepts_layer0_abstention():
    reset_memory()
    run(storage_service.record_software_type_disagreement(
        "github:example/cold-start",
        "sha1",
        "v1",
        {
            "layer0": None,
            "gemini": {"software_type": "web_app", "confidence": 0.8},
            "selected_software_type": "web_app",
            "training_status": "not_applicable_layer0_abstained",
        },
    ))

    rows = run(storage_service.get_software_type_disagreements())
    assert rows[0]["layer0"] is None


def test_technology_role_merge_uses_existing_feedback_store_in_memory():
    reset_memory()
    run(storage_service.seed_builtin_taxonomy_technology_roles())
    run(storage_service.record_emergent_technology_role("bundler", "Vite", "github:vitejs/vite"))
    run(storage_service.apply_taxonomy_action("technology_role", "bundler", "merge", "infra"))
    decisions = run(storage_service.get_technology_role_feedback_decisions())
    assert decisions["merged"] == {"bundler": "infra"}
    assert "bundler" not in run(storage_service.get_valid_technology_roles())


def test_search_analysis_examples_supports_technology_combinations():
    reset_memory()
    run(storage_service.upsert_repo_analysis(
        "github:example/api",
        {
            "software_type": "web_app",
            "primary_language": "Python",
            "stack_pattern": "Hexagonal",
            "frameworks": [{"name": "FastAPI"}],
            "databases": [{"name": "PostgreSQL"}],
        },
        [],
        "sha1",
        "v1",
    ))

    matches = run(storage_service.search_analysis_examples(
        "technology", "fastapi, postgres", 10
    ))
    missing = run(storage_service.search_analysis_examples(
        "technology", "fastapi, redis", 10
    ))

    assert matches[0]["repo"] == "example/api"
    assert matches[0]["matched_technologies"] == ["FastAPI", "PostgreSQL"]
    assert missing == []


def test_search_analysis_examples_applies_software_type_corrections():
    reset_memory()
    run(storage_service.upsert_repo_analysis(
        "github:example/search",
        {"software_type": "library", "frameworks": [{"name": "SearchKit"}]},
        [],
        "sha1",
        "v1",
    ))
    run(storage_service.upsert_correction(
        "github:example/search", "software_type", "database"
    ))

    assert run(storage_service.search_analysis_examples("software_type", "library")) == []
    matches = run(storage_service.search_analysis_examples("software_type", "database"))
    assert matches[0]["repo"] == "example/search"
    assert matches[0]["software_type"] == "database"
