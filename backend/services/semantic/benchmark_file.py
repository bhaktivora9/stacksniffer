"""Benchmark question files: parse, check against analyzed repositories, load as a DRAFT, freeze.

A question file (``backend/evaluation/benchmark/*.yaml``) names its repositories by
``repositories.yaml`` id, so every question is tied to the manifest's frozen commit. Loading
resolves each id to the stored repository version (the commit must already be analyzed) and
checks every judgment against that analysis: the entity exists, or the file exists and the line
range lies inside it. A file that fails any check loads nothing.

Freezing is separate and stricter: every judgment must have had its human review
(``judgment_source: HUMAN``), as the labelling rubric requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..benchmark_manifest import BenchmarkManifest
from .benchmark import JudgmentTarget, add_judgment, add_question, add_repository, create_dataset, freeze_dataset

CODE_VERSION = "stacksniffer-bench/1"
TASK_CATEGORIES = ("code_search", "flow", "configuration", "architecture", "data_model")
_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class BenchmarkFileError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JudgmentSpec(_Model):
    entity: str | None = Field(default=None, min_length=1)
    file: str | None = Field(default=None, min_length=1)
    lines: tuple[int, int] | None = None
    grade: int = Field(ge=0, le=3)
    note: str | None = None
    # Overrides of the file-level defaults, set by the human second pass.
    source: Literal["HUMAN", "MODEL_ASSISTED", "DERIVED"] | None = None
    annotator: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_target(self) -> JudgmentSpec:
        if (self.entity is None) == (self.file is None):
            raise ValueError("a judgment names exactly one of 'entity' or 'file'")
        if self.file is not None and (self.lines is None or not 1 <= self.lines[0] <= self.lines[1]):
            raise ValueError("a file judgment needs 'lines: [start, end]' with 1 <= start <= end")
        if self.entity is not None and self.lines is not None:
            raise ValueError("an entity judgment takes no 'lines'")
        return self


class QuestionSpec(_Model):
    key: str
    question_class: Literal["local", "global"] = Field(alias="class")
    task_category: Literal[TASK_CATEGORIES]  # type: ignore[valid-type]
    scope: Literal["SYMBOL", "FILE", "MODULE", "REPOSITORY"]
    lexical_overlap: Literal["low", "high"] = "low"
    text: str = Field(min_length=1)
    judgments: tuple[JudgmentSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _answerable(self) -> QuestionSpec:
        if not _KEY.fullmatch(self.key):
            raise ValueError(f"question key {self.key!r} must be lowercase letters, digits, '.', '_' or '-'")
        if not any(j.grade == 3 for j in self.judgments):
            raise ValueError(f"{self.key}: every question needs a grade-3 target")
        return self


class RepositorySpec(_Model):
    manifest_id: str
    role: Literal["TRAIN", "VALIDATION", "TEST", "DEMO"]
    questions: tuple[QuestionSpec, ...] = Field(min_length=1)


class BenchmarkFile(_Model):
    dataset_key: str
    dataset_version: int = Field(ge=1)
    status: Literal["draft", "reviewed"]
    description: str = Field(min_length=1)
    rubric: str
    annotator_version: str = Field(min_length=1)
    judgment_source: Literal["HUMAN", "MODEL_ASSISTED", "DERIVED"]
    annotator: str = Field(min_length=1)
    repositories: tuple[RepositorySpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> BenchmarkFile:
        keys = [q.key for r in self.repositories for q in r.questions]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"duplicate question keys: {duplicates}")
        ids = [r.manifest_id for r in self.repositories]
        if len(ids) != len(set(ids)):
            raise ValueError("a repository is listed twice")
        return self

    def source_of(self, judgment: JudgmentSpec) -> str:
        return judgment.source or self.judgment_source

    def unreviewed(self) -> int:
        return sum(self.source_of(j) != "HUMAN" for r in self.repositories for q in r.questions for j in q.judgments)


def parse_benchmark_file(path: str | Path) -> BenchmarkFile:
    try:
        return BenchmarkFile.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except ValidationError as exc:
        raise BenchmarkFileError([f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]) from exc
    except yaml.YAMLError as exc:
        raise BenchmarkFileError([f"invalid YAML: {exc}"]) from exc


def check_roles(spec: BenchmarkFile, manifest: BenchmarkManifest) -> list[str]:
    """Each repository is in the manifest and plays the role its evaluation purpose gives it."""
    entries = {repo.id: repo for repo in manifest.repositories}
    problems = []
    for repository in spec.repositories:
        entry = entries.get(repository.manifest_id)
        if entry is None:
            problems.append(f"{repository.manifest_id}: not in the manifest")
        elif entry.dataset_role != repository.role:
            problems.append(f"{repository.manifest_id}: role {repository.role}, but the manifest's evaluation "
                            f"purpose {entry.evaluation_purpose.value} makes it {entry.dataset_role}")
    return problems


@dataclass(frozen=True)
class ResolvedRepository:
    manifest_id: str
    repository_version_id: UUID
    analysis_id: UUID


def resolve_repositories(conn: Any, spec: BenchmarkFile, manifest: BenchmarkManifest,
                         ) -> tuple[dict[str, ResolvedRepository], list[str]]:
    """The stored version of each repository's frozen commit, and its latest analysis with extracted files."""
    entries = {repo.id: repo for repo in manifest.repositories}
    resolved, problems = {}, []
    for repository in spec.repositories:
        entry = entries.get(repository.manifest_id)
        if entry is None:
            continue  # reported by check_roles
        row = conn.fetch_one(
            "SELECT rv.id, a.id FROM core.repository r JOIN core.repository_version rv ON rv.repository_id = r.id "
            "JOIN core.analysis a ON a.repository_version_id = rv.id "
            "WHERE r.canonical_key = %s AND rv.commit_sha = %s "
            "AND EXISTS (SELECT 1 FROM core.source_file f WHERE f.analysis_id = a.id) "
            "ORDER BY a.created_at DESC LIMIT 1", entry.canonical_repository_key, entry.frozen_commit)
        if row is None:
            problems.append(f"{repository.manifest_id}: {entry.canonical_repository_key}@{entry.frozen_commit[:12]} "
                            f"has no extracted analysis; analyze it first")
            continue
        resolved[repository.manifest_id] = ResolvedRepository(repository.manifest_id, row[0], row[1])
    return resolved, problems


