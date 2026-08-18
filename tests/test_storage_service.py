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


def test_specific_identity_counts_are_ranked_from_persisted_analyses():
    reset_memory()
    storage_service._memory_store["analyses_result"] = {
        "github:a/one": {"stack": {"specific_identity": "model_serving"}},
        "github:a/two": {"stack": {"specific_identity": "model_serving"}},
        "github:b/one": {"stack": {"specific_identity": "ci_cd_engine"}},
        "github:c/none": {"stack": {"specific_identity": None}},
    }

    assert run(storage_service.get_specific_identity_counts()) == [
        {"specific_identity": "model_serving", "count": 2},
        {"specific_identity": "ci_cd_engine", "count": 1},
    ]


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


def test_embedding_similarity_memory_fallback_scores_and_excludes_repo():
    reset_memory()
    run(storage_service.upsert_repo_analysis(
        "github:example/current",
        {"software_type": "library"},
        [1.0, 0.0],
        "sha1",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/closest",
        {"software_type": "library"},
        [0.9, 0.1],
        "sha2",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/far",
        {"software_type": "library"},
        [0.0, 1.0],
        "sha3",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/stale",
        {"software_type": "library"},
        [1.0, 0.0],
        "sha4",
        "old-pipeline",
    ))

    results = run(storage_service.find_similar(
        [1.0, 0.0],
        limit=10,
        exclude_repo_key="github:example/current",
    ))

    assert [row["repo_key"] for row in results] == [
        "github:example/closest",
        "github:example/far",
    ]
    assert results[0]["score"] > results[1]["score"]
    assert "stack_embedding" not in results[0]


def test_specific_identity_and_software_type_similarity_ranking():
    reset_memory()
    run(storage_service.upsert_repo_analysis(
        "github:example/current",
        {"software_type": "deployable_service", "specific_identity": "model_serving"},
        None,
        "sha1",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/exact",
        {"software_type": "deployable_service", "specific_identity": "model_serving"},
        None,
        "sha2",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/identity",
        {"software_type": "library", "specific_identity": "model_serving"},
        None,
        "sha3",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/type",
        {"software_type": "deployable_service", "specific_identity": "auth_server"},
        None,
        "sha4",
        storage_service.PIPELINE_VERSION,
    ))
    run(storage_service.upsert_repo_analysis(
        "github:example/stale",
        {"software_type": "deployable_service", "specific_identity": "model_serving"},
        None,
        "sha5",
        "old-pipeline",
    ))

    results = run(storage_service.find_similar_by_specific_identity_and_software_type(
        "model_serving",
        "deployable_service",
        limit=10,
        exclude_repo_key="github:example/current",
    ))

    assert [row["repo_key"] for row in results] == [
        "github:example/exact",
        "github:example/identity",
        "github:example/type",
    ]
    assert [row["match_basis"] for row in results] == [
        "specific_identity+software_type",
        "specific_identity",
        "software_type",
    ]


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


def test_software_type_review_is_pending_until_approved_and_repo_scoped():
    reset_memory()
    repo_key = "github:qos-ch/slf4j"
    run(storage_service.upsert_software_type_correction(
        "deployable_service",
        "library",
        {"repo_key": repo_key, "repo": "qos-ch/slf4j"},
    ))

    assert run(storage_service.get_approved_software_type_correction(
        repo_key, "deployable_service",
    )) is None

    run(storage_service.approve_software_type_correction(
        "deployable_service",
        "library",
        repo_key=repo_key,
        evidence={"repo_key": repo_key},
        assignment_method="ai_inferred",
    ))

    approved = run(storage_service.get_approved_software_type_correction(
        repo_key, "deployable_service",
    ))
    assert approved["corrected_value"] == "library"
    assert approved["assignment_method"] == "ai_inferred"
    assert run(storage_service.get_approved_software_type_correction(
        "github:celery/celery", "deployable_service",
    )) is None


def test_correction_triggers_are_append_only_and_queue_is_a_summary():
    reset_memory()
    evidence = {
        "repo_key": "github:qos-ch/slf4j",
        "repo": "qos-ch/slf4j",
        "analysis_id": "analysis-123",
    }

    first_item = run(storage_service.upsert_software_type_correction(
        "deployable_service", "library", evidence,
    ))
    second_item = run(storage_service.upsert_software_type_correction(
        "deployable_service", "library", evidence,
    ))

    assert first_item == second_item
    events = run(storage_service.get_correction_events())
    assert len(events) == 2
    assert len({event["_id"] for event in events}) == 2
    assert all(event["trigger_ref"] == "analysis-123" for event in events)
    assert all(event["actor"] is None for event in events)
    assert all(event["actor_kind"] == "user" for event in events)
    assert all(event["review_item_id"] == first_item for event in events)
    item = run(storage_service.get_review_item(first_item))
    assert item["seen_count"] == 1
    assert len(item["trigger_event_ids"]) == 2
    assert len(item["trigger_keys"]) == 1


def test_new_correction_after_completed_review_gets_a_new_queue_id():
    reset_memory()
    evidence = {
        "repo_key": "github:example/repo",
        "analysis_id": "analysis-1",
    }
    first = run(storage_service.upsert_software_type_correction(
        "application_platform", "library", evidence,
    ))
    run(storage_service.set_review_item_status(first, "approved", reviewed_by="alice"))

    second = run(storage_service.upsert_software_type_correction(
        "application_platform", "ml_platform", evidence,
    ))

    assert second != first
    assert run(storage_service.get_review_item(first))["status"] == "approved"
    assert run(storage_service.get_review_item(second))["status"] == "pending"


