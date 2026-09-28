"""Canonical IR persistence against the real schema, inside a rolled-back transaction.

Skipped unless TEST_DATABASE_URL points at a disposable database with the core schema
applied. DATABASE_URL is deliberately ignored: importing backend.main loads
backend/.env, which would otherwise point these tests at a real database.
"""

import asyncio
import os
import shutil
import subprocess
from uuid import uuid4

import pytest

from backend.services.analysis_persistence import create_or_reuse_analysis
from backend.services.extraction.analyzers import PythonAnalyzer
from backend.services.extraction.contracts import CancellationToken, ExtractionContext
from backend.services.extraction.extractor import RepositoryExtractor
from backend.services.extraction.registry import AnalyzerRegistry
from backend.services.extraction.store import (
    ExtractionPersistenceError,
    PostgresExtractionStore,
    build_batch_payload,
    write_batch,
)

pytestmark = [
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set"),
    pytest.mark.skipif(not PythonAnalyzer().available, reason="tree-sitter grammars are not installed"),
]

PYTHON = b"""import os
from .models import User

class Base:
    pass

class Service(Base):
    def run(self):
        self.helper()
        os.getcwd()
        os.getcwd()

    def helper(self):
        return 1
"""
JAVA = b"""package com.acme;
import java.util.List;
public class Repo extends Base implements Store {
    public void save(int id) { validate(id); }
    private void validate(int id) {}
}
class Base {}
"""


@pytest.fixture
def conn():
    from test_analysis_postgres import UncommittedConnection

    from backend.services.postgres import make_connection_factory

    real = make_connection_factory(os.environ["TEST_DATABASE_URL"])()
    try:
        yield UncommittedConnection(real)
    finally:
        real.rollback()
        real.close()


def create_analysis(conn, sha="d" * 40):
    name = f"repo-{uuid4().hex[:8]}"
    return create_or_reuse_analysis(
        conn, canonical_repository_key=f"github:test-owner/{name}", owner_name="test-owner",
        repository_name=name, clone_url=f"https://github.com/test-owner/{name}.git",
        requested_reference="main", resolved_commit_sha=sha, structural_pipeline_version="structural-test",
    ).analysis_id


def extract_into(conn, tmp_path, analysis_id, files, batch_size=100):
    root = tmp_path / f"source-{uuid4().hex[:6]}"
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    context = ExtractionContext(analysis_id, uuid4(), root, "d" * 40, CancellationToken(), tuple(sorted(files)))
    store = PostgresExtractionStore(lambda: conn, analysis_id)
    return RepositoryExtractor(AnalyzerRegistry.default(), batch_size=batch_size).extract(context, store)


def counts(conn, analysis_id):
    a = str(analysis_id)
    return {
        "files": conn.fetch_scalar("SELECT count(*) FROM core.source_file WHERE analysis_id = %s", a),
        "entities": conn.fetch_scalar("SELECT count(*) FROM core.entity WHERE analysis_id = %s", a),
        "evidence": conn.fetch_scalar("SELECT count(*) FROM core.evidence WHERE analysis_id = %s", a),
        "edges": conn.fetch_scalar("SELECT count(*) FROM core.repository_edge WHERE analysis_id = %s", a),
        "entity_links": conn.fetch_scalar(
            "SELECT count(*) FROM core.entity_evidence ee JOIN core.entity e ON e.id = ee.entity_id "
            "WHERE e.analysis_id = %s", a),
        "edge_links": conn.fetch_scalar(
            "SELECT count(*) FROM core.edge_evidence ee JOIN core.repository_edge e ON e.id = ee.edge_id "
            "WHERE e.analysis_id = %s", a),
    }


