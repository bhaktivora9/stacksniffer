"""Canonical StackSniffer identifier bindings."""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import re
from typing import Any, ClassVar
from uuid import UUID, uuid4

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REPOSITORY_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}/[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}$")
_TAXONOMY_VERSION_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

_ID_FIELDS = (
    "repository_version_id", "analysis_id", "analysis_job_id",
    "classification_run_id", "classification_result_id", "agent_run_id",
    "event_id", "request_id", "correlation_id",
)
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_HASH_PREFIX = "sha256:"


def _validate_uuid(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical lowercase UUID")
    try:
        UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc
    return value


def validate_repository_key(value: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY_KEY_RE.fullmatch(value):
        raise ValueError("repository_key must be provider/owner/repository in lowercase")
    return value


def validate_taxonomy_version(value: str) -> str:
    if not isinstance(value, str) or not _TAXONOMY_VERSION_RE.fullmatch(value):
        raise ValueError("taxonomy_version must use vMAJOR.MINOR.PATCH")
    return value


def _validate_text(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_commit_sha(value: str) -> str:
    if not isinstance(value, str) or not _COMMIT_SHA_RE.fullmatch(value):
        raise ValueError("commit_sha must be 7-64 lowercase hexadecimal characters")
    return value


def _derive(namespace: str, values: dict[str, str]) -> str:
    payload = {"namespace": namespace, **values}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


def deterministic_analysis_key(repository_key: str, commit_sha: str,
                               deterministic_pipeline_version: str) -> str:
    validate_repository_key(repository_key)
    _validate_commit_sha(commit_sha)
    _validate_text("deterministic_pipeline_version", deterministic_pipeline_version)
    return _derive("deterministic_analysis", {
        "commit_sha": commit_sha,
        "deterministic_pipeline_version": deterministic_pipeline_version,
        "repository_key": repository_key,
    })


def classification_contract_id(classification_pipeline_version: str,
                               classification_model_profile: str,
                               prompt_version: str,
                               taxonomy_version: str) -> str:
    _validate_text("classification_pipeline_version", classification_pipeline_version)
    _validate_text("classification_model_profile", classification_model_profile)
    _validate_text("prompt_version", prompt_version)
    validate_taxonomy_version(taxonomy_version)
    return _derive("classification_contract", {
        "classification_model_profile": classification_model_profile,
        "classification_pipeline_version": classification_pipeline_version,
        "prompt_version": prompt_version,
        "taxonomy_version": taxonomy_version,
    })


def classification_key(analysis_id: str, contract_id: str) -> str:
    _validate_uuid("analysis_id", analysis_id)
    _validate_text("classification_contract_id", contract_id)
    return _derive("classification", {
        "analysis_id": analysis_id,
        "classification_contract_id": contract_id,
    })


def assembled_analysis_key(deterministic_key: str, classification_key_value: str) -> str:
    _validate_text("deterministic_analysis_key", deterministic_key)
    _validate_text("classification_key", classification_key_value)
    return _derive("assembled_analysis", {
        "classification_key": classification_key_value,
        "deterministic_analysis_key": deterministic_key,
    })


def agent_review_spec_key(repository_key: str, commit_sha: str, review_goal: str,
                          agent_version: str, prompt_version: str,
                          model_profile: str) -> str:
    validate_repository_key(repository_key)
    _validate_commit_sha(commit_sha)
    for field_name, value in (("review_goal", review_goal), ("agent_version", agent_version),
                              ("prompt_version", prompt_version), ("model_profile", model_profile)):
        _validate_text(field_name, value)
    return _derive("agent_review_spec", {
        "agent_version": agent_version,
        "commit_sha": commit_sha,
        "model_profile": model_profile,
        "prompt_version": prompt_version,
        "repository_key": repository_key,
        "review_goal": review_goal,
    })


def agent_context_fingerprint(classification_result_id: str, retrieval_index_version: str,
                              taxonomy_version: str, graph_snapshot_version: str = "NONE") -> str:
    _validate_uuid("classification_result_id", classification_result_id)
    _validate_text("retrieval_index_version", retrieval_index_version)
    validate_taxonomy_version(taxonomy_version)
    _validate_text("graph_snapshot_version", graph_snapshot_version)
    return _derive("agent_context", {
        "classification_result_id": classification_result_id,
        "graph_snapshot_version": graph_snapshot_version,
        "retrieval_index_version": retrieval_index_version,
        "taxonomy_version": taxonomy_version,
    })


def agent_result_cache_key(review_spec_key: str, context_fingerprint: str) -> str:
    _validate_text("agent_review_spec_key", review_spec_key)
    _validate_text("agent_context_fingerprint", context_fingerprint)
    return _derive("agent_result_cache", {
        "agent_context_fingerprint": context_fingerprint,
        "agent_review_spec_key": review_spec_key,
    })


@dataclass(frozen=True)
class IdentifierBundle:
    repository_key: str
    repository_version_id: str
    analysis_id: str
    analysis_job_id: str
    classification_run_id: str
    classification_result_id: str
    agent_run_id: str
    event_id: str
    request_id: str
    correlation_id: str
    taxonomy_version: str

    def __post_init__(self) -> None:
        validate_repository_key(self.repository_key)
        for field_name in _ID_FIELDS:
            _validate_uuid(field_name, getattr(self, field_name))
        validate_taxonomy_version(self.taxonomy_version)

    @classmethod
    def new(cls, repository_key: str, taxonomy_version: str) -> "IdentifierBundle":
        values = {field_name: str(uuid4()) for field_name in _ID_FIELDS}
        return cls(repository_key=repository_key, taxonomy_version=taxonomy_version, **values)

    def to_dict(self) -> dict[str, str]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IdentifierBundle":
        expected = {field.name for field in fields(cls) if field.name != "_FIELD_NAMES"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("identifier payload must contain exactly the canonical fields")
        return cls(**payload)

    @classmethod
    def from_json(cls, payload: str) -> "IdentifierBundle":
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("identifier payload must be valid JSON") from exc
        return cls.from_dict(decoded)
