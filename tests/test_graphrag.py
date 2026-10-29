"""The GraphRAG slice: Louvain, routing (pure), and projection -> communities -> summaries -> answers
against the canonical schema in one rolled-back transaction (stages share it; commits are no-ops)."""

import asyncio
import os
from uuid import uuid4

import pytest

from backend.services.graphrag.ask import ask, route
from backend.services.graphrag.louvain import louvain, modularity

# --- pure ----------------------------------------------------------------------------------------


def cliques():
    left = [("a", "b"), ("a", "c"), ("b", "c"), ("c", "d"), ("a", "d"), ("b", "d")]
    right = [(x.upper(), y.upper()) for x, y in left]
    return [(x, y, 1.0) for x, y in left + right + [("d", "A")]]


def test_louvain_separates_two_cliques_and_is_deterministic():
    edges = cliques()
    nodes = {n for e in edges for n in e[:2]}
    levels = louvain(nodes, edges, seed=7)
    coarse = levels[-1]
    assert len(set(coarse.values())) == 2
    assert len({coarse[n] for n in "abcd"}) == 1 and len({coarse[n] for n in "ABCD"}) == 1
    assert modularity(edges, coarse) > 0.3
    assert louvain(nodes, list(reversed(edges)), seed=7)[-1] == coarse  # input order does not matter


def test_louvain_keeps_isolated_nodes_apart():
    levels = louvain(["x", "y", "z"], [("x", "y", 1.0)])
    assert len(set(levels[-1].values())) == 2 and levels[-1]["z"] not in (levels[-1]["x"],)


@pytest.mark.parametrize("question, mode", [
    ("What is the overall architecture?", "GLOBAL"),
    ("Which databases can the application run against?", "GLOBAL"),
    ("How is the application organized into components?", "GLOBAL"),
    ("How does get_current_user validate the token?", "LOCAL"),
    ("What does processFindForm do?", "LOCAL"),
    ("How does the app stop an owner from adding two pets with the same name?", "LOCAL"),
    ("How are passwords hashed?", "LOCAL"),
])
def test_routing(question, mode):
    assert route(question) == mode


# --- against the canonical schema ----------------------------------------------------------------

needs_db = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")

FILES = {
    "app/__init__.py": b"",
    "app/models.py": b"class Base:\n    def save(self):\n        return True\n\n\n"
                     b"class User(Base):\n    def display_name(self):\n        return 'user'\n",
    "app/service.py": b"from app.models import User\n\n\ndef make_user():\n    return User()\n\n\n"
                      b"def greet():\n    return make_user().display_name()\n",
    "app/api.py": b"from app import service\n\n\ndef handler():\n    return service.greet()\n\n\n"
                  b"def health():\n    return 'ok'\n",
    "tests/test_api.py": b"from app.api import handler\n\n\ndef test_handler():\n    assert handler()\n",
}


@pytest.fixture
def conn():
    from test_semantic_indexing_postgres import SharedConnection

    from backend.services.postgres import make_connection_factory

    real = make_connection_factory(os.environ["TEST_DATABASE_URL"])()
    try:
        yield SharedConnection(real)
    finally:
        real.rollback()
        real.close()


@pytest.fixture
def indexed(conn, tmp_path):
    """An analysis extracted and indexed (offline vectors), left in INDEXING as the indexing stage leaves it."""
    from test_extraction_postgres import create_analysis, extract_into

    from backend.services.semantic.embeddings import OfflineEmbeddingProvider
    from backend.services.semantic.profiles import DEFAULT_CHUNK_PROFILE, OFFLINE_EMBEDDING_PROFILE
    from backend.services.semantic.source_content import retain_source_content
    from backend.services.semantic.store import embed_analysis_chunks, index_analysis_chunks

    analysis_id = create_analysis(conn, sha=uuid4().hex[:8] * 5)
    extract_into(conn, tmp_path, analysis_id, FILES)
    retain_source_content(conn, analysis_id, FILES.__getitem__)
    index_analysis_chunks(conn, analysis_id, DEFAULT_CHUNK_PROFILE)
    embed_analysis_chunks(conn, analysis_id, DEFAULT_CHUNK_PROFILE, OFFLINE_EMBEDDING_PROFILE, OfflineEmbeddingProvider())
    conn.execute("UPDATE core.analysis SET status = 'INDEXING' WHERE id = %s", str(analysis_id))
    return analysis_id


def status(conn, analysis_id):
    return conn.fetch_scalar("SELECT status FROM core.analysis WHERE id = %s", str(analysis_id))