def test_extraction_persists_facts_with_evidence(conn, tmp_path):
    analysis_id = create_analysis(conn)
    extract_into(conn, tmp_path, analysis_id, {"svc.py": PYTHON, "Repo.java": JAVA, "main.rs": b"fn main() {}\n"},
                 batch_size=1)
    a = str(analysis_id)

    stored = counts(conn, analysis_id)
    assert stored["files"] == 3 and stored["entities"] > 3 and stored["edges"] > 3
    assert conn.fetch_scalar("""
        SELECT count(*) FROM core.entity e
        WHERE e.analysis_id = %s AND NOT EXISTS (SELECT 1 FROM core.entity_evidence ee WHERE ee.entity_id = e.id)
    """, a) == 0
    assert conn.fetch_scalar("""
        SELECT count(*) FROM core.repository_edge r
        WHERE r.analysis_id = %s AND NOT EXISTS (SELECT 1 FROM core.edge_evidence ee WHERE ee.edge_id = r.id)
    """, a) == 0
    # Every link stays inside the analysis, including the tables without composite foreign keys.
    assert conn.fetch_scalar("""
        SELECT count(*) FROM core.entity_evidence ee
        JOIN core.entity e ON e.id = ee.entity_id JOIN core.evidence ev ON ev.id = ee.evidence_id
        WHERE e.analysis_id = %s AND ev.analysis_id <> e.analysis_id
    """, a) == 0

    evidence = conn.fetch_one("""
        SELECT ev.start_line, ev.end_line, ev.extractor_version, ev.metadata->>'commit_sha', sf.path
        FROM core.entity e
        JOIN core.entity_evidence ee ON ee.entity_id = e.id
        JOIN core.evidence ev ON ev.id = ee.evidence_id
        JOIN core.source_file sf ON sf.id = ev.file_id
        WHERE e.analysis_id = %s AND e.stable_key = 'method:svc.py::Service.run'
    """, a)
    assert tuple(evidence[:2]) == (8, 11)
    assert evidence[2] == PythonAnalyzer().extractor_version
    assert (evidence[3], evidence[4]) == ("d" * 40, "svc.py")

    parent = conn.fetch_scalar("""
        SELECT p.stable_key FROM core.entity e JOIN core.entity p ON p.id = e.parent_entity_id
        WHERE e.analysis_id = %s AND e.stable_key = 'method:svc.py::Service.run'
    """, a)
    assert parent == "class:svc.py::Service"
    assert conn.fetch_scalar(
        "SELECT parse_status FROM core.source_file WHERE analysis_id = %s AND path = 'main.rs'", a) == "UNSUPPORTED"
    # Two identical call sites on different lines: one edge, two pieces of evidence.
    assert conn.fetch_scalar("""
        SELECT count(*) FROM core.repository_edge r
        JOIN core.entity t ON t.id = r.target_entity_id JOIN core.edge_evidence ee ON ee.edge_id = r.id
        WHERE r.analysis_id = %s AND t.stable_key = 'external:python:call:os.getcwd'
    """, a) == 2


def test_repeated_extraction_is_idempotent(conn, tmp_path):
    analysis_id = create_analysis(conn)
    files = {"svc.py": PYTHON, "Repo.java": JAVA}
    extract_into(conn, tmp_path, analysis_id, files)
    first = counts(conn, analysis_id)
    extract_into(conn, tmp_path, analysis_id, files, batch_size=1)

    assert counts(conn, analysis_id) == first


def test_store_cannot_link_to_another_analysis(conn, tmp_path):
    first, second = create_analysis(conn, "e" * 40), create_analysis(conn, "f" * 40)
    extract_into(conn, tmp_path, first, {"svc.py": PYTHON})

    # A batch for the second analysis whose relationship names an entity that exists only in the first.
    from test_extraction_framework import snapshot

    context = snapshot(tmp_path / "other", {"svc.py": PYTHON})
    files = []
    RepositoryExtractor(AnalyzerRegistry.default()).extract(context, type("S", (), {"persist": lambda s, b: files.extend(b)})())
    payload = build_batch_payload(files)
    payload["entities"] = [e for e in payload["entities"] if e["stable_key"] != "class:svc.py::Base"]

    with pytest.raises(ExtractionPersistenceError, match="did not resolve") as caught:
        write_batch(conn, second, payload)
    unresolved = caught.value.context["unresolved"]
    assert unresolved and all(row["file"] == "svc.py" for row in unresolved)
    assert any("entity:class:svc.py::Base" in row["missing"] for row in unresolved)


