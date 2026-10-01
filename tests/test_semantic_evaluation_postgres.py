"""Frozen benchmark datasets and audited evaluation runs against the canonical schema (rolled back)."""

import hashlib
import os
import re
from dataclasses import replace
from uuid import uuid4

import pytest

from backend.services.semantic.benchmark import (
    JudgmentTarget, add_judgment, add_question, add_repository, create_dataset, freeze_dataset,
)
from backend.services.semantic.embeddings import ProviderEmbedding
from backend.services.semantic.evaluation_runs import RunConfiguration, run_vector_evaluation, start_run
from backend.services.semantic.profiles import DEFAULT_CHUNK_PROFILE, DEFAULT_EMBEDDING_PROFILE, canonical_json
from backend.services.semantic.retrieval import search_query, vector_search
from backend.services.semantic.store import (
    embed_analysis_chunks, index_analysis_chunks, register_chunk_profile, register_embedding_profile,
)

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")

FILES = {
    "app/cart.py": b"class Cart:\n    def total(self, prices):\n        return sum(prices) * tax_rate()\n\n"
                   b"    def empty(self):\n        return not self.items\n\n"
                   b"def tax_rate():\n    return 1.2\n",
    "vendor/lib/tax.py": b"def tax_rate():\n    return 1.1\n",
}
CONFIG = RunConfiguration(retriever_profile="vector-cosine", retriever_version="1", query_transformation_version="none/1",
                          evaluation_code_version="stacksniffer-eval/1", git_commit="a" * 40)


class KeywordProvider:
    """A bag-of-words vector, so chunks sharing words with the query rank first."""

    def embed(self, texts, *, profile, task):
        results = []
        for text in texts:
            values = [0.0] * profile.dimension
            values[-1] = 0.01  # never the zero vector
            for word in re.findall(r"[a-z_]+", text.lower()):
                values[int(hashlib.sha256(word.encode()).hexdigest(), 16) % (profile.dimension - 1)] += 1.0
            results.append(ProviderEmbedding(values, {"provider": "keyword", "task_type": task}))
        return results


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


@pytest.fixture
def indexed(conn, tmp_path):
    """An analysis with chunks and vectors (vendored code included, to test default exclusion)."""
    from test_extraction_postgres import create_analysis, extract_into

    analysis_id = create_analysis(conn, sha=uuid4().hex[:8] * 5)
    extract_into(conn, tmp_path, analysis_id, FILES)
    chunks = replace(DEFAULT_CHUNK_PROFILE, profile_version=7, include_vendored=True)
    index_analysis_chunks(conn, analysis_id, chunks, FILES.__getitem__)
    embed_analysis_chunks(conn, analysis_id, chunks, DEFAULT_EMBEDDING_PROFILE, KeywordProvider())
    version = conn.fetch_scalar("SELECT repository_version_id FROM core.analysis WHERE id = %s", str(analysis_id))
    return analysis_id, version, chunks


def build_dataset(conn, version_id, key=None, dataset_version=1):
    dataset_id = create_dataset(conn, key or f"cart-bench-{uuid4().hex[:6]}", dataset_version,
                                "questions about the cart", "stacksniffer-bench/1")
    add_repository(conn, dataset_id, version_id)
    total = add_question(conn, dataset_id, version_id, "q-total", "total of the prices sum with tax_rate",
                         "code_search", "SYMBOL")
    add_judgment(conn, total, JudgmentTarget(entity_stable_key="method:app/cart.py::Cart.total"), 3, "HUMAN",
                 "reviewer-a", "rubric/1")
    add_judgment(conn, total, JudgmentTarget(file_path="app/cart.py", start_line=8, end_line=9), 1, "HUMAN",
                 "reviewer-a", "rubric/1", notes="the tax rate helper")
    empty = add_question(conn, dataset_id, version_id, "q-empty", "empty when self has no items", "code_search",
                         "SYMBOL")
    add_judgment(conn, empty, JudgmentTarget(entity_stable_key="method:app/cart.py::Cart.empty"), 3, "HUMAN",
                 "reviewer-a", "rubric/1")
    return dataset_id, total


