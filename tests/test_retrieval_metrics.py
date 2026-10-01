"""Relevance of retrieved chunks to judged canonical targets, and the retrieval metrics (no database)."""

import math
from uuid import uuid4

import pytest

from backend.services.semantic.evaluation_runs import (
    QueryOutcome, RunConfiguration, aggregate, ndcg_at, reciprocal_rank, recall_at,
)
from backend.services.semantic.retrieval import Judgment, RetrievedChunk, matching_judgments

CONFIG = RunConfiguration("vector", "v1", "none/1", "eval/1", "0" * 40)


def chunk(key, path="app/cart.py", lines=(1, 2), entity=None, score=0.5):
    return RetrievedChunk(uuid4(), key, "h", path, lines[0], lines[1], entity, score, score)


def judgment(grade, entity=None, path=None, lines=(None, None)):
    return Judgment(uuid4(), grade, entity, path, lines[0], lines[1])


def test_a_chunk_matches_its_entity_or_an_overlapping_file_range():
    by_entity = judgment(3, entity="method:app/cart.py::Cart.total")
    by_range = judgment(2, path="app/cart.py", lines=(10, 12))
    elsewhere = judgment(1, path="app/other.py", lines=(10, 12))
    judged = [by_entity, by_range, elsewhere]
    assert matching_judgments(chunk("k", entity="method:app/cart.py::Cart.total"), judged) == [by_entity]
    assert matching_judgments(chunk("k", lines=(12, 20)), judged) == [by_range]
    assert matching_judgments(chunk("k", lines=(13, 20)), judged) == []


def outcome():
    main = judgment(3, entity="method:app/cart.py::Cart.total")
    helper = judgment(1, path="app/cart.py", lines=(30, 31))
    ignored = judgment(0, entity="function:app/cart.py::unrelated")
    ranked = [
        chunk("noise#0", lines=(50, 60)),
        chunk("total#0", entity="method:app/cart.py::Cart.total"),
        chunk("total#1", entity="method:app/cart.py::Cart.total"),  # the same target again earns nothing
        chunk("helper#0", lines=(29, 33)),
        chunk("unrelated#0", entity="function:app/cart.py::unrelated"),  # grade 0 is not relevant
    ]
    result = QueryOutcome(ranked, [main, helper, ignored], latency_ms=10.0)
    result.score()
    return result


def test_recall_mrr_and_ndcg_credit_each_target_once():
    result = outcome()
    assert (recall_at(result, 1), recall_at(result, 2), recall_at(result, 4)) == (0.0, 0.5, 1.0)
    assert reciprocal_rank(result) == 0.5
    dcg = 7 / math.log2(3) + 1 / math.log2(5)
    idcg = 7 / math.log2(2) + 1 / math.log2(3)
    assert ndcg_at(result, 10) == pytest.approx(dcg / idcg)
    assert [best.relevance_grade if best else None for _, best, _ in result.credited] == [None, 3, None, 1, None]


def test_questions_without_relevant_judgments_do_not_count():
    empty = QueryOutcome([chunk("x#0")], [judgment(0, entity="e")])
    empty.score()
    assert recall_at(empty, 5) is None and reciprocal_rank(empty) is None and ndcg_at(empty, 10) is None


def test_aggregate_reports_latency_failures_and_empty_results():
    good = outcome()
    failed = QueryOutcome([], [judgment(3, entity="e")], status="FAILED", latency_ms=30.0)
    empty = QueryOutcome([], [judgment(3, entity="e")], latency_ms=20.0)
    empty.score()
    metrics = aggregate([good, failed, empty], CONFIG)
    assert metrics["recall@5"] == pytest.approx(0.5)  # the failed question is not scored
    assert metrics["mrr"] == pytest.approx(0.25)
    assert metrics["failure_rate"] == pytest.approx(1 / 3)
    assert metrics["empty_result_rate"] == pytest.approx(1 / 3)
    assert (metrics["latency_ms_p50"], metrics["latency_ms_mean"]) == (20.0, 20.0)
    assert metrics["latency_ms_p95"] == pytest.approx(29.0)
    assert {"recall@10", "ndcg@10", "questions"} <= set(metrics)
