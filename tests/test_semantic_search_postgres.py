"""Vector search over one analysis (SS-ML-302), in the service and through the API (rolled back)."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import analyses
from backend.services.semantic.embeddings import OfflineEmbeddingProvider
from backend.services.semantic.profiles import DEFAULT_CHUNK_PROFILE, OFFLINE_EMBEDDING_PROFILE
from backend.services.semantic.search import IndexNotReady, search_analysis
from backend.services.semantic.store import embed_analysis_chunks, index_analysis_chunks

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")

from test_semantic_evaluation_postgres import FILES, KeywordProvider, conn, indexed  # noqa: E402,F401

CHUNKS = replace(DEFAULT_CHUNK_PROFILE, profile_version=7, include_vendored=True)


class RollbackFreeConnection:
    """The test's one transaction, kept alive across the router's commit, rollback and close."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.include_router(analyses.router)
    analyses.configure_connection_factory(lambda: RollbackFreeConnection(conn))
    analyses.configure_search(KeywordProvider(), CHUNKS, OFFLINE_EMBEDDING_PROFILE)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        analyses.configure_search(None)


COMPLETED_BEFORE = {"INDEXING": "EXTRACTING", "PROJECTING": "INDEXING"}


def set_status(conn, analysis_id, status, awaiting=None):
    conn.execute("UPDATE core.analysis SET status = %s, awaiting_stage = %s, last_completed_stage = %s "
                 "WHERE id = %s", status, awaiting, COMPLETED_BEFORE.get(awaiting), str(analysis_id))


def second_analysis(conn, tmp_path, files):
    from test_extraction_postgres import create_analysis, extract_into

    analysis_id = create_analysis(conn, sha=uuid4().hex[:8] * 5)
    extract_into(conn, tmp_path, analysis_id, files)
    index_analysis_chunks(conn, analysis_id, CHUNKS, files.__getitem__)
    embed_analysis_chunks(conn, analysis_id, CHUNKS, OFFLINE_EMBEDDING_PROFILE, KeywordProvider())
    set_status(conn, analysis_id, "AWAITING_STAGE", "PROJECTING")
    return analysis_id


def test_search_ranks_by_meaning_and_returns_versions_and_evidence(conn, indexed, client):
    analysis_id, version, _ = indexed
    set_status(conn, analysis_id, "AWAITING_STAGE", "PROJECTING")  # where indexing leaves it today
    response = client.post(f"/api/v1/analyses/{analysis_id}/search",
                           json={"query": "total of the prices sum with tax_rate", "k": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repository_version_id"] == str(version) and len(body["commit_sha"]) == 40
    assert body["retrieval_mode"] == "VECTOR_ONLY"
    assert body["embedding_profile"] == "offline-sha256-3072/1" and body["chunk_profile"] == "symbol-windows/7"
    assert body["index_complete"] and body["indexed_chunks"] == body["embedded_chunks"] > 0
    top = body["results"][0]
    assert top["rank"] == 1 and top["entity_stable_key"] == "method:app/cart.py::Cart.total"
    assert top["entity_type"] == "METHOD" and "def total" in top["content"]
    assert top["evidence"] == {"file_path": "app/cart.py", "start_line": 2, "end_line": 3}
    scores = [hit["score"] for hit in body["results"]]
    assert scores == sorted(scores, reverse=True) and len(scores) == 3
    assert set(body["latency_ms"]) == {"embed", "search", "total"}


def test_vendored_code_is_excluded_unless_asked_for(conn, indexed, client):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, "READY")
    query = {"query": "tax_rate return", "k": 10}
    default = client.post(f"/api/v1/analyses/{analysis_id}/search", json=query).json()["results"]
    everything = client.post(f"/api/v1/analyses/{analysis_id}/search",
                             json={**query, "include_third_party": True}).json()["results"]
    assert default and not any(hit["evidence"]["file_path"].startswith("vendor/") for hit in default)
    assert any(hit["evidence"]["file_path"] == "vendor/lib/tax.py" for hit in everything)


def test_the_language_filter_applies_before_the_top_k_cut(conn, indexed, client):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, "READY")
    url = f"/api/v1/analyses/{analysis_id}/search"
    assert client.post(url, json={"query": "cart total", "language": "python"}).json()["results"]
    assert client.post(url, json={"query": "cart total", "language": "java"}).json()["results"] == []


