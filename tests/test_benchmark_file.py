"""Benchmark question files: structural rules (no database) and loading against the canonical schema."""

import os
from pathlib import Path

import pytest
import yaml

from backend.services.benchmark_manifest import load_manifest, parse_manifest
from backend.services.semantic.benchmark_file import (
    BenchmarkFileError,
    check_roles,
    freeze_reviewed,
    load_benchmark,
    parse_benchmark_file,
)

ROOT = Path(__file__).resolve().parents[1]
DRAFTS = sorted((ROOT / "backend" / "evaluation" / "benchmark").glob("*.yaml"))


def spec(**overrides):
    question = {"key": "q-total", "class": "local", "task_category": "code_search", "scope": "SYMBOL",
                "text": "How is the cart total computed?",
                "judgments": [{"entity": "method:app/cart.py::Cart.total", "grade": 3},
                              {"file": "app/cart.py", "lines": [8, 9], "grade": 1, "note": "tax helper"}]}
    question.update(overrides.pop("question", {}))
    data = {"dataset_key": "cart-bench", "dataset_version": 1, "status": "draft", "description": "cart questions",
            "rubric": "docs/evaluation/retrieval-benchmark-rubric.md", "annotator_version": "rubric/1",
            "judgment_source": "MODEL_ASSISTED", "annotator": "drafter",
            "repositories": [{"manifest_id": "cart", "role": "TEST", "questions": [question]}]}
    data.update(overrides)
    return data


def parse(tmp_path, data):
    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
    return parse_benchmark_file(path)


@pytest.mark.parametrize("path", DRAFTS, ids=lambda p: p.name)
def test_checked_in_question_files_are_valid_and_match_the_manifest(path):
    parsed = parse_benchmark_file(path)
    assert check_roles(parsed, load_manifest(ROOT / "repositories.yaml")) == []


def test_a_valid_file_counts_what_still_needs_human_review(tmp_path):
    parsed = parse(tmp_path, spec())
    assert parsed.unreviewed() == 2
    reviewed = spec(question={"judgments": [{"entity": "method:app/cart.py::Cart.total", "grade": 3,
                                              "source": "HUMAN", "annotator": "reviewer-a"}]})
    assert parse(tmp_path, reviewed).unreviewed() == 0


@pytest.mark.parametrize("question, message", [
    ({"judgments": [{"entity": "method:x", "grade": 2}]}, "grade-3 target"),
    ({"judgments": [{"entity": "method:x", "file": "a.py", "lines": [1, 2], "grade": 3}]}, "exactly one"),
    ({"judgments": [{"file": "a.py", "grade": 3}]}, "lines"),
    ({"judgments": [{"file": "a.py", "lines": [5, 2], "grade": 3}]}, "lines"),
    ({"judgments": [{"entity": "method:x", "grade": 4}]}, "less than or equal to 3"),
    ({"task_category": "trivia"}, "task_category"),
    ({"key": "Q 1"}, "question key"),
])
def test_malformed_questions_are_rejected(tmp_path, question, message):
    with pytest.raises(BenchmarkFileError, match=message):
        parse(tmp_path, spec(question=question))


def test_duplicate_question_keys_are_rejected(tmp_path):
    data = spec()
    data["repositories"][0]["questions"] *= 2
    with pytest.raises(BenchmarkFileError, match="duplicate question keys"):
        parse(tmp_path, data)


def test_roles_must_follow_the_manifest(tmp_path):
    manifest = parse_manifest({"schema_version": 1, "dataset_version": "t1", "repositories": [
        {"id": "cart", "url": "https://github.com/test-owner/cart", "frozen_commit": "a" * 40, "language": "python",
         "license": "MIT", "evaluation_purpose": "retrieval_validation"}]})
    assert check_roles(parse(tmp_path, spec()), manifest) == [
        "cart: role TEST, but the manifest's evaluation purpose retrieval_validation makes it VALIDATION"]
    missing = spec(repositories=[{"manifest_id": "other", "role": "TEST", "questions": spec()["repositories"][0]["questions"]}])
    assert check_roles(parse(tmp_path, missing), manifest) == ["other: not in the manifest"]


# --- against the canonical schema ----------------------------------------------------------------

needs_db = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


