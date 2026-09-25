"""Versioned benchmark repository manifest (repositories.yaml).

Structural validation only: nothing here talks to GitHub. Checking that each
frozen commit exists remotely is a separate step (see manifest_verification).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

try:
    from services.repository_resolver import PROVIDER, RepositoryResolutionError, parse_github_repository_url
except ModuleNotFoundError:
    from backend.services.repository_resolver import PROVIDER, RepositoryResolutionError, parse_github_repository_url

SUPPORTED_SCHEMA_VERSIONS = {1}

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]+$")
_LANGUAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+#._-]*$")
# SPDX short identifiers use letters, digits, '.', '-' and '+' (e.g. MIT, Apache-2.0, GPL-3.0-or-later).
_SPDX_ID_PATTERN = re.compile(r"^(?:LicenseRef-)?[A-Za-z0-9][A-Za-z0-9.+-]*$")


class EvaluationPurpose(str, Enum):
    SMOKE = "smoke"
    DEVELOPMENT = "development"
    CONTRASTIVE_TRAIN = "contrastive_train"
    RETRIEVAL_VALIDATION = "retrieval_validation"
    RETRIEVAL_TEST = "retrieval_test"
    DEMO = "demo"


# evaluation.dataset_repository.dataset_role; smoke/development repos are not dataset members.
DATASET_ROLES = {
    EvaluationPurpose.CONTRASTIVE_TRAIN: "TRAIN",
    EvaluationPurpose.RETRIEVAL_VALIDATION: "VALIDATION",
    EvaluationPurpose.RETRIEVAL_TEST: "TEST",
    EvaluationPurpose.DEMO: "DEMO",
}


class ManifestValidationError(ValueError):
    """A manifest failed structural validation; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str], source: str | None = None):
        self.errors = errors
        self.source = source
        where = f" {source}" if source else ""
        super().__init__(f"benchmark manifest{where} is invalid:\n" + "\n".join(f"  - {e}" for e in errors))


class ManifestRepository(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: StrictStr
    url: StrictStr
    frozen_commit: StrictStr
    language: StrictStr
    license: StrictStr
    evaluation_purpose: EvaluationPurpose

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase slug (letters, digits, '.', '_', '-'), at most 100 characters")
        return value

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        try:
            owner, repository = parse_github_repository_url(value)
        except RepositoryResolutionError as exc:
            raise ValueError(str(exc)) from exc
        return f"https://github.com/{owner}/{repository}"

    @field_validator("frozen_commit")
    @classmethod
    def _check_frozen_commit(cls, value: str) -> str:
        if _SHA_PATTERN.fullmatch(value):
            return value
        if len(value) == 40 and _HEX_PATTERN.fullmatch(value):
            raise ValueError("commit SHA must be lowercase hexadecimal")
        if _HEX_PATTERN.fullmatch(value):
            raise ValueError(
                f"'{value}' is {len(value)} hex characters; use the full 40-character commit SHA "
                "(git rev-parse <ref>)"
            )
        raise ValueError(
            f"'{value}' looks like a branch or tag name; frozen_commit must be a full 40-character commit SHA"
        )

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _LANGUAGE_PATTERN.fullmatch(normalized):
            raise ValueError("must be a language name such as 'python' or 'java'")
        return normalized

    @field_validator("license")
    @classmethod
    def _check_license(cls, value: str) -> str:
        if not _SPDX_ID_PATTERN.fullmatch(value):
            raise ValueError("must be a single SPDX license identifier such as 'MIT' or 'Apache-2.0'")
        return value

    @property
    def canonical_repository_key(self) -> str:
        return f"{PROVIDER}:{self.url.removeprefix('https://github.com/')}"

    @property
    def dataset_role(self) -> str | None:
        return DATASET_ROLES.get(self.evaluation_purpose)


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: StrictInt
    dataset_version: StrictStr
    repositories: Annotated[list[ManifestRepository], Field(min_length=1)]

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version {value}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}")
        return value

    @field_validator("dataset_version")
    @classmethod
    def _check_dataset_version(cls, value: str) -> str:
        if not _VERSION_PATTERN.fullmatch(value):
            raise ValueError("must be a version label such as 'r1.0.0'")
        return value

    @model_validator(mode="after")
    def _check_cross_entry_rules(self) -> BenchmarkManifest:
        errors: list[str] = []
        first_by_id: dict[str, int] = {}
        first_by_identity: dict[tuple[str, str], int] = {}
        for index, repo in enumerate(self.repositories):
            if repo.id in first_by_id:
                errors.append(
                    f"repositories[{index}]: duplicate id '{repo.id}' (first used at repositories[{first_by_id[repo.id]}])"
                )
            else:
                first_by_id[repo.id] = index

            identity = (repo.canonical_repository_key, repo.frozen_commit)
            if identity in first_by_identity:
                other = self.repositories[first_by_identity[identity]]
                errors.append(
                    f"repositories[{index}] '{repo.id}': {repo.canonical_repository_key}@{repo.frozen_commit[:12]} "
                    f"is already listed as '{other.id}'; list each repository commit once"
                )
            else:
                first_by_identity[identity] = index

        # Split by repository, not by commit: another commit of a test repository still leaks it into training.
        train = {r.canonical_repository_key: r.id for r in self.repositories
                 if r.evaluation_purpose is EvaluationPurpose.CONTRASTIVE_TRAIN}
        for repo in self.repositories:
            if repo.evaluation_purpose is EvaluationPurpose.RETRIEVAL_TEST and repo.canonical_repository_key in train:
                errors.append(
                    f"'{repo.id}' (retrieval_test) and '{train[repo.canonical_repository_key]}' (contrastive_train) "
                    f"use the same repository {repo.canonical_repository_key}; test repositories must not be trained on"
                )

        if errors:
            raise ValueError("\n".join(errors))
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Normalized content with repositories ordered by id, independent of YAML formatting."""
        payload = self.model_dump(mode="json")
        payload["repositories"] = sorted(payload["repositories"], key=lambda repo: repo["id"])
        return payload

    @property
    def fingerprint(self) -> str:
        """sha256 of the canonical JSON form; stable across key order, comments and entry order."""
        serialized = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last one."""


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key '{key}'", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeySafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _format_validation_error(exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors():
        location = ""
        for part in error["loc"]:
            location += f"[{part}]" if isinstance(part, int) else (f".{part}" if location else str(part))
        message = error["msg"].removeprefix("Value error, ")
        if error["type"] == "string_type" and isinstance(error.get("input"), (int, float)):
            message += " (quote the value in YAML so it is not read as a number)"
        messages.extend(f"{location}: {line}" if location else line for line in message.split("\n"))
    return messages


def parse_manifest(data: Mapping[str, Any], *, source: str | None = None) -> BenchmarkManifest:
    """Validate already-parsed manifest data."""
    if not isinstance(data, Mapping):
        raise ManifestValidationError(["top level must be a mapping with schema_version, dataset_version and repositories"], source)
    try:
        return BenchmarkManifest.model_validate(dict(data))
    except ValidationError as exc:
        raise ManifestValidationError(_format_validation_error(exc), source) from exc


def parse_manifest_yaml(text: str, *, source: str | None = None) -> BenchmarkManifest:
    """Parse and validate manifest YAML text."""
    try:
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ManifestValidationError([f"invalid YAML: {exc}"], source) from exc
    return parse_manifest(data, source=source)


def load_manifest(path: str | Path) -> BenchmarkManifest:
    """Read and validate a repositories.yaml file."""
    manifest_path = Path(path)
    return parse_manifest_yaml(manifest_path.read_text(encoding="utf-8"), source=str(manifest_path))