def raises(conn, statement, *args, error="IntegrityError"):
    import psycopg

    conn.execute("SAVEPOINT expect_error")
    with pytest.raises(getattr(psycopg, error)):
        conn.execute(statement, *args)
    conn.execute("ROLLBACK TO SAVEPOINT expect_error")


# --- datasets ------------------------------------------------------------------------------------


def test_freezing_fingerprints_the_rows_canonical_manifest(conn, indexed):
    _, version, _ = indexed
    dataset_id, _ = build_dataset(conn, version)
    fingerprint = freeze_dataset(conn, dataset_id)
    status, canonical, stored = conn.fetch_one(
        "SELECT status, canonical_manifest, fingerprint FROM evaluation.dataset WHERE id = %s", str(dataset_id))
    assert (status, stored) == ("FROZEN", fingerprint)
    assert hashlib.sha256(canonical.encode()).hexdigest() == fingerprint
    manifest = conn.fetch_scalar("SELECT evaluation.dataset_manifest(%s)", str(dataset_id))
    assert canonical == canonical_json(manifest)
    assert [q["question_key"] for q in manifest["questions"]] == ["q-empty", "q-total"]
    assert len(manifest["questions"][1]["judgments"]) == 2 and manifest["repositories"][0]["commit_sha"]


def test_a_frozen_dataset_rejects_every_change(conn, indexed):
    _, version, _ = indexed
    dataset_id, question_id = build_dataset(conn, version)
    freeze_dataset(conn, dataset_id)
    d, q = str(dataset_id), str(question_id)
    raises(conn, "INSERT INTO evaluation.question (dataset_id, repository_version_id, question_key, question_text, "
                 "task_category, expected_answer_scope) VALUES (%s, %s, 'q-new', 'new', 'code_search', 'SYMBOL')",
           d, str(version))
    raises(conn, "UPDATE evaluation.question SET question_text = 'changed' WHERE id = %s", q)
    raises(conn, "DELETE FROM evaluation.question WHERE id = %s", q)
    raises(conn, "UPDATE evaluation.relevance_judgment SET relevance_grade = 0 WHERE question_id = %s", q)
    raises(conn, "DELETE FROM evaluation.relevance_judgment WHERE question_id = %s", q)
    raises(conn, "INSERT INTO evaluation.relevance_judgment (question_id, repository_version_id, entity_stable_key, "
                 "relevance_grade, judgment_source, annotator, annotator_version) "
                 "VALUES (%s, %s, 'x', 1, 'HUMAN', 'b', '1')", q, str(version))
    raises(conn, "DELETE FROM evaluation.dataset_repository WHERE dataset_id = %s", d)
    raises(conn, "UPDATE evaluation.dataset SET fingerprint = %s WHERE id = %s", "0" * 64, d)
    raises(conn, "UPDATE evaluation.dataset SET status = 'DRAFT', canonical_manifest = NULL, manifest = NULL, "
                 "fingerprint = NULL, frozen_at = NULL WHERE id = %s", d)
    raises(conn, "DELETE FROM evaluation.dataset WHERE id = %s", d)


def test_a_freeze_must_describe_the_rows_exactly(conn, indexed):
    _, version, _ = indexed
    dataset_id, _ = build_dataset(conn, version)
    fake = canonical_json({"questions": []})
    raises(conn, "UPDATE evaluation.dataset SET status = 'FROZEN', canonical_manifest = %s, manifest = %s::jsonb, "
                 "fingerprint = %s, frozen_at = now() WHERE id = %s",
           fake, fake, hashlib.sha256(fake.encode()).hexdigest(), str(dataset_id))
    empty = create_dataset(conn, f"empty-{uuid4().hex[:6]}", 1, "nothing", "v1")
    with pytest.raises(Exception):
        conn.execute("SAVEPOINT empty_freeze")
        freeze_dataset(conn, empty)
    conn.execute("ROLLBACK TO SAVEPOINT empty_freeze")


