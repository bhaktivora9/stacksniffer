"""Evaluation runs over frozen datasets: every material input version, per-query rankings and
aggregate metrics are persisted, so each number can be audited back to its inputs.

Relevance is decided against the judged canonical targets: a retrieved chunk is relevant when it
belongs to a judged entity or overlaps a judged file range (retrieval.matching_judgments). Each
judged target is credited once, to the highest-ranked chunk that satisfies it.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from .embeddings import EmbeddingProvider, check_compatibility, validate_vector
from .profiles import ChunkProfile, EmbeddingProfile
from .retrieval import Judgment, RetrievedChunk, matching_judgments, vector_search


@dataclass(frozen=True)
class RunConfiguration:
    retriever_profile: str
    retriever_version: str
    query_transformation_version: str
    evaluation_code_version: str
    git_commit: str
    retrieval_mode: str = "VECTOR_ONLY"
    graph_expansion_profile: str | None = None
    graph_expansion_version: str | None = None
    recall_ks: tuple[int, ...] = (5, 10)
    ndcg_k: int = 10
    first_party_only: bool = True

    @property
    def depth(self) -> int:
        return max(max(self.recall_ks), self.ndcg_k)

    def metric_configuration(self) -> dict:
        return {"recall_ks": list(self.recall_ks), "ndcg_k": self.ndcg_k, "depth": self.depth,
                "first_party_only": self.first_party_only, "gain": "2^grade-1", "target_credit": "first_match"}


# --- metrics (pure) ------------------------------------------------------------------------------


@dataclass
class QueryOutcome:
    ranked: list[RetrievedChunk]
    judgments: list[Judgment]
    status: str = "SUCCEEDED"
    latency_ms: float | None = None
    credited: list[tuple[int, Judgment | None, list[Judgment]]] = field(default_factory=list)

    def score(self) -> None:
        """For each rank: the judgment first credited there (or None) and every judgment the chunk matches."""
        seen: set[int] = set()
        self.credited = []
        for position, chunk in enumerate(self.ranked, start=1):
            matches = [j for j in matching_judgments(chunk, self.judgments) if j.relevance_grade > 0]
            new = [j for j in matches if id(j) not in seen]
            best = max(new, key=lambda j: j.relevance_grade, default=None)
            for judgment in new:
                seen.add(id(judgment))
            self.credited.append((position, best, matches))

    @property
    def relevant(self) -> list[Judgment]:
        return [j for j in self.judgments if j.relevance_grade > 0]


def recall_at(outcome: QueryOutcome, k: int) -> float | None:
    relevant = outcome.relevant
    if not relevant:
        return None
    found = {id(j) for position, _, matches in outcome.credited if position <= k for j in matches}
    return sum(1 for j in relevant if id(j) in found) / len(relevant)


def reciprocal_rank(outcome: QueryOutcome) -> float | None:
    if not outcome.relevant:
        return None
    return next((1.0 / position for position, _, matches in outcome.credited if matches), 0.0)


def ndcg_at(outcome: QueryOutcome, k: int) -> float | None:
    relevant = outcome.relevant
    if not relevant:
        return None
    dcg = sum((2 ** best.relevance_grade - 1) / math.log2(position + 1)
              for position, best, _ in outcome.credited if best is not None and position <= k)
    ideal = sorted((j.relevance_grade for j in relevant), reverse=True)[:k]
    idcg = sum((2 ** grade - 1) / math.log2(position + 1) for position, grade in enumerate(ideal, start=1))
    return dcg / idcg if idcg else None


def aggregate(outcomes: Sequence[QueryOutcome], config: RunConfiguration) -> dict[str, float]:
    def mean(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else 0.0

    scored = [o for o in outcomes if o.status != "FAILED"]
    metrics = {f"recall@{k}": mean(recall_at(o, k) for o in scored) for k in config.recall_ks}
    metrics["mrr"] = mean(reciprocal_rank(o) for o in scored)
    metrics[f"ndcg@{config.ndcg_k}"] = mean(ndcg_at(o, config.ndcg_k) for o in scored)
    latencies = sorted(o.latency_ms for o in outcomes if o.latency_ms is not None)
    if latencies:
        metrics["latency_ms_mean"] = statistics.fmean(latencies)
        metrics["latency_ms_p50"] = _percentile(latencies, 50)
        metrics["latency_ms_p95"] = _percentile(latencies, 95)
    total = len(outcomes) or 1
    metrics["failure_rate"] = sum(o.status == "FAILED" for o in outcomes) / total
    metrics["empty_result_rate"] = sum(o.status != "FAILED" and not o.ranked for o in outcomes) / total
    metrics["questions"] = float(len(outcomes))
    return metrics


def recompute_metrics(conn: Any, run_id: UUID) -> dict[str, float]:
    """Aggregate metrics rebuilt only from what the run stored: its metric configuration, per-question
    results and rankings, and the dataset's judgments. Equal to the stored metrics for any audited run."""
    configuration = conn.fetch_scalar("SELECT metric_configuration FROM evaluation.run WHERE id = %s", str(run_id))
    settings = SimpleNamespace(recall_ks=tuple(configuration["recall_ks"]), ndcg_k=configuration["ndcg_k"])
    outcomes = []
    for question_id, status, latency in conn.fetch_all(
            "SELECT r.question_id, r.status, r.latency_ms FROM evaluation.question_result r "
            "JOIN evaluation.question q ON q.id = r.question_id WHERE r.evaluation_run_id = %s ORDER BY q.question_key",
            str(run_id)):
        judgments = [Judgment(*row) for row in conn.fetch_all(
            "SELECT id, relevance_grade, entity_stable_key, file_path, start_line, end_line "
            "FROM evaluation.relevance_judgment WHERE question_id = %s ORDER BY id", str(question_id))]
        ranked = [RetrievedChunk(*row) for row in conn.fetch_all(
            "SELECT chunk_id, stable_chunk_key, chunk_content_hash, file_path, start_line, end_line, entity_stable_key, "
            "score, vector_score FROM evaluation.ranked_result WHERE evaluation_run_id = %s AND question_id = %s "
            "ORDER BY rank", str(run_id), str(question_id))]
        outcome = QueryOutcome(ranked, judgments, status=status, latency_ms=latency)
        outcome.score()
        outcomes.append(outcome)
    return aggregate(outcomes, settings)