def result_for(analysis_id):
    from types import SimpleNamespace

    return SimpleNamespace(analysis_id=analysis_id, attempt_id=uuid4())


@needs_db
def test_projection_resolves_references_to_internal_entities(conn, indexed):
    from backend.services.graphrag.projection import build_graph

    graph = build_graph(conn, indexed)
    names = {n.id: n.qualified_name or n.name for n in graph.nodes.values()}
    edges = {(names[e.source], e.relation, names[e.target]) for e in graph.edges}
    derived = {(names[e.source], e.relation, names[e.target]) for e in graph.edges if e.derived}
    assert ("User", "EXTENDS", "Base") in edges  # canonical or resolved, inheritance is in the graph
    assert ("make_user", "CALLS", "User") in derived
    assert ("handler", "CALLS", "greet") in derived  # service.greet(): the qualifier names the module
    assert any(r == "IMPORTS" and t == "app/models.py" for _, r, t in derived)
    assert graph.unresolved >= 0 and graph.fingerprint == build_graph(conn, indexed).fingerprint


def degraded_reasons(conn, analysis_id):
    return [item["reason"] for item in conn.fetch_scalar(
        "SELECT metadata->'degraded' FROM core.analysis WHERE id = %s", str(analysis_id)) or []]


@needs_db
def test_the_stage_takes_an_indexed_analysis_to_success_and_answers_are_grounded(conn, indexed):
    from backend.services.graphrag.llm import OfflineTextProvider
    from backend.services.graphrag.stage import GraphRAGStage
    from backend.services.semantic.embeddings import OfflineEmbeddingProvider
    from backend.services.semantic.profiles import DEFAULT_CHUNK_PROFILE, OFFLINE_EMBEDDING_PROFILE

    provider = OfflineTextProvider()
    # Offline summaries are placeholders: the analysis completes, degraded, and says why.
    assert asyncio.run(GraphRAGStage(lambda: conn, provider)(result_for(indexed))) == "DEGRADED"
    assert status(conn, indexed) == "SUMMARIZING"  # the worker moves it to DEGRADED
    assert "OFFLINE_PROVIDER" in degraded_reasons(conn, indexed)
    snapshot = conn.fetch_one("SELECT status, node_count FROM graphrag.graph_snapshot WHERE analysis_id = %s",
                              str(indexed))
    assert snapshot[0] == "ACTIVE" and snapshot[1] > 0
    test_members = conn.fetch_scalar(
        "SELECT count(*) FROM graphrag.community_member m JOIN core.entity e ON e.id = m.entity_id "
        "JOIN core.source_file f ON f.id = e.file_id WHERE e.analysis_id = %s AND f.path LIKE 'tests/%%'", str(indexed))
    assert test_members == 0
    ready = conn.fetch_scalar(
        "SELECT count(*) FROM graphrag.community_summary s JOIN graphrag.community c ON c.id = s.community_id "
        "JOIN graphrag.community_run r ON r.id = c.community_run_id JOIN graphrag.graph_snapshot g "
        "ON g.id = r.graph_snapshot_id WHERE g.analysis_id = %s AND s.status = 'READY'", str(indexed))
    assert ready >= 1
    calls = len(provider.calls)
    asyncio.run(GraphRAGStage(lambda: conn, provider)(result_for(indexed)))  # a resumed attempt
    assert len(provider.calls) == calls  # identical inputs reuse every summary

    options = dict(embedder=OfflineEmbeddingProvider(), chunk_profile=DEFAULT_CHUNK_PROFILE,
                   embedding_profile=OFFLINE_EMBEDDING_PROFILE, provider=provider)
    local = ask(conn, indexed, "How does handler greet the user?", mode="local", **options)
    assert local.mode == "LOCAL" and local.grounding_status == "SUPPORTED"
    assert local.citations and all(c["file_path"] for c in local.citations if c["kind"] == "code")
    assert any(item.source_type == "ENTITY" for item in local.context)  # graph expansion added neighbours
    cited = conn.fetch_scalar(
        "SELECT count(*) FROM graphrag.answer_citation ac JOIN graphrag.answer a ON a.id = ac.answer_id "
        "JOIN core.evidence ev ON ev.id = ac.evidence_id WHERE a.query_run_id = %s AND ev.analysis_id = %s",
        str(local.query_run_id), str(indexed))
    assert cited >= 1
    events = [row[0] for row in conn.fetch_all("SELECT event_type FROM graphrag.trace_event WHERE query_run_id = %s "
                                               "ORDER BY sequence_number", str(local.query_run_id))]
    assert events == ["route", "seed", "expand", "generate"]

    overall = ask(conn, indexed, "What is the overall architecture?", **options)
    kinds = [item.kind for item in overall.context]
    assert overall.mode == "GLOBAL" and "community" in kinds
    assert 0 < kinds.count("code") <= 4  # vector anchors reach files outside any community
    assert conn.fetch_scalar("SELECT status FROM graphrag.query_run WHERE id = %s",
                             str(overall.query_run_id)) in ("SUCCEEDED", "DEGRADED")