def test_a_correction_is_a_new_version_with_a_new_fingerprint(conn, indexed):
    _, version, _ = indexed
    key = f"cart-bench-{uuid4().hex[:6]}"
    first, _ = build_dataset(conn, version, key=key)
    second, question = build_dataset(conn, version, key=key, dataset_version=2)
    add_judgment(conn, question, JudgmentTarget(entity_stable_key="class:app/cart.py::Cart"), 1, "HUMAN",
                 "reviewer-b", "rubric/1")
    assert freeze_dataset(conn, first) != freeze_dataset(conn, second)


def test_questions_and_judgments_stay_within_the_datasets_repositories(conn, indexed):
    from test_extraction_postgres import create_analysis

    _, version, _ = indexed
    dataset_id, _ = build_dataset(conn, version)
    other = conn.fetch_scalar("SELECT repository_version_id FROM core.analysis WHERE id = %s",
                              str(create_analysis(conn, sha="f" * 40)))
    raises(conn, "INSERT INTO evaluation.question (dataset_id, repository_version_id, question_key, question_text, "
                 "task_category, expected_answer_scope) VALUES (%s, %s, 'q-x', 'x', 'code_search', 'FILE')",
           str(dataset_id), str(other))
    with pytest.raises(ValueError):
        JudgmentTarget()


# --- runs ----------------------------------------------------------------------------------------


def test_a_run_records_every_input_ranking_and_metric(conn, indexed):
    analysis_id, version, chunks = indexed
    dataset_id, _ = build_dataset(conn, version)
    fingerprint = freeze_dataset(conn, dataset_id)
    run_id = run_vector_evaluation(conn, dataset_id, chunks, DEFAULT_EMBEDDING_PROFILE, CONFIG, {version: analysis_id},
                                   KeywordProvider())
    run = conn.fetch_one(
        "SELECT status, dataset_fingerprint, chunk_profile_fingerprint, embedding_profile_fingerprint, "
        "retriever_version, query_transformation_version, evaluation_code_version, git_commit, metric_configuration, "
        "completed_at IS NOT NULL FROM evaluation.run WHERE id = %s", str(run_id))
    assert run[:8] == ("SUCCEEDED", fingerprint, chunks.fingerprint, DEFAULT_EMBEDDING_PROFILE.fingerprint, "1",
                       "none/1", "stacksniffer-eval/1", "a" * 40)
    assert run[8]["recall_ks"] == [5, 10] and run[9]
    assert conn.fetch_all("SELECT analysis_id FROM evaluation.run_analysis WHERE evaluation_run_id = %s",
                          str(run_id)) == [(analysis_id,)]
    top = conn.fetch_one(
        "SELECT r.stable_chunk_key, r.is_relevant, r.relevance_grade, r.evidence FROM evaluation.ranked_result r "
        "JOIN evaluation.question q ON q.id = r.question_id WHERE r.evaluation_run_id = %s AND q.question_key = "
        "'q-total' AND r.rank = 1", str(run_id))
    assert top[0] == "method:app/cart.py::Cart.total#0" and top[1:3] == (True, 3)
    assert top[3][0]["entity_stable_key"] == "method:app/cart.py::Cart.total" and top[3][0]["credited"]
    metrics = dict(conn.fetch_all("SELECT metric_name, metric_value FROM evaluation.metric "
                                  "WHERE evaluation_run_id = %s AND question_id IS NULL", str(run_id)))
    assert {"recall@5", "recall@10", "mrr", "ndcg@10", "latency_ms_p50", "latency_ms_p95", "failure_rate",
            "empty_result_rate"} <= set(metrics)
    assert metrics["mrr"] == 1.0 and metrics["recall@10"] == 1.0 and metrics["failure_rate"] == 0.0


def test_default_retrieval_excludes_vendored_chunks(conn, indexed):
    analysis_id, _, chunks = indexed
    chunk_profile = register_chunk_profile(conn, chunks)
    embedding_profile = register_embedding_profile(conn, DEFAULT_EMBEDDING_PROFILE)
    query = KeywordProvider().embed(["tax rate"], profile=DEFAULT_EMBEDDING_PROFILE, task="q")[0].values
    default = vector_search(conn, analysis_id, chunk_profile, embedding_profile, "COSINE", query, 10)
    everything = vector_search(conn, analysis_id, chunk_profile, embedding_profile, "COSINE", query, 10,
                               first_party_only=False)
    assert not any(c.file_path.startswith("vendor/") for c in default)
    assert any(c.file_path.startswith("vendor/") for c in everything)
    python_only = vector_search(conn, analysis_id, chunk_profile, embedding_profile, "COSINE", query, 10,
                                language="python")
    assert [c.stable_chunk_key for c in python_only] == [c.stable_chunk_key for c in default]