def _percentile(sorted_values: list[float], percent: float) -> float:
    rank = (len(sorted_values) - 1) * percent / 100
    low, high = math.floor(rank), math.ceil(rank)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low)


# --- persistence and execution -------------------------------------------------------------------


def start_run(conn: Any, dataset_id: UUID, chunk_profile: ChunkProfile, embedding_profile: EmbeddingProfile,
              config: RunConfiguration, analyses: dict[UUID, UUID]) -> UUID:
    """Record a run and its inputs. ``analyses`` maps each dataset repository version to its analysis."""
    run_id = conn.fetch_scalar(
        "INSERT INTO evaluation.run (dataset_id, dataset_fingerprint, chunk_profile_id, chunk_profile_fingerprint, "
        "embedding_profile_id, embedding_profile_fingerprint, retrieval_mode, retriever_profile, retriever_version, "
        "graph_expansion_profile, graph_expansion_version, query_transformation_version, evaluation_code_version, "
        "git_commit, metric_configuration) "
        "SELECT d.id, d.fingerprint, cp.id, cp.configuration_fingerprint, ep.id, ep.configuration_fingerprint, "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb "
        "FROM evaluation.dataset d, semantic.chunk_profile cp, semantic.embedding_profile ep "
        "WHERE d.id = %s AND cp.configuration_fingerprint = %s AND ep.configuration_fingerprint = %s "
        "RETURNING id",
        config.retrieval_mode, config.retriever_profile, config.retriever_version, config.graph_expansion_profile,
        config.graph_expansion_version, config.query_transformation_version, config.evaluation_code_version,
        config.git_commit, json.dumps(config.metric_configuration()), str(dataset_id), chunk_profile.fingerprint,
        embedding_profile.fingerprint)
    if run_id is None:
        raise LookupError("the dataset or a profile is not registered")
    for repository_version_id, analysis_id in analyses.items():
        conn.execute("INSERT INTO evaluation.run_analysis (evaluation_run_id, dataset_id, repository_version_id, "
                     "analysis_id) VALUES (%s, %s, %s, %s)", str(run_id), str(dataset_id), str(repository_version_id),
                     str(analysis_id))
    return run_id