def test_a_failed_batch_identifies_its_input_without_source_contents(conn, tmp_path):
    from test_extraction_framework import snapshot

    analysis_id, attempt_id = create_analysis(conn), uuid4()
    context = snapshot(tmp_path / "src", {"a.py": b"x = 1\n", "svc.py": PYTHON})
    files = []
    RepositoryExtractor(AnalyzerRegistry.default()).extract(context, type("S", (), {"persist": lambda s, b: files.extend(b)})())
    svc = next(f for f in files if f.path == "svc.py")
    # Drop the Base class from the batch: EXTENDS Service -> Base and its CONTAINS edge cannot resolve.
    broken = type(svc)(**{**svc.__dict__, "entities": tuple(e for e in svc.entities if e.name != "Base")})

    store = PostgresExtractionStore(lambda: conn, analysis_id, attempt_id)
    store.persist([next(f for f in files if f.path == "a.py")])  # batch 1 succeeds
    with pytest.raises(ExtractionPersistenceError) as caught:
        store.persist([broken])  # the store rolls the batch back; nothing below needs the database

    error = caught.value
    detail = error.failure_detail
    assert (detail["analysis_id"], detail["attempt_id"], detail["batch"]) == (str(analysis_id), str(attempt_id), 2)
    assert detail["table"] in ("edges", "entity_links") and detail["files"] == ["svc.py"]
    row = next(r for r in detail["unresolved"] if "entity:class:svc.py::Base" in r["missing"])
    assert row["file"] == "svc.py" and row["stable_key"] and row["relationship_type"] in ("EXTENDS", "CONTAINS")
    for text in (str(error), repr(detail)):  # identifies the input, never its contents
        assert "helper()" not in text and "osp.join" not in text
    assert f"batch=2" in str(error) and "svc.py" in str(error)


def test_other_database_errors_report_sqlstate_and_constraint_only(conn, tmp_path):
    from test_extraction_framework import snapshot

    analysis_id = create_analysis(conn)
    context = snapshot(tmp_path / "src", {"svc.py": PYTHON})
    files = []
    RepositoryExtractor(AnalyzerRegistry.default()).extract(context, type("S", (), {"persist": lambda s, b: files.extend(b)})())
    store = PostgresExtractionStore(lambda: conn, analysis_id, uuid4())
    original = build_batch_payload

    def invalid_payload(batch):
        payload = original(batch)
        payload["entities"][1]["entity_type"] = "NOT_A_TYPE"  # violates entity_type_ck
        return payload

    import backend.services.extraction.store as store_module
    try:
        store_module.build_batch_payload = invalid_payload
        with pytest.raises(ExtractionPersistenceError) as caught:
            store.persist(files)
    finally:
        store_module.build_batch_payload = original
    detail = caught.value.failure_detail
    assert (detail["table"], detail["sqlstate"], detail["constraint"]) == ("entities", "23514", "entity_type_ck")
    assert detail["files"] == ["svc.py"] and "NOT_A_TYPE" not in str(caught.value)


