"""Benchmark datasets: assemble a DRAFT, then freeze it into an immutable, fingerprinted version.

Freezing reads the manifest PostgreSQL builds from the dataset's rows
(evaluation.dataset_manifest), serializes it canonically and stores it with its SHA-256; the
database refuses the freeze unless the stored manifest is exactly the rows' manifest. After that
the dataset and its repositories, questions and judgments reject every change: a correction is a
new dataset version with a new fingerprint.

Judgments name canonical source facts (an entity stable key and/or a file range), never a chunk,
so changing the chunking strategy does not change the benchmark.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .profiles import canonical_json

SCOPES = ("SYMBOL", "FILE", "MODULE", "REPOSITORY")


@dataclass(frozen=True)
class JudgmentTarget:
    entity_stable_key: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    content_hash: str | None = None

    def __post_init__(self):
        if not self.entity_stable_key and not (self.file_path and self.start_line):
            raise ValueError("a judgment targets an entity stable key or a file range")


def create_dataset(conn: Any, dataset_key: str, dataset_version: int, description: str, code_version: str) -> UUID:
    return conn.fetch_scalar(
        "INSERT INTO evaluation.dataset (dataset_key, dataset_version, description, code_version) "
        "VALUES (%s, %s, %s, %s) RETURNING id", dataset_key, dataset_version, description, code_version)


def add_repository(conn: Any, dataset_id: UUID, repository_version_id: UUID, role: str = "TEST") -> None:
    conn.execute("INSERT INTO evaluation.dataset_repository (dataset_id, repository_version_id, dataset_role) "
                 "VALUES (%s, %s, %s)", str(dataset_id), str(repository_version_id), role)


def add_question(conn: Any, dataset_id: UUID, repository_version_id: UUID, question_key: str, question_text: str,
                 task_category: str, expected_answer_scope: str, metadata: dict | None = None) -> UUID:
    return conn.fetch_scalar(
        "INSERT INTO evaluation.question (dataset_id, repository_version_id, question_key, question_text, "
        "task_category, expected_answer_scope, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
        str(dataset_id), str(repository_version_id), question_key, question_text, task_category,
        expected_answer_scope, json.dumps(metadata or {}))


def add_judgment(conn: Any, question_id: UUID, target: JudgmentTarget, relevance_grade: int, judgment_source: str,
                 annotator: str, annotator_version: str, notes: str | None = None) -> UUID:
    return conn.fetch_scalar(
        "INSERT INTO evaluation.relevance_judgment (question_id, repository_version_id, entity_stable_key, file_path, "
        "start_line, end_line, content_hash, relevance_grade, judgment_source, annotator, annotator_version, notes) "
        "SELECT q.id, q.repository_version_id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
        "FROM evaluation.question q WHERE q.id = %s RETURNING id",
        target.entity_stable_key, target.file_path, target.start_line, target.end_line, target.content_hash,
        relevance_grade, judgment_source, annotator, annotator_version, notes, str(question_id))


def freeze_dataset(conn: Any, dataset_id: UUID) -> str:
    """Freeze a DRAFT dataset; returns its fingerprint."""
    manifest = conn.fetch_scalar("SELECT evaluation.dataset_manifest(%s)", str(dataset_id))
    if manifest is None:
        raise LookupError(f"dataset {dataset_id} does not exist")
    canonical = canonical_json(manifest)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    updated = conn.execute(
        "UPDATE evaluation.dataset SET status = 'FROZEN', canonical_manifest = %s, manifest = %s::jsonb, "
        "fingerprint = %s, frozen_at = now() WHERE id = %s AND status = 'DRAFT'",
        canonical, canonical, fingerprint, str(dataset_id))
    if updated != 1:
        raise RuntimeError(f"dataset {dataset_id} is not a draft")
    return fingerprint