def run_vector_evaluation(conn: Any, dataset_id: UUID, chunk_profile: ChunkProfile,
                          embedding_profile: EmbeddingProfile, config: RunConfiguration,
                          analyses: dict[UUID, UUID], provider: EmbeddingProvider) -> UUID:
    """Evaluate vector retrieval on a frozen dataset and persist everything; returns the run id."""
    check_compatibility(provider, embedding_profile)
    run_id = start_run(conn, dataset_id, chunk_profile, embedding_profile, config, analyses)
    # Savepoints keep the transaction usable after a database error, so the failure is recorded.
    conn.execute("SAVEPOINT evaluation_body")
    try:
        ids = conn.fetch_one("SELECT chunk_profile_id, embedding_profile_id FROM evaluation.run WHERE id = %s",
                             str(run_id))
        outcomes = [_evaluate_question(conn, run_id, dataset_id, question, ids, embedding_profile, config, analyses,
                                       provider)
                    for question in conn.fetch_all(
                        "SELECT id, repository_version_id, question_text FROM evaluation.question "
                        "WHERE dataset_id = %s ORDER BY question_key", str(dataset_id))]
        for name, value in aggregate(outcomes, config).items():
            conn.execute("INSERT INTO evaluation.metric (evaluation_run_id, metric_name, metric_value) "
                         "VALUES (%s, %s, %s)", str(run_id), name, value)
        status = "DEGRADED" if any(o.status == "FAILED" for o in outcomes) else "SUCCEEDED"
        conn.execute("UPDATE evaluation.run SET status = %s, completed_at = now() WHERE id = %s", status, str(run_id))
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT evaluation_body")
        conn.execute("UPDATE evaluation.run SET status = 'FAILED', completed_at = now(), failure_detail = %s::jsonb "
                     "WHERE id = %s", json.dumps({"error": type(exc).__name__, "message": str(exc)[:500]}),
                     str(run_id))
        raise
    return run_id


def _evaluate_question(conn, run_id, dataset_id, question, profile_ids, embedding_profile, config, analyses,
                       provider) -> QueryOutcome:
    question_id, repository_version_id, text = question
    judgments = [Judgment(*row) for row in conn.fetch_all(
        "SELECT id, relevance_grade, entity_stable_key, file_path, start_line, end_line "
        "FROM evaluation.relevance_judgment WHERE question_id = %s ORDER BY id", str(question_id))]
    started = time.perf_counter()
    conn.execute("SAVEPOINT evaluation_question")
    try:
        # The template frames documents; a question is embedded as written, under the query task.
        query = provider.embed([text], profile=embedding_profile, task=embedding_profile.query_task)[0]
        vector = validate_vector(query.values, embedding_profile)
        ranked = vector_search(conn, analyses[repository_version_id], profile_ids[0], profile_ids[1],
                               embedding_profile.distance_metric, vector, config.depth,
                               first_party_only=config.first_party_only)
        outcome = QueryOutcome(ranked, judgments, latency_ms=(time.perf_counter() - started) * 1000)
    except Exception as exc:  # one failing question degrades the run; it does not abort it
        conn.execute("ROLLBACK TO SAVEPOINT evaluation_question")
        outcome = QueryOutcome([], judgments, status="FAILED", latency_ms=(time.perf_counter() - started) * 1000)
        conn.execute("INSERT INTO evaluation.question_result (evaluation_run_id, dataset_id, question_id, status, "
                     "latency_ms, failure_detail) VALUES (%s, %s, %s, 'FAILED', %s, %s::jsonb)",
                     str(run_id), str(dataset_id), str(question_id), outcome.latency_ms,
                     json.dumps({"error": type(exc).__name__, "message": str(exc)[:500]}))
        return outcome
    outcome.score()
    conn.execute("INSERT INTO evaluation.question_result (evaluation_run_id, dataset_id, question_id, status, "
                 "result_count, latency_ms) VALUES (%s, %s, %s, 'SUCCEEDED', %s, %s)",
                 str(run_id), str(dataset_id), str(question_id), len(ranked), outcome.latency_ms)
    for (position, best, matches), chunk in zip(outcome.credited, ranked):
        grade = max((j.relevance_grade for j in matches), default=0)
        evidence = [{"judgment_id": str(j.id), "relevance_grade": j.relevance_grade,
                     "entity_stable_key": j.entity_stable_key, "file_path": j.file_path,
                     "start_line": j.start_line, "end_line": j.end_line, "credited": j is best} for j in matches]
        conn.execute(
            "INSERT INTO evaluation.ranked_result (evaluation_run_id, question_id, rank, chunk_id, stable_chunk_key, "
            "chunk_content_hash, file_path, start_line, end_line, entity_stable_key, score, vector_score, "
            "relevance_grade, is_relevant, evidence) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            str(run_id), str(question_id), position, str(chunk.chunk_id), chunk.stable_chunk_key, chunk.content_hash,
            chunk.file_path, chunk.start_line, chunk.end_line, chunk.entity_stable_key, chunk.score,
            chunk.vector_score, grade, grade > 0, json.dumps(evidence))
    return outcome