def test_retrieval_uses_the_first_party_hnsw_index(conn, indexed):
    analysis_id, _, chunks = indexed
    index = conn.fetch_scalar("SELECT semantic.create_profile_hnsw_index(%s, %s)",
                              DEFAULT_EMBEDDING_PROFILE.profile_key, DEFAULT_EMBEDDING_PROFILE.profile_version)
    # With sorting disabled, only an index matching the query's expression and predicate can order it.
    conn.execute("SET LOCAL enable_seqscan = off")
    conn.execute("SET LOCAL enable_sort = off")
    query = "[" + ",".join(["0.01"] * 3072) + "]"
    plan = "\n".join(row[0] for row in conn.fetch_all(
        "EXPLAIN " + search_query("COSINE"), query, str(register_embedding_profile(conn, DEFAULT_EMBEDDING_PROFILE)),
        str(analysis_id), str(register_chunk_profile(conn, chunks)), 10))
    assert index in plan and index.endswith("_fp")


def test_runs_need_a_frozen_dataset_and_are_immutable_once_complete(conn, indexed):
    analysis_id, version, chunks = indexed
    draft, _ = build_dataset(conn, version)
    conn.execute("SAVEPOINT draft_run")
    with pytest.raises(Exception):
        start_run(conn, draft, chunks, DEFAULT_EMBEDDING_PROFILE, CONFIG, {version: analysis_id})
    conn.execute("ROLLBACK TO SAVEPOINT draft_run")
    dataset_id, question_id = build_dataset(conn, version)
    freeze_dataset(conn, dataset_id)
    run_id = str(run_vector_evaluation(conn, dataset_id, chunks, DEFAULT_EMBEDDING_PROFILE, CONFIG,
                                       {version: analysis_id}, KeywordProvider()))
    raises(conn, "UPDATE evaluation.run SET retriever_version = '2' WHERE id = %s", run_id)
    raises(conn, "UPDATE evaluation.ranked_result SET score = 0 WHERE evaluation_run_id = %s", run_id)
    raises(conn, "INSERT INTO evaluation.metric (evaluation_run_id, metric_name, metric_value) VALUES (%s, 'x', 1)",
           run_id)
    raises(conn, "DELETE FROM evaluation.run WHERE id = %s", run_id)


def test_a_failing_question_degrades_the_run_without_aborting_it(conn, indexed):
    analysis_id, version, chunks = indexed
    dataset_id, _ = build_dataset(conn, version)
    freeze_dataset(conn, dataset_id)

    class FlakyProvider(KeywordProvider):
        def embed(self, texts, *, profile, task):
            if "empty" in texts[0]:
                return [ProviderEmbedding([0.1] * 768)]  # the wrong dimension: rejected, never coerced
            return super().embed(texts, profile=profile, task=task)

    run_id = run_vector_evaluation(conn, dataset_id, chunks, DEFAULT_EMBEDDING_PROFILE, CONFIG,
                                   {version: analysis_id}, FlakyProvider())
    assert conn.fetch_scalar("SELECT status FROM evaluation.run WHERE id = %s", str(run_id)) == "DEGRADED"
    failed = conn.fetch_one("SELECT r.failure_detail FROM evaluation.question_result r JOIN evaluation.question q "
                            "ON q.id = r.question_id WHERE r.evaluation_run_id = %s AND r.status = 'FAILED'",
                            str(run_id))
    assert failed[0]["error"] == "EmbeddingDimensionError"
    assert conn.fetch_scalar("SELECT metric_value FROM evaluation.metric WHERE evaluation_run_id = %s "
                             "AND metric_name = 'failure_rate'", str(run_id)) == 0.5


