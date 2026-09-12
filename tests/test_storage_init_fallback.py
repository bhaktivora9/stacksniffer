import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from services import storage_service


class BrokenCollection:
    async def create_index(self, *args, **kwargs):
        raise TimeoutError("index creation timed out")


class BrokenDb:
    analyses_result = BrokenCollection()


class BrokenClient:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def __getitem__(self, name):
        return BrokenDb()

    def close(self):
        self.closed = True


def test_init_db_falls_back_to_memory_when_mongo_startup_times_out(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.invalid")
    monkeypatch.setenv("MONGODB_DB", "stacksniffer_test")
    monkeypatch.setattr(storage_service, "AsyncIOMotorClient", BrokenClient)
    monkeypatch.setattr(storage_service, "_client", None)
    monkeypatch.setattr(storage_service, "_db", None)
    monkeypatch.setattr(storage_service, "_memory_store", {})

    asyncio.run(storage_service.init_db())

    assert storage_service._client is None
    assert storage_service._db is None
    assert storage_service.is_available() is False
    assert storage_service._memory("taxonomy_software_types")
    assert storage_service._memory("taxonomy_technology_roles")
    assert storage_service._memory("dep_technology_roles")
