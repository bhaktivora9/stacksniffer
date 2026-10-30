"""Schema migrations: discovery, applying them to a database that already holds data, the race the
state version must survive, and the API refusing an outdated schema with the fix.

The database tests create a scratch database next to TEST_DATABASE_URL's (same local server) and
drop it afterwards, so the baseline-only state can be built and migrated for real.
"""

import os
import threading
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from backend.services.schema_migrations import (
    BASELINE_FILES,
    SCHEMA_DIR,
    applied_version,
    apply_pending,
    available_migrations,
    required_version,
    schema_problem,
)

needs_db = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


def test_migrations_are_numbered_without_gaps(tmp_path):
    migrations = available_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "analysis_state_version" and required_version() == len(migrations)

    (tmp_path / "0001_first.sql").write_text("SELECT 1;")
    (tmp_path / "0003_gap.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="without gaps"):
        available_migrations(tmp_path)
    (tmp_path / "0003_gap.sql").unlink()
    (tmp_path / "2_bad-name.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="NNNN_description"):
        available_migrations(tmp_path)


@pytest.fixture
def scratch_url():
    """A fresh database holding only the baseline schema (001-006), dropped afterwards."""
    import psycopg

    base = os.environ["TEST_DATABASE_URL"]
    name = f"ss_migration_{uuid4().hex[:10]}"
    parts = urlsplit(base)
    url = urlunsplit(parts._replace(path=f"/{name}"))
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for file in BASELINE_FILES:
                conn.execute((SCHEMA_DIR / file).read_text(encoding="utf-8"))
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _factory(url):
    from backend.services.postgres import make_connection_factory

    return make_connection_factory(url)


def _existing_analysis(conn):
    """An analysis written before the migration, as live databases hold them."""
    repository = conn.fetch_scalar(
        "INSERT INTO core.repository (canonical_key, provider, owner_name, repository_name, clone_url) "
        "VALUES ('github:o/r', 'github', 'o', 'r', 'https://github.com/o/r.git') RETURNING id")
    version = conn.fetch_scalar("INSERT INTO core.repository_version (repository_id, commit_sha) "
                                "VALUES (%s, %s) RETURNING id", repository, "a" * 40)
    analysis = conn.fetch_scalar("INSERT INTO core.analysis (repository_version_id, structural_pipeline_version, "
                                 "status) VALUES (%s, 'structural-test', 'READY') RETURNING id", version)
    conn.commit()
    return str(analysis)


@needs_db
def test_an_existing_database_is_migrated_in_place_and_keeps_its_data(scratch_url):
    factory = _factory(scratch_url)
    conn = factory()
    try:
        analysis = _existing_analysis(conn)
        assert applied_version(conn) == 0
        problem = schema_problem(conn)
        assert problem["found"] == 0 and problem["required"] == required_version()
        assert "Run migrations: python -m backend.migrate" in problem["detail"]
        conn.rollback()
    finally:
        conn.close()

    applied = apply_pending(factory, log=lambda _: None)
    assert [m.version for m in applied] == list(range(1, required_version() + 1))
    assert apply_pending(factory, log=lambda _: None) == []  # nothing twice

    conn = factory()
    try:
        assert schema_problem(conn) is None and applied_version(conn) == required_version()
        assert conn.fetch_scalar("SELECT state_version FROM core.analysis WHERE id = %s", analysis) == 1  # backfilled
        conn.execute("UPDATE core.analysis SET status = 'FAILED' WHERE id = %s", analysis)
        assert conn.fetch_scalar("SELECT state_version FROM core.analysis WHERE id = %s", analysis) == 2
        assert conn.fetch_scalar("SELECT count(*) FROM core.analysis") == 1
        conn.rollback()
    finally:
        conn.close()


@needs_db
def test_a_failing_migration_leaves_no_trace(scratch_url, tmp_path):
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE core.migration_probe (id INT);")
    (tmp_path / "0002_broken.sql").write_text("CREATE TABLE core.half_done (id INT); SELECT no_such_function();")
    factory = _factory(scratch_url)
    with pytest.raises(Exception, match="no_such_function"):
        apply_pending(factory, tmp_path, log=lambda _: None)
    conn = factory()
    try:
        assert applied_version(conn) == 1  # the first one committed with its record
        assert conn.fetch_scalar("SELECT to_regclass('core.half_done')") is None  # the second rolled back whole
    finally:
        conn.close()


@needs_db
def test_concurrent_status_changes_never_share_a_version(scratch_url):
    """The worker and another writer race on one analysis: each change gets its own version."""
    factory = _factory(scratch_url)
    apply_pending(factory, log=lambda _: None)
    setup = factory()
    analysis = _existing_analysis(setup)
    setup.close()

    first, second = factory(), factory()
    try:
        first.execute("UPDATE core.analysis SET status = 'QUEUED' WHERE id = %s", analysis)  # holds the row lock
        versions = {}

        def racing_writer():
            versions["second"] = second.fetch_scalar(
                "UPDATE core.analysis SET status = 'INGESTING' WHERE id = %s RETURNING state_version", analysis)
            second.commit()

        thread = threading.Thread(target=racing_writer)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()  # waiting for the first writer's lock
        versions["first"] = first.fetch_scalar("SELECT state_version FROM core.analysis WHERE id = %s", analysis)
        first.commit()
        thread.join(timeout=10)
        assert versions == {"first": 2, "second": 3}
    finally:
        first.close()
        second.close()


@needs_db
def test_the_api_refuses_an_outdated_schema_with_the_fix(scratch_url, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import create_app

    monkeypatch.setenv("DATABASE_URL", scratch_url)  # a local scratch database, never a configured one
    monkeypatch.setenv("ANALYSIS_EMBEDDING_PROVIDER", "offline")
    monkeypatch.setenv("ANALYSIS_LLM_PROVIDER", "none")
    with TestClient(create_app()) as client:
        refused = client.get(f"/api/v1/analyses/{uuid4()}")
        health = client.get("/api/health").json()
    assert refused.status_code == 503
    assert refused.json()["detail"] == (f"Database schema version {required_version()} is required, but the database "
                                        "is at version 0. Run migrations: python -m backend.migrate")
    assert health["status"] == "degraded" and health["schema"]["status"] == "outdated"
    assert health["worker"] == "disabled"  # no worker writes to a schema it does not match