def test_approved_overlay_keeps_event_foreign_keys_and_approval_audit():
    reset_memory()
    evidence = {
        "repo_key": "github:qos-ch/slf4j",
        "repo": "qos-ch/slf4j",
        "analysis_id": "analysis-123",
    }
    item_id = run(storage_service.upsert_software_type_correction(
        "deployable_service", "library", evidence,
    ))
    item = run(storage_service.get_review_item(item_id))
    event_ids = item["trigger_event_ids"]

    run(storage_service.approve_software_type_correction(
        "deployable_service", "library", repo_key=evidence["repo_key"],
        evidence=evidence, source_event_ids=event_ids,
    ))
    run(storage_service.resolve_correction_events(
        event_ids, resolution="approved", actor="alice",
    ))

    overlay = run(storage_service.get_approved_software_type_correction(
        evidence["repo_key"], "deployable_service",
    ))
    assert overlay["source_event_ids"] == event_ids
    event = run(storage_service.get_correction_events("approved"))[0]
    assert event["approved_by"] == "alice"
    assert event["approved_at"]


def test_system_coercion_uses_the_same_ledger_with_explicit_system_actor():
    reset_memory()
    run(storage_service.append_correction_event(
        correction_field="software_type",
        pipeline_value="ml_platform",
        proposed_value="unknown",
        evidence={"repo_key": "github:example/ml", "analysis_id": "analysis-ml"},
        assignment_method="system_guard",
        actor="system",
        actor_kind="system",
        resolution="system_coercion",
        event_kind="system_coercion",
    ))

    event = run(storage_service.get_correction_events("system_coercion"))[0]
    assert event["event_kind"] == "system_coercion"
    assert event["actor"] == "system"
    assert event["actor_kind"] == "system"


def test_inactive_canonical_role_is_added_as_activation_candidate():
    reset_memory()
    run(storage_service.record_emergent_technology_role(
        "observability", "Sentry", "getsentry/sentry",
    ))
    run(storage_service.record_emergent_technology_role(
        "observability", "OpenTelemetry", "open-telemetry/opentelemetry-python",
    ))

    items, total = run(storage_service.get_review_queue(
        kind="emergent_role", status="pending",
    ))
    assert total == 1
    assert items[0]["pipeline_value"] == "observability"
    assert items[0]["proposed_value"] == "observability"
    assert items[0]["seen_count"] == 2
    assert items[0]["evidence"]["example"] == "OpenTelemetry"
    assert items[0]["assignment_method"] == "inactive_canonical"
    assert items[0]["evidence"]["known_inactive_role"] is True
    taxonomy = storage_service._memory("taxonomy_technology_roles")["observability"]
    assert taxonomy["status"] == "pending_activation"
    assert taxonomy["builtin"] is True
    assert taxonomy["seen_count"] == 2

    # Pending roles are review subjects, not canonical dropdown targets.
    assert "observability" not in run(storage_service.get_valid_technology_roles())
    run(storage_service.apply_taxonomy_action(
        "technology_role", "observability", "promote",
        source="maintainer_approved",
    ))
    assert "observability" in run(storage_service.get_valid_technology_roles())


def test_unknown_role_remains_a_genuine_emergent_candidate():
    reset_memory()
    run(storage_service.record_emergent_technology_role(
        "dev_tool", "Asciidoctor", "spring-projects/spring-boot",
    ))

    items, total = run(storage_service.get_review_queue(
        kind="emergent_role", status="pending",
    ))
    assert total == 1
    assert items[0]["pipeline_value"] == "dev_tool"
    assert items[0]["assignment_method"] == "ai_inferred"
    assert items[0]["evidence"]["known_inactive_role"] is False


def test_repository_scoped_technology_role_overrides_are_rejected():
    reset_memory()
    repo_key = "github:example/api"
    stack = {
        "frameworks": [
            {"name": "Starlette", "technology_role": "frameworks"},
            {"name": "Pydantic", "technology_role": "frameworks"},
        ],
        "library": [],
    }
    try:
        run(storage_service.upsert_technology_role_correction(
            repo_key, "Starlette", "library",
        ))
    except ValueError as exc:
        assert "globally stable" in str(exc)
    else:
        raise AssertionError("repo-scoped technology-role override was accepted")


def test_approved_architectural_layer_override_preserves_original_stack():
    reset_memory()
    repo_key = "github:example/app"
    stack = {
        "frameworks": [{
            "name": "React",
            "technology_role": "frameworks",
            "architectural_layer": {
                "primary": "backend",
                "secondary": ["frontend"],
                "assignment_method": "ai_inferred",
                "confidence": 0.7,
                "disambiguation_pending": True,
            },
        }],
    }

    run(storage_service.upsert_architectural_layer_correction(
        repo_key, "React", "frontend",
    ))
    corrected, touched = run(storage_service.apply_corrections(repo_key, stack))

    assert touched is True
    assert stack["frameworks"][0]["architectural_layer"]["primary"] == "backend"
    layer = corrected["frameworks"][0]["architectural_layer"]
    assert layer["primary"] == "frontend"
    assert layer["secondary"] == []
    assert layer["assignment_method"] == "maintainer_approved"
    assert layer["confidence"] == 1.0
    assert layer["disambiguation_pending"] is False