def check_targets(conn: Any, spec: BenchmarkFile, resolved: dict[str, ResolvedRepository]) -> list[str]:
    """Every judgment names an entity of the analysis, or a line range inside one of its files."""
    problems = []
    for repository in spec.repositories:
        target = resolved.get(repository.manifest_id)
        if target is None:
            continue
        analysis = str(target.analysis_id)
        entities = {row[0] for row in conn.fetch_all(
            "SELECT stable_key FROM core.entity WHERE analysis_id = %s", analysis)}
        files = {path: (content, size) for path, content, size in conn.fetch_all(
            "SELECT f.path, sc.content, f.size_bytes FROM core.source_file f "
            "LEFT JOIN core.source_content sc ON sc.content_hash = f.content_hash WHERE f.analysis_id = %s", analysis)}
        for question in repository.questions:
            for judgment in question.judgments:
                where = f"{repository.manifest_id}/{question.key}"
                if judgment.entity is not None:
                    if judgment.entity not in entities:
                        problems.append(f"{where}: no entity {judgment.entity}")
                    continue
                if judgment.file not in files:
                    problems.append(f"{where}: no file {judgment.file}")
                    continue
                content = files[judgment.file][0]
                if content is None:
                    problems.append(f"{where}: {judgment.file} has no retained source to check lines against")
                    continue
                total = len(bytes(content).decode("utf-8", errors="replace").splitlines())
                if judgment.lines[1] > total:
                    problems.append(f"{where}: {judgment.file} lines {list(judgment.lines)} exceed its {total} lines")
    return problems


def load_benchmark(conn: Any, spec: BenchmarkFile, manifest: BenchmarkManifest) -> UUID:
    """Check everything, then create the DRAFT dataset with its repositories, questions and judgments.

    The caller owns the transaction; on any problem nothing is written.
    """
    problems = check_roles(spec, manifest)
    resolved, unresolved = resolve_repositories(conn, spec, manifest)
    problems += unresolved
    problems += check_targets(conn, spec, resolved)
    if problems:
        raise BenchmarkFileError(problems)

    dataset_id = create_dataset(conn, spec.dataset_key, spec.dataset_version, spec.description, CODE_VERSION)
    for repository in spec.repositories:
        version_id = resolved[repository.manifest_id].repository_version_id
        add_repository(conn, dataset_id, version_id, repository.role)
        for question in repository.questions:
            question_id = add_question(
                conn, dataset_id, version_id, question.key, question.text, question.task_category, question.scope,
                {"question_class": question.question_class, "lexical_overlap": question.lexical_overlap,
                 "manifest_id": repository.manifest_id})
            for judgment in question.judgments:
                target = (JudgmentTarget(entity_stable_key=judgment.entity) if judgment.entity is not None else
                          JudgmentTarget(file_path=judgment.file, start_line=judgment.lines[0],
                                         end_line=judgment.lines[1]))
                add_judgment(conn, question_id, target, judgment.grade, spec.source_of(judgment),
                             judgment.annotator or spec.annotator, spec.annotator_version, judgment.note)
    return dataset_id


def freeze_reviewed(conn: Any, spec: BenchmarkFile, dataset_id: UUID) -> str:
    """Freeze a loaded dataset, only when every judgment has had its human review."""
    pending = spec.unreviewed()
    if pending or spec.status != "reviewed":
        raise BenchmarkFileError([f"{pending} judgments are not HUMAN-reviewed and the file is '{spec.status}'; "
                                  f"complete the second pass (rubric) and set status: reviewed before freezing"])
    return freeze_dataset(conn, dataset_id)