@needs_db
def test_without_a_text_provider_the_analysis_parks_after_projection(conn, indexed):
    from backend.services.graphrag.stage import GraphRAGStage

    stage = GraphRAGStage(lambda: conn, None)
    assert stage.resumable_stages == ("PROJECTING",)
    assert asyncio.run(stage(result_for(indexed))) == "AWAITING_STAGE"
    assert status(conn, indexed) == "PROJECTING"  # the worker parks it awaiting SUMMARIZING


@needs_db
def test_a_provider_failure_fails_the_stage_and_a_retry_reuses_finished_summaries(conn, indexed):
    from backend.services.graphrag.llm import GenerationUnavailable, OfflineTextProvider
    from backend.services.graphrag.stage import GraphRAGStage
    from backend.services.graphrag.summaries import SummaryGenerationIncomplete

    class Flaky(OfflineTextProvider):
        def __init__(self, fail_after):
            super().__init__()
            self.fail_after = fail_after

        def generate_json(self, system, prompt, schema):
            if len(self.calls) >= self.fail_after:
                self.calls.append(prompt)
                raise GenerationUnavailable("provider unavailable (503)")
            return super().generate_json(system, prompt, schema)

    with pytest.raises(SummaryGenerationIncomplete) as caught:
        asyncio.run(GraphRAGStage(lambda: conn, Flaky(fail_after=1), summary_workers=1)(result_for(indexed)))
    assert caught.value.failure_code == "SUMMARIES_INCOMPLETE"
    statuses = [row[0] for row in conn.fetch_all(
        "SELECT s.status FROM graphrag.community_summary s JOIN graphrag.community c ON c.id = s.community_id "
        "JOIN graphrag.community_run r ON r.id = c.community_run_id JOIN graphrag.graph_snapshot g "
        "ON g.id = r.graph_snapshot_id WHERE g.analysis_id = %s", str(indexed))]
    assert "FAILED" in statuses and "READY" in statuses

    retry = OfflineTextProvider()
    assert asyncio.run(GraphRAGStage(lambda: conn, retry)(result_for(indexed))) == "DEGRADED"  # offline text
    assert len(retry.calls) == statuses.count("FAILED")  # only what failed is generated again


@needs_db
def test_complete_summaries_succeed_and_clear_an_earlier_degradation(conn, indexed):
    from backend.services.graphrag.llm import OfflineTextProvider
    from backend.services.graphrag.stage import GraphRAGStage

    assert asyncio.run(GraphRAGStage(lambda: conn, OfflineTextProvider())(result_for(indexed))) == "DEGRADED"

    class Model(OfflineTextProvider):
        """Stands in for a real model: same supported output, but not the placeholder provider."""
        name = "test-model"

    # Every community of this fixture has evidence, so a model's summaries are all supported.
    assert asyncio.run(GraphRAGStage(lambda: conn, Model())(result_for(indexed))) == "SUCCEEDED"
    assert degraded_reasons(conn, indexed) == []


def test_degradation_names_placeholder_text_and_communities_without_a_summary():
    from types import SimpleNamespace

    from backend.services.graphrag.stage import degradation

    class Counts:
        def __init__(self, unsupported):
            self.unsupported = unsupported

        def fetch_scalar(self, sql, *params):
            return self.unsupported

    model, offline = SimpleNamespace(name="gemini"), SimpleNamespace(name="offline")
    assert degradation(Counts(0), uuid4(), model) == []
    [unsupported] = degradation(Counts(3), uuid4(), model)
    assert unsupported["reason"] == "UNSUPPORTED" and unsupported["count"] == 3 and "3 components" in unsupported["detail"]
    assert [item["reason"] for item in degradation(Counts(3), uuid4(), offline)] == ["OFFLINE_PROVIDER", "UNSUPPORTED"]