def test_search_never_crosses_into_another_analysis(conn, indexed, client, tmp_path):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, "READY")
    # The other repository matches the query far better than anything in the searched one.
    other = second_analysis(conn, tmp_path, {"app/billing.py": b"def invoice_total_prices_tax_rate():\n"
                                                              b"    return total_prices_tax_rate\n"})
    hits = client.post(f"/api/v1/analyses/{analysis_id}/search",
                       json={"query": "invoice total prices tax_rate", "k": 50}).json()["results"]
    owners = {row[0] for row in conn.fetch_all("SELECT DISTINCT analysis_id FROM semantic.chunk "
                                               "WHERE id = ANY(%s::uuid[])", [hit["chunk_id"] for hit in hits])}
    assert hits and owners == {analysis_id} and other not in owners


def test_a_filtered_hnsw_scan_still_returns_k_results(conn, indexed):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, "READY")
    conn.fetch_scalar("SELECT semantic.create_profile_hnsw_index(%s, %s)", OFFLINE_EMBEDDING_PROFILE.profile_key,
                      OFFLINE_EMBEDDING_PROFILE.profile_version)
    # Force the index and shrink its candidate list: without iterative scans it yields one row.
    conn.execute("SET LOCAL enable_seqscan = off")
    conn.execute("SET LOCAL enable_sort = off")
    conn.execute("SET LOCAL hnsw.ef_search = 1")
    result = search_analysis(conn, analysis_id, "cart total", KeywordProvider(), CHUNKS, OFFLINE_EMBEDDING_PROFILE,
                             k=3)
    assert len(result.hits) == 3


@pytest.mark.parametrize("status,awaiting", [("QUEUED", None), ("EXTRACTING", None), ("INDEXING", None),
                                             ("AWAITING_STAGE", "INDEXING")])
def test_an_analysis_that_is_not_indexed_yet_is_refused(conn, indexed, client, status, awaiting):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, status, awaiting)
    response = client.post(f"/api/v1/analyses/{analysis_id}/search", json={"query": "cart total"})
    assert response.status_code == 409
    assert "has not been indexed yet" in response.json()["detail"]


def test_an_analysis_indexed_under_other_profiles_is_refused(conn, indexed, client):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, "READY")
    other_chunks = replace(CHUNKS, profile_version=8)
    analyses.configure_search(KeywordProvider(), other_chunks, OFFLINE_EMBEDDING_PROFILE)
    response = client.post(f"/api/v1/analyses/{analysis_id}/search", json={"query": "cart total"})
    assert response.status_code == 409 and "no chunks under chunk profile symbol-windows/8" in response.text

    other_vectors = replace(OFFLINE_EMBEDDING_PROFILE, profile_version=2)
    with pytest.raises(IndexNotReady, match="no vectors under embedding profile offline-sha256-3072/2"):
        search_analysis(conn, analysis_id, "cart total", KeywordProvider(), CHUNKS, other_vectors)


def test_unknown_analyses_bad_requests_and_a_mismatched_provider_fail_clearly(conn, indexed, client):
    analysis_id, _, _ = indexed
    set_status(conn, analysis_id, "READY")
    url = f"/api/v1/analyses/{analysis_id}/search"
    assert client.post(f"/api/v1/analyses/{uuid4()}/search", json={"query": "cart"}).status_code == 404
    assert client.post(url, json={"query": ""}).status_code == 422
    assert client.post(url, json={"query": "cart", "k": 51}).status_code == 422
    assert client.post(url, json={"query": "   "}).status_code == 400

    analyses.configure_search(OfflineEmbeddingProvider(), CHUNKS, replace(OFFLINE_EMBEDDING_PROFILE,
                                                                         provider="google"))
    response = client.post(url, json={"query": "cart"})
    assert response.status_code == 503 and response.json()["detail"].endswith("EMBEDDING_PROFILE_INCOMPATIBLE")

    analyses.configure_search(None)
    assert client.post(url, json={"query": "cart"}).status_code == 503


def test_recent_analyses_are_listed_newest_first_with_their_repository(conn, indexed, client):
    analysis_id, _, _ = indexed
    listed = client.get("/api/v1/analyses", params={"limit": 100})
    assert listed.status_code == 200
    rows = listed.json()
    created = [row["created_at"] for row in rows]
    assert created == sorted(created, reverse=True)
    mine = next(row for row in rows if row["analysis_id"] == str(analysis_id))
    assert mine["canonical_repository_key"] and mine["commit_sha"] and mine["status"]
    assert len(client.get("/api/v1/analyses", params={"limit": 1}).json()) == 1
    assert client.get("/api/v1/analyses", params={"limit": 0}).status_code == 422
