import asyncio

import backend.services.storage_service as storage_service
from backend.routers.taxonomy import list_technology_roles, list_software_types


def run(coro):
    return asyncio.run(coro)


def reset_taxonomy_memory():
    storage_service._db = None
    storage_service._memory_store["taxonomy_software_types"] = {}
    storage_service._memory_store["taxonomy_technology_roles"] = {}
    storage_service._invalidate_taxonomy_cache()


def test_software_type_taxonomy_is_gt_ordered_and_idempotent():
    reset_taxonomy_memory()

    run(storage_service.seed_builtin_software_types())
    run(storage_service.seed_builtin_software_types())
    software_types = run(storage_service.get_software_types())

    assert [software_type["_id"] for software_type in software_types] == [
        "database", "data_pipeline", "ml_platform", "infra_tool",
        "web_app", "library", "unknown",
    ]
    assert run(storage_service.is_valid_software_type("database")) is True
    assert run(storage_service.is_valid_software_type("web_api")) is False
    assert len(storage_service._memory_store["taxonomy_software_types"]) == 7


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