@pytest.fixture
def cart(request):
    """An extracted analysis of FILES, and a manifest that freezes its commit as a test repository."""
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is not set")
    from test_semantic_evaluation_postgres import FILES

    from test_extraction_postgres import create_analysis, extract_into

    conn = request.getfixturevalue("conn")
    from backend.services.semantic.source_content import retain_source_content

    analysis_id = create_analysis(conn, sha="c" * 40)
    extract_into(conn, request.getfixturevalue("tmp_path"), analysis_id, FILES)
    retain_source_content(conn, analysis_id, FILES.__getitem__)  # line ranges are checked against it
    key = conn.fetch_scalar("SELECT r.canonical_key FROM core.analysis a JOIN core.repository_version v "
                            "ON v.id = a.repository_version_id JOIN core.repository r ON r.id = v.repository_id "
                            "WHERE a.id = %s", str(analysis_id))
    manifest = parse_manifest({"schema_version": 1, "dataset_version": "t1", "repositories": [
        {"id": "cart", "url": f"https://github.com/{key.removeprefix('github:')}", "frozen_commit": "c" * 40,
         "language": "python", "license": "MIT", "evaluation_purpose": "retrieval_test"}]})
    return conn, manifest


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


@needs_db
def test_a_checked_file_loads_as_a_draft_with_its_review_state(cart, tmp_path):
    conn, manifest = cart
    dataset_id = load_benchmark(conn, parse(tmp_path, spec()), manifest)
    assert conn.fetch_scalar("SELECT status FROM evaluation.dataset WHERE id = %s", str(dataset_id)) == "DRAFT"
    rows = conn.fetch_all("SELECT j.entity_stable_key, j.file_path, j.start_line, j.end_line, j.relevance_grade, "
                          "j.judgment_source, j.annotator, j.notes, q.metadata->>'question_class' "
                          "FROM evaluation.relevance_judgment j JOIN evaluation.question q ON q.id = j.question_id "
                          "WHERE q.dataset_id = %s ORDER BY j.relevance_grade DESC", str(dataset_id))
    assert rows == [("method:app/cart.py::Cart.total", None, None, None, 3, "MODEL_ASSISTED", "drafter", None, "local"),
                    (None, "app/cart.py", 8, 9, 1, "MODEL_ASSISTED", "drafter", "tax helper", "local")]


@needs_db
def test_unknown_targets_load_nothing(cart, tmp_path):
    conn, manifest = cart
    data = spec(question={"judgments": [{"entity": "method:app/cart.py::Cart.missing", "grade": 3},
                                        {"file": "app/cart.py", "lines": [8, 99], "grade": 1},
                                        {"file": "app/none.py", "lines": [1, 1], "grade": 1}]})
    before = conn.fetch_scalar("SELECT count(*) FROM evaluation.dataset")
    with pytest.raises(BenchmarkFileError) as caught:
        load_benchmark(conn, parse(tmp_path, data), manifest)
    assert caught.value.problems == ["cart/q-total: no entity method:app/cart.py::Cart.missing",
                                     "cart/q-total: app/cart.py lines [8, 99] exceed its 9 lines",
                                     "cart/q-total: no file app/none.py"]
    assert conn.fetch_scalar("SELECT count(*) FROM evaluation.dataset") == before


@needs_db
def test_an_unanalyzed_commit_is_reported(cart, tmp_path):
    conn, manifest = cart
    entry = manifest.repositories[0].model_dump()
    other = parse_manifest({"schema_version": 1, "dataset_version": "t1",
                            "repositories": [{**entry, "evaluation_purpose": "retrieval_test", "frozen_commit": "d" * 40}]})
    with pytest.raises(BenchmarkFileError, match="has no extracted analysis"):
        load_benchmark(conn, parse(tmp_path, spec()), other)


@needs_db
def test_only_a_human_reviewed_file_freezes(cart, tmp_path):
    conn, manifest = cart
    draft = parse(tmp_path, spec())
    dataset_id = load_benchmark(conn, draft, manifest)
    with pytest.raises(BenchmarkFileError, match="not HUMAN-reviewed"):
        freeze_reviewed(conn, draft, dataset_id)

    reviewed = parse(tmp_path, spec(dataset_version=2, status="reviewed", judgment_source="HUMAN",
                                    annotator="reviewer-a"))
    dataset_id = load_benchmark(conn, reviewed, manifest)
    fingerprint = freeze_reviewed(conn, reviewed, dataset_id)
    assert conn.fetch_one("SELECT status, fingerprint FROM evaluation.dataset WHERE id = %s",
                          str(dataset_id)) == ("FROZEN", fingerprint)