# --- SS-DB-301 acceptance: reproduction, material inputs, determinism, search, deletion ----------


def completed_run(conn, indexed):
    analysis_id, version, chunks = indexed
    dataset_id, _ = build_dataset(conn, version)
    freeze_dataset(conn, dataset_id)
    run_id = run_vector_evaluation(conn, dataset_id, chunks, DEFAULT_EMBEDDING_PROFILE, CONFIG,
                                   {version: analysis_id}, KeywordProvider())
    return dataset_id, run_id


def stored_metrics(conn, run_id):
    return dict(conn.fetch_all("SELECT metric_name, metric_value FROM evaluation.metric "
                               "WHERE evaluation_run_id = %s AND question_id IS NULL", str(run_id)))


def test_stored_rankings_reproduce_the_aggregate_metrics(conn, indexed):
    from backend.services.semantic.evaluation_runs import recompute_metrics

    _, run_id = completed_run(conn, indexed)
    stored = stored_metrics(conn, run_id)
    assert recompute_metrics(conn, run_id) == pytest.approx(stored)
    assert {"recall@5", "recall@10", "mrr", "ndcg@10"} <= set(stored)


def test_a_run_missing_a_material_version_is_rejected(conn, indexed):
    _, version, chunks = indexed
    dataset_id, _ = build_dataset(conn, version)
    fingerprint = freeze_dataset(conn, dataset_id)
    columns = {
        "dataset_id": str(dataset_id), "dataset_fingerprint": fingerprint,
        "chunk_profile_id": str(register_chunk_profile(conn, chunks)),
        "chunk_profile_fingerprint": chunks.fingerprint,
        "embedding_profile_id": str(register_embedding_profile(conn, DEFAULT_EMBEDDING_PROFILE)),
        "embedding_profile_fingerprint": DEFAULT_EMBEDDING_PROFILE.fingerprint, "retrieval_mode": "VECTOR_ONLY",
        "retriever_profile": "vector-cosine", "retriever_version": "1", "query_transformation_version": "none/1",
        "evaluation_code_version": "eval/1", "git_commit": "a" * 40, "metric_configuration": "{}",
    }

    def rejected(**changes):
        values = {name: value for name, value in {**columns, **changes}.items() if value is not None}
        names = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        raises(conn, f"INSERT INTO evaluation.run ({names}) VALUES ({placeholders})", *values.values())

    for missing in ("retriever_version", "query_transformation_version", "evaluation_code_version", "git_commit",
                    "metric_configuration", "chunk_profile_fingerprint", "embedding_profile_fingerprint",
                    "dataset_fingerprint"):
        rejected(**{missing: None})  # the column is omitted
    rejected(git_commit=" ")
    rejected(chunk_profile_fingerprint="0" * 64)  # not the profile's fingerprint
    rejected(embedding_profile_fingerprint="0" * 64)
    rejected(retrieval_mode="GRAPH_EXPANDED")  # a graph mode must name its expansion profile and version


def test_a_run_against_a_draft_dataset_is_rejected(conn, indexed):
    import psycopg

    analysis_id, version, chunks = indexed
    draft, _ = build_dataset(conn, version)
    conn.execute("SAVEPOINT draft_run")
    with pytest.raises(psycopg.IntegrityError):
        start_run(conn, draft, chunks, DEFAULT_EMBEDDING_PROFILE, CONFIG, {version: analysis_id})
    conn.execute("ROLLBACK TO SAVEPOINT draft_run")
    # Even a guessed fingerprint cannot reference a draft: the key requires FROZEN.
    raises(conn, "INSERT INTO evaluation.run (dataset_id, dataset_fingerprint, chunk_profile_id, "
                 "chunk_profile_fingerprint, embedding_profile_id, embedding_profile_fingerprint, retrieval_mode, "
                 "retriever_profile, retriever_version, query_transformation_version, evaluation_code_version, "
                 "git_commit, metric_configuration) VALUES (%s, %s, %s, %s, %s, %s, 'VECTOR_ONLY', 'v', '1', 'n', "
                 "'e', 'g', '{}')",
           str(draft), "0" * 64, str(register_chunk_profile(conn, chunks)), chunks.fingerprint,
           str(register_embedding_profile(conn, DEFAULT_EMBEDDING_PROFILE)), DEFAULT_EMBEDDING_PROFILE.fingerprint)


