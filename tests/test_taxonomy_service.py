import asyncio

import backend.services.storage_service as storage_service
from backend.routers.taxonomy import (
    list_specific_identity_counts,
    list_technology_roles,
    list_software_types,
)


def run(coro):
    return asyncio.run(coro)


def reset_taxonomy_memory():
    storage_service._db = None
    storage_service._memory_store["taxonomy_software_types"] = {}
    storage_service._memory_store["taxonomy_technology_roles"] = {}
    storage_service._invalidate_taxonomy_cache()


def test_software_type_taxonomy_is_gt_ordered_and_idempotent():
    reset_taxonomy_memory()
    storage_service._memory_store["taxonomy_software_types"]["library"] = {
        "_id": "library", "label": "Stale label", "order": 200, "active": False,
    }
    storage_service._memory_store["taxonomy_software_types"]["ml_platform"] = {
        "_id": "ml_platform", "label": "ML Platform", "order": 3,
        "active": True, "builtin": True,
    }

    run(storage_service.seed_builtin_software_types())
    run(storage_service.seed_builtin_software_types())
    software_types = run(storage_service.get_software_types())

    assert [software_type["_id"] for software_type in software_types] == [
        "database", "data_pipeline", "web_application", "deployable_service",
        "cli_tool", "library", "sdk", "framework", "build_tool",
        "infrastructure_tool", "desktop_application", "application_platform",
        "unknown",
    ]
    assert run(storage_service.is_valid_software_type("database")) is True
    assert run(storage_service.is_valid_software_type("web_api")) is False
    assert len([
        record
        for record in storage_service._memory_store["taxonomy_software_types"].values()
        if record.get("active", True)
    ]) == 13
    stale = storage_service._memory_store["taxonomy_software_types"]["ml_platform"]
    assert stale["active"] is False
    assert stale["deprecated"] is True


def test_taxonomy_empty_store_safely_falls_back_to_builtins():
    reset_taxonomy_memory()

    assert "database" in run(storage_service.get_valid_software_types())
    assert run(storage_service.get_valid_technology_roles()) == set(
        storage_service.BUILTIN_TECHNOLOGY_ROLES
    )


def test_taxonomy_endpoints_separate_ids_and_labels():
    reset_taxonomy_memory()

    software_type_response = run(list_software_types())
    technology_role_response = run(list_technology_roles())

    assert software_type_response["software_types"][0] == {
        "id": "database", "label": "Database", "sentinel": False,
    }
    assert software_type_response["software_types"][-1]["sentinel"] is True
    assert technology_role_response["technology_roles"][0]["id"] == "languages"


def test_specific_identity_endpoint_exposes_frequency_distribution():
    storage_service._db = None
    storage_service._memory_store["analyses_result"] = {
        "github:a/one": {"stack": {"specific_identity": "model_serving"}},
        "github:a/two": {"stack": {"specific_identity": "model_serving"}},
    }

    assert run(list_specific_identity_counts()) == {
        "specific_identities": [{"specific_identity": "model_serving", "count": 2}],
        "count": 1,
    }