def test_schema_rejects_cross_analysis_entity_references(conn, tmp_path):
    import psycopg

    first, second = create_analysis(conn, "1" * 40), create_analysis(conn, "2" * 40)
    extract_into(conn, tmp_path, first, {"svc.py": PYTHON})
    extract_into(conn, tmp_path, second, {"svc.py": PYTHON})
    source = conn.fetch_scalar("SELECT id FROM core.entity WHERE analysis_id = %s LIMIT 1", str(first))
    target = conn.fetch_scalar("SELECT id FROM core.entity WHERE analysis_id = %s LIMIT 1", str(second))
    other_file = conn.fetch_scalar("SELECT id FROM core.source_file WHERE analysis_id = %s", str(second))

    for statement, args in (
        ("""INSERT INTO core.repository_edge (analysis_id, source_entity_id, target_entity_id, relationship_type,
                certainty, extractor, extractor_version) VALUES (%s, %s, %s, 'CALLS', 'LOW', 'x', 'x')""",
         (str(first), source, target)),
        ("""INSERT INTO core.entity (analysis_id, file_id, entity_type, name, stable_key, extractor_version)
            VALUES (%s, %s, 'CLASS', 'X', 'cross-file', 'x')""", (str(first), other_file)),
    ):
        conn.execute("SAVEPOINT cross_analysis")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(statement, *args)
        conn.execute("ROLLBACK TO SAVEPOINT cross_analysis")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_pipeline_parks_in_awaiting_stage_and_resumes_without_reacquiring(conn, tmp_path):
    from test_analysis_postgres import _local_repository, _process_claimed

    from backend.routers.analyses import _get_analysis, _state_event
    from backend.services.analysis_pipeline import AnalysisPipeline
    from backend.services.analysis_worker import claim_analysis_transactionally, complete_attempt
    from backend.services.extraction.stage import StructuralExtractionStage
    from backend.services.repository_acquisition import GitRepositoryAcquirer

    source, _ = _local_repository(tmp_path)
    (source / "svc.py").write_bytes(PYTHON)
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", *identity, "add", "svc.py"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", *identity, "commit", "-qm", "two"], cwd=source, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()

    name = f"repo-{uuid4().hex[:8]}"
    created = create_or_reuse_analysis(
        conn, canonical_repository_key=f"github:test-owner/{name}", owner_name="test-owner", repository_name=name,
        clone_url=f"https://github.com/test-owner/{name}.git", requested_reference="main",
        resolved_commit_sha=sha, structural_pipeline_version="structural-test",
    )
    acquisitions = []

    class CountingAcquirer(GitRepositoryAcquirer):
        def acquire(self, request, **kwargs):
            acquisitions.append(request.attempt_id)
            return super().acquire(request, **kwargs)

    acquirer = CountingAcquirer(workdir_root=tmp_path / "work", remote_url_for=lambda key: source.as_uri(),
                                allowed_protocols=("file",))
    structural_only = AnalysisPipeline(lambda: conn, acquirer, structural_stage=StructuralExtractionStage(lambda: conn),
                                       ownership_check_seconds=60)

    first = asyncio.run(_process_claimed(conn, structural_only, created))

    # Extraction's attempt succeeds; the analysis rests in AWAITING_STAGE, neither failed nor "indexing".
    state = _get_analysis(conn, created.analysis_id)
    assert (state.status, state.completed_at, state.failure_code) == ("AWAITING_STAGE", None, None)
    assert (state.last_completed_stage, state.awaiting_stage) == ("EXTRACTING", "INDEXING")
    assert conn.fetch_scalar("SELECT status FROM core.analysis_attempt WHERE id = %s", str(first)) == "SUCCEEDED"
    event = _state_event(state, 1)
    assert event["message"] == "Completed through extracting; waiting for the indexing stage"
    entities = counts(conn, created.analysis_id)["entities"]
    assert entities > 3 and list((tmp_path / "work").iterdir()) == []

    # A worker that cannot run INDEXING leaves it parked.
    assert structural_only.resumable_stages == ()
    assert claim_analysis_transactionally(lambda: conn, structural_only.resumable_stages) is None

    # Once an indexing stage is deployed, the worker resumes it under a new attempt.
    indexed = []

    async def indexing_stage(result):
        indexed.append(result)
        return "SUCCEEDED"

    with_indexing = AnalysisPipeline(
        lambda: conn, acquirer, ownership_check_seconds=60,
        structural_stage=StructuralExtractionStage(lambda: conn, next_stage=indexing_stage),
    )
    assert with_indexing.resumable_stages == ("INDEXING",)
    analysis_id, second = claim_analysis_transactionally(lambda: conn, with_indexing.resumable_stages)
    assert (analysis_id, _get_analysis(conn, analysis_id).status) == (created.analysis_id, "INDEXING")
    assert second != first

    outcome = asyncio.run(with_indexing(analysis_id, second))
    asyncio.run(complete_attempt(lambda: conn, analysis_id=analysis_id, attempt_id=second, outcome=outcome))

    assert acquisitions == [first]  # no second acquisition
    assert counts(conn, created.analysis_id)["entities"] == entities  # no second extraction
    assert indexed[0].commit_sha == sha and indexed[0].attempt_id == second
    state = _get_analysis(conn, created.analysis_id)
    assert (state.status, state.awaiting_stage, state.last_completed_stage) == ("READY", None, "EXTRACTING")


def test_a_failed_resume_returns_to_awaiting_stage_not_to_acquisition(conn):
    from backend.services.analysis_lifecycle import requeue_failed_analysis

    analysis_id = create_analysis(conn)
    conn.execute("""UPDATE core.analysis SET status = 'FAILED', last_completed_stage = 'EXTRACTING',
                    awaiting_stage = 'INDEXING' WHERE id = %s""", str(analysis_id))
    asyncio.run(requeue_failed_analysis(conn, analysis_id, requested_by="pytest"))
    assert conn.fetch_scalar("SELECT status FROM core.analysis WHERE id = %s", str(analysis_id)) == "AWAITING_STAGE"


def test_awaiting_stage_requires_its_progress_columns(conn):
    import psycopg

    analysis_id = create_analysis(conn)
    conn.execute("SAVEPOINT incomplete")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE core.analysis SET status = 'AWAITING_STAGE' WHERE id = %s", str(analysis_id))
    conn.execute("ROLLBACK TO SAVEPOINT incomplete")