def test_the_fingerprint_does_not_depend_on_insertion_order(conn, indexed):
    _, version, _ = indexed
    first, _ = build_dataset(conn, version, key="order-a")
    second = create_dataset(conn, "order-b", 1, "questions about the cart", "stacksniffer-bench/1")
    add_repository(conn, second, version)
    empty = add_question(conn, second, version, "q-empty", "empty when self has no items", "code_search", "SYMBOL")
    add_judgment(conn, empty, JudgmentTarget(entity_stable_key="method:app/cart.py::Cart.empty"), 3, "HUMAN",
                 "reviewer-a", "rubric/1")
    total = add_question(conn, second, version, "q-total", "total of the prices sum with tax_rate", "code_search",
                         "SYMBOL")
    add_judgment(conn, total, JudgmentTarget(file_path="app/cart.py", start_line=8, end_line=9), 1, "HUMAN",
                 "reviewer-a", "rubric/1", notes="the tax rate helper")
    add_judgment(conn, total, JudgmentTarget(entity_stable_key="method:app/cart.py::Cart.total"), 3, "HUMAN",
                 "reviewer-a", "rubric/1")
    manifests = [conn.fetch_scalar("SELECT evaluation.dataset_manifest(%s) - 'dataset_key'", str(d))
                 for d in (first, second)]
    assert canonical_json(manifests[0]) == canonical_json(manifests[1])
    fingerprint = freeze_dataset(conn, first)
    recomputed = canonical_json(conn.fetch_scalar("SELECT evaluation.dataset_manifest(%s)", str(first)))
    assert hashlib.sha256(recomputed.encode()).hexdigest() == fingerprint


def test_exact_and_approximate_search_return_the_same_neighbours(conn, indexed):
    analysis_id, _, chunks = indexed
    chunk_profile = register_chunk_profile(conn, chunks)
    embedding_profile = register_embedding_profile(conn, DEFAULT_EMBEDDING_PROFILE)
    query = KeywordProvider().embed(["total of the prices"], profile=DEFAULT_EMBEDDING_PROFILE, task="q")[0].values
    exact = vector_search(conn, analysis_id, chunk_profile, embedding_profile, "COSINE", query, 3)
    conn.fetch_scalar("SELECT semantic.create_profile_hnsw_index(%s, %s)", DEFAULT_EMBEDDING_PROFILE.profile_key,
                      DEFAULT_EMBEDDING_PROFILE.profile_version)
    conn.execute("SET LOCAL enable_seqscan = off")
    conn.execute("SET LOCAL enable_sort = off")
    approximate = vector_search(conn, analysis_id, chunk_profile, embedding_profile, "COSINE", query, 3)
    assert exact and [c.stable_chunk_key for c in approximate] == [c.stable_chunk_key for c in exact]
    assert exact[0].stable_chunk_key == "method:app/cart.py::Cart.total#0"


def test_deleting_a_ranked_chunk_keeps_the_audited_ranking(conn, indexed):
    from backend.services.semantic.evaluation_runs import recompute_metrics

    analysis_id, _, _ = indexed
    _, run_id = completed_run(conn, indexed)
    before = stored_metrics(conn, run_id)
    conn.execute("DELETE FROM semantic.chunk WHERE analysis_id = %s", str(analysis_id))
    rows = conn.fetch_all("SELECT chunk_id, stable_chunk_key FROM evaluation.ranked_result "
                          "WHERE evaluation_run_id = %s", str(run_id))
    assert rows and all(chunk_id is None and key for chunk_id, key in rows)
    assert recompute_metrics(conn, run_id) == pytest.approx(before)


def test_an_analysis_used_by_an_evaluation_run_cannot_be_deleted(conn, indexed):
    analysis_id, _, _ = indexed
    completed_run(conn, indexed)
    raises(conn, "DELETE FROM core.analysis WHERE id = %s", str(analysis_id))
