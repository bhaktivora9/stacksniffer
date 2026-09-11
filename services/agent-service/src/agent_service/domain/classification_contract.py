"""Agent-owned classification contract identity and provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

_TAXONOMY_VERSION_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_CONTRACT_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _required(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _taxonomy_version(value: str) -> str:
    if not isinstance(value, str) or not _TAXONOMY_VERSION_RE.fullmatch(value):
        raise ValueError("taxonomy_version must use vMAJOR.MINOR.PATCH")
    return value


def derive_classification_contract_id(
    classification_pipeline_version: str,
    classification_model_profile: str,
    prompt_version: str,
    taxonomy_version: str,
) -> str:
    values = {
        "classification_model_profile": _required("classification_model_profile", classification_model_profile),
        "classification_pipeline_version": _required("classification_pipeline_version", classification_pipeline_version),
        "prompt_version": _required("prompt_version", prompt_version),
        "taxonomy_version": _taxonomy_version(taxonomy_version),
    }
    payload = {"namespace": "classification_contract", **values}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ClassificationContract:
    classification_contract_id: str
    classification_pipeline_version: str
    classification_model_profile: str
    prompt_version: str
    taxonomy_version: str
    created_at: str

    def __post_init__(self) -> None:
        if not _CONTRACT_ID_RE.fullmatch(self.classification_contract_id):
            raise ValueError("classification_contract_id must be an opaque SHA-256 identity")
        _required("classification_pipeline_version", self.classification_pipeline_version)
        _required("classification_model_profile", self.classification_model_profile)
        _required("prompt_version", self.prompt_version)
        _taxonomy_version(self.taxonomy_version)
        parsed = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("created_at must include a timezone")

    @classmethod
    def create(cls, classification_pipeline_version: str, classification_model_profile: str,
               prompt_version: str, taxonomy_version: str,
               created_at: datetime | None = None) -> "ClassificationContract":
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return cls(
            classification_contract_id=derive_classification_contract_id(
                classification_pipeline_version, classification_model_profile,
                prompt_version, taxonomy_version),
            classification_pipeline_version=classification_pipeline_version,
            classification_model_profile=classification_model_profile,
            prompt_version=prompt_version,
            taxonomy_version=taxonomy_version,
            created_at=timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "classification_contract_id": self.classification_contract_id,
            "classification_pipeline_version": self.classification_pipeline_version,
            "classification_model_profile": self.classification_model_profile,
            "prompt_version": self.prompt_version,
            "taxonomy_version": self.taxonomy_version,
            "created_at": self.created_at,
        }

    def supersedes(self, previous: "ClassificationContract") -> bool:
        return self.classification_contract_id != previous.classification_contract_id