def test_generated_and_vendored_flags_are_persisted(conn, tmp_path):
    analysis_id = create_analysis(conn)
    extract_into(conn, tmp_path, analysis_id, {
        "app/service.py": PYTHON,
        "vendor/requests/api.py": b"def get(url):\n    return url\n",
        "proto/user_pb2.py": b"# Generated by the protocol buffer compiler.  DO NOT EDIT!\nX = 1\n",
    })
    rows = dict(((path, (generated, vendored)) for path, generated, vendored in conn.fetch_all(
        "SELECT path, is_generated, is_vendored FROM core.source_file WHERE analysis_id = %s", str(analysis_id))))
    assert rows == {"app/service.py": (False, False), "vendor/requests/api.py": (False, True),
                    "proto/user_pb2.py": (True, False)}


def test_downstream_reads_are_first_party_by_default(conn, tmp_path):
    from backend.services.extraction.queries import IRScope, fetch_edges, fetch_entities

    analysis_id = create_analysis(conn)
    extract_into(conn, tmp_path, analysis_id, {
        "app/service.py": b"from vendor.lib import helper\n\n\ndef run():\n    return helper()\n",
        "vendor/lib.py": b"def helper():\n    return 1\n\n\ndef inner():\n    return helper()\n",
        "proto/user_pb2.py": b"# Generated by the protocol buffer compiler.  DO NOT EDIT!\ndef build():\n    return 1\n",
    })

    default = {e["stable_key"] for e in fetch_entities(conn, analysis_id, entity_types=["FUNCTION"])}
    assert default == {"function:app/service.py::run"}
    everything = {e["stable_key"] for e in fetch_entities(conn, analysis_id, scope=IRScope.diagnostic(),
                                                          entity_types=["FUNCTION"])}
    assert everything == {"function:app/service.py::run", "function:vendor/lib.py::helper",
                          "function:vendor/lib.py::inner", "function:proto/user_pb2.py::build"}
    only_vendored = {e["stable_key"] for e in fetch_entities(conn, analysis_id, scope=IRScope(include_vendored=True),
                                                             entity_types=["FUNCTION"])}
    assert "function:vendor/lib.py::helper" in only_vendored and "function:proto/user_pb2.py::build" not in only_vendored

    edges = fetch_edges(conn, analysis_id, relationship_types=["CALLS", "CONTAINS"])
    assert all(not e[k].startswith(("function:vendor/", "function:proto/", "file:vendor/", "file:proto/"))
               for e in edges for k in ("source_key", "target_key"))
    # The first-party call to the (unresolved, file-less) helper placeholder stays visible.
    assert any(e["source_key"] == "function:app/service.py::run" and e["target_key"].startswith("external:")
               for e in edges)
    assert any(e["source_key"] == "function:vendor/lib.py::inner"
               for e in fetch_edges(conn, analysis_id, scope=IRScope.diagnostic(), relationship_types=["CALLS"]))


def test_deleting_an_analysis_finds_evidence_links_by_index(conn, tmp_path):
    """Deleting an analysis cascades through evidence into both link tables. Without an
    index on evidence_id each cascaded evidence row scans them, so deletion is quadratic."""
    analysis_id = create_analysis(conn)
    module = b"".join(b"def f%d():\n    return f%d()\n\n\n" % (i, i + 1) for i in range(40))
    extract_into(conn, tmp_path, analysis_id, {f"pkg/m{n}.py": module for n in range(20)})
    assert counts(conn, analysis_id)["edge_links"] > 1000

    def scans():
        # This transaction's own counters, including the cascade's referential-action queries.
        return {row[0]: (row[1], row[2]) for row in conn.fetch_all(
            "SELECT relname, seq_scan, idx_scan FROM pg_stat_xact_user_tables "
            "WHERE schemaname = 'core' AND relname IN ('entity_evidence', 'edge_evidence')")}

    before = scans()
    conn.execute("DELETE FROM core.analysis WHERE id = %s", str(analysis_id))
    after = scans()

    for table in ("entity_evidence", "edge_evidence"):
        assert after[table][0] == before[table][0], f"{table} was sequentially scanned"
        assert after[table][1] > before[table][1], f"{table} was not reached by index"
    assert counts(conn, analysis_id) == dict.fromkeys(counts(conn, analysis_id), 0)
