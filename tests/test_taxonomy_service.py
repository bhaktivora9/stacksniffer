import asyncio

import backend.services.storage_service as storage_service
from backend.routers.taxonomy import list_categories, list_domains


def run(coro):
    return asyncio.run(coro)


def reset_taxonomy_memory():
    storage_service._db = None
    storage_service._memory_store["taxonomy_domains"] = {}
    storage_service._memory_store["taxonomy_categories"] = {}
    storage_service._invalidate_taxonomy_cache()


def test_domain_taxonomy_is_gt_ordered_and_idempotent():
    reset_taxonomy_memory()

    run(storage_service.seed_builtin_domains())
    run(storage_service.seed_builtin_domains())
    domains = run(storage_service.get_domains())

    assert [domain["_id"] for domain in domains] == [
        "database", "data_pipeline", "ml_platform", "infra_tool",
        "web_app", "library", "unknown",
    ]
    assert run(storage_service.is_valid_domain("database")) is True
    assert run(storage_service.is_valid_domain("web_api")) is False
    assert len(storage_service._memory_store["taxonomy_domains"]) == 7


def test_taxonomy_empty_store_safely_falls_back_to_builtins():
    reset_taxonomy_memory()

    assert "database" in run(storage_service.get_valid_domains())
    assert run(storage_service.get_valid_categories()) == set(
        storage_service.BUILTIN_CATEGORIES
    )


def test_taxonomy_endpoints_separate_ids_and_labels():
    reset_taxonomy_memory()

    domain_response = run(list_domains())
    category_response = run(list_categories())

    assert domain_response["domains"][0] == {
        "id": "database", "label": "Database", "sentinel": False,
    }
    assert domain_response["domains"][-1]["sentinel"] is True
    assert category_response["categories"][0]["id"] == "languages"
