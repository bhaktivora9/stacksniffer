"""The canonical extraction contract shared by every language analyzer.

Every analyzer, whatever parser it uses, emits the same records: entities,
relationships and the source evidence that supports them. Records hold only
JSON-compatible primitives, so no parser object (for example a Tree-sitter
node) can reach persistence. ``validate_file_extraction`` enforces the rules
that make a file's output acceptable, including that an analyzer never emits a
fact for a capability it reports as UNSUPPORTED.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID


class CanonicalContractError(ValueError):
    """A record violates the canonical contract; the analyzer output is rejected."""


# --- cancellation ----------------------------------------------------------------------------


class ExtractionCancelled(RuntimeError):
    """Extraction stopped on request; distinct from a parser failure."""

    failure_code = "EXTRACTION_CANCELLED"


class CancellationToken:
    """Cooperative cancellation shared by acquisition and extraction for one attempt."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ExtractionCancelled(f"extraction was cancelled: {self.reason or 'unspecified'}")


@dataclass(frozen=True)
class ExtractionContext:
    """Everything the structural stage receives for one attempt.

    ``checkout_path`` and ``files`` are valid only until the stage returns and
    must never be persisted. The stage checks ``cancellation_token`` before
    each file, between parser operations and before each database batch; once
    it is set, nothing further is written and ``ExtractionCancelled`` is
    raised. The token is set when the worker loses ownership of the attempt.
    """

    analysis_id: UUID
    attempt_id: UUID
    checkout_path: Path
    commit_sha: str
    cancellation_token: CancellationToken
    files: tuple[str, ...] = ()


# --- vocabularies (values match the core schema CHECK constraints) ---------------------------


class Capability(str, Enum):
    DECLARATIONS = "declarations"
    IMPORTS = "imports"
    CALLS = "calls"
    INHERITANCE = "inheritance"
    INTERFACES = "interfaces"
    DEPENDENCIES = "dependencies"
    DECORATORS = "decorators"


class CapabilityLevel(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"


class EntityType(str, Enum):
    REPOSITORY = "REPOSITORY"
    MODULE = "MODULE"
    PACKAGE = "PACKAGE"
    FILE = "FILE"
    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    METHOD = "METHOD"
    FUNCTION = "FUNCTION"
    ENDPOINT = "ENDPOINT"
    DEPENDENCY = "DEPENDENCY"
    DATASTORE = "DATASTORE"
    MESSAGE_TOPIC = "MESSAGE_TOPIC"
    TECHNOLOGY = "TECHNOLOGY"
    CONFIGURATION = "CONFIGURATION"
    EXTERNAL_SYMBOL = "EXTERNAL_SYMBOL"


class RelationshipType(str, Enum):
    CONTAINS = "CONTAINS"
    DECLARES = "DECLARES"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    EXPOSES = "EXPOSES"
    DEPENDS_ON = "DEPENDS_ON"
    READS_FROM = "READS_FROM"
    WRITES_TO = "WRITES_TO"
    PUBLISHES_TO = "PUBLISHES_TO"
    CONSUMES_FROM = "CONSUMES_FROM"


class Certainty(str, Enum):
    EXACT = "EXACT"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EvidenceRole(str, Enum):
    DEFINITION = "DEFINITION"
    DECLARATION = "DECLARATION"
    USAGE = "USAGE"
    CONFIGURATION = "CONFIGURATION"
    INFERENCE_SUPPORT = "INFERENCE_SUPPORT"


class ParseStatus(str, Enum):
    PARSED = "PARSED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# Which capability an emitted fact depends on. Facts not listed (FILE entities,
# unresolved external symbols) are framework-level and always allowed.
ENTITY_CAPABILITY = {
    EntityType.CLASS: Capability.DECLARATIONS,
    EntityType.FUNCTION: Capability.DECLARATIONS,
    EntityType.METHOD: Capability.DECLARATIONS,
    EntityType.MODULE: Capability.DECLARATIONS,
    EntityType.PACKAGE: Capability.DECLARATIONS,
    EntityType.INTERFACE: Capability.INTERFACES,
    EntityType.DEPENDENCY: Capability.DEPENDENCIES,
}
RELATIONSHIP_CAPABILITY = {
    RelationshipType.CONTAINS: Capability.DECLARATIONS,
    RelationshipType.DECLARES: Capability.DECLARATIONS,
    RelationshipType.IMPORTS: Capability.IMPORTS,
    RelationshipType.CALLS: Capability.CALLS,
    RelationshipType.EXTENDS: Capability.INHERITANCE,
    RelationshipType.IMPLEMENTS: Capability.INTERFACES,
    RelationshipType.DEPENDS_ON: Capability.DEPENDENCIES,
}
DECORATOR_METADATA_KEYS = ("decorators", "annotations")
FRAMEWORK_ENTITY_TYPES = {EntityType.FILE, EntityType.EXTERNAL_SYMBOL}


# --- primitives guard ------------------------------------------------------------------------


def _require_primitive(value: Any, where: str) -> None:
    """Reject anything that is not plain JSON data (e.g. parser nodes or trees)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _require_primitive(item, where)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalContractError(f"{where}: metadata keys must be strings, got {type(key).__name__}")
            _require_primitive(item, where)
        return
    raise CanonicalContractError(f"{where}: {type(value).__module__}.{type(value).__name__} is not plain data")


def _freeze(metadata: Mapping[str, Any] | None, where: str) -> Mapping[str, Any]:
    metadata = dict(metadata or {})
    _require_primitive(metadata, where)
    return MappingProxyType(metadata)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CanonicalContractError(message)


def _require_type(value: Any, expected: type | tuple[type, ...], where: str) -> None:
    if value is not None and not isinstance(value, expected):
        raise CanonicalContractError(f"{where} must be {expected}, got {type(value).__name__}")


# --- records ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalyzerCapabilities:
    """An explicit level for every capability; nothing is implied by omission."""

    levels: Mapping[Capability, CapabilityLevel]

    def __post_init__(self) -> None:
        missing = set(Capability) - set(self.levels)
        _require(not missing, f"capabilities must declare every capability; missing {sorted(c.value for c in missing)}")
        for capability, level in self.levels.items():
            _require(isinstance(capability, Capability) and isinstance(level, CapabilityLevel),
                     f"invalid capability entry {capability!r}: {level!r}")
        object.__setattr__(self, "levels", MappingProxyType({c: self.levels[c] for c in Capability}))

    @classmethod
    def none(cls) -> AnalyzerCapabilities:
        return cls({capability: CapabilityLevel.UNSUPPORTED for capability in Capability})

    def level(self, capability: Capability) -> CapabilityLevel:
        return self.levels[capability]

    def allows(self, capability: Capability) -> bool:
        return self.levels[capability] is not CapabilityLevel.UNSUPPORTED

    def as_dict(self) -> dict[str, str]:
        return {capability.value: level.value for capability, level in self.levels.items()}


EvidenceKey = tuple[str, int, int, str, str]  # path, start_line, end_line, content_hash, extractor_version


@dataclass(frozen=True)
class SourceEvidence:
    """A located span of source that supports one or more facts."""

    path: str
    start_line: int
    end_line: int
    start_byte: int | None
    end_byte: int | None
    content_hash: str
    commit_sha: str
    extractor: str
    extractor_version: str
    certainty: Certainty = Certainty.EXACT
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("path", "content_hash", "commit_sha", "extractor", "extractor_version"):
            _require(isinstance(getattr(self, name), str) and getattr(self, name).strip(), f"evidence {name} is required")
        _require_type(self.start_line, int, "evidence start_line")
        _require_type(self.end_line, int, "evidence end_line")
        _require(self.start_line >= 1 and self.end_line >= self.start_line,
                 f"evidence line range {self.start_line}-{self.end_line} is invalid")
        _require((self.start_byte is None) == (self.end_byte is None), "evidence byte range must be complete or absent")
        if self.start_byte is not None:
            _require_type(self.start_byte, int, "evidence start_byte")
            _require_type(self.end_byte, int, "evidence end_byte")
            _require(0 <= self.start_byte <= self.end_byte, "evidence byte range is invalid")
        _require(isinstance(self.certainty, Certainty), "evidence certainty must be a Certainty")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "evidence metadata"))

    @property
    def key(self) -> EvidenceKey:
        return (self.path, self.start_line, self.end_line, self.content_hash, self.extractor_version)


@dataclass(frozen=True)
class CanonicalEntity:
    stable_key: str
    entity_type: EntityType
    name: str
    qualified_name: str | None
    path: str | None
    parent_key: str | None
    start_line: int | None
    end_line: int | None
    extractor: str
    extractor_version: str
    evidence: tuple[tuple[EvidenceKey, EvidenceRole], ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(isinstance(self.entity_type, EntityType), "entity_type must be an EntityType")
        for name in ("stable_key", "name", "extractor", "extractor_version"):
            _require(isinstance(getattr(self, name), str) and getattr(self, name).strip(), f"entity {name} is required")
        for name in ("qualified_name", "path", "parent_key"):
            _require_type(getattr(self, name), str, f"entity {name}")
        _require((self.start_line is None) == (self.end_line is None), "entity line range must be complete or absent")
        if self.start_line is not None:
            _require_type(self.start_line, int, "entity start_line")
            _require_type(self.end_line, int, "entity end_line")
            _require(self.start_line >= 1 and self.end_line >= self.start_line, "entity line range is invalid")
        _require(bool(self.evidence), f"entity {self.stable_key} has no evidence")
        _require_primitive(self.evidence, "entity evidence")
        object.__setattr__(self, "metadata", _freeze(self.metadata, f"entity {self.stable_key} metadata"))


@dataclass(frozen=True)
class CanonicalRelationship:
    source_key: str
    relationship_type: RelationshipType
    target_key: str
    certainty: Certainty
    extractor: str
    extractor_version: str
    evidence: tuple[EvidenceKey, ...]
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(isinstance(self.relationship_type, RelationshipType), "relationship_type must be a RelationshipType")
        _require(isinstance(self.certainty, Certainty), "relationship certainty must be a Certainty")
        _require(self.source_key != self.target_key, f"relationship {self.source_key} points at itself")
        _require(self.confidence is None or 0 <= self.confidence <= 1, "confidence must be within [0, 1]")
        _require(bool(self.evidence), f"relationship {self.identity} has no evidence")
        _require_primitive(self.evidence, "relationship evidence")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "relationship metadata"))

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.source_key, self.relationship_type.value, self.target_key, self.extractor_version)


@dataclass(frozen=True)
class FileExtractionError:
    path: str
    code: str
    message: str
    start_line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "start_line": self.start_line}


@dataclass(frozen=True)
class FileExtraction:
    """The complete, validated output for one file."""

    path: str
    language: str | None
    analyzer: str
    extractor_version: str
    content_hash: str
    size_bytes: int
    line_count: int
    parse_status: ParseStatus
    capabilities: AnalyzerCapabilities
    entities: tuple[CanonicalEntity, ...]
    relationships: tuple[CanonicalRelationship, ...]
    evidence: tuple[SourceEvidence, ...]
    errors: tuple[FileExtractionError, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_generated: bool = False
    is_vendored: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata, f"file {self.path} metadata"))

    @property
    def first_party(self) -> bool:
        return not (self.is_generated or self.is_vendored)


def validate_file_extraction(result: FileExtraction) -> None:
    """Reject output that is internally inconsistent or claims an unsupported fact."""
    evidence_keys = set()
    for evidence in result.evidence:
        _require(evidence.path == result.path, f"evidence for {evidence.path} attached to {result.path}")
        _require(evidence.end_line <= max(result.line_count, 1),
                 f"evidence ends at line {evidence.end_line} beyond {result.path}'s {result.line_count} lines")
        _require(evidence.end_byte is None or evidence.end_byte <= result.size_bytes,
                 f"evidence ends at byte {evidence.end_byte} beyond {result.path}'s {result.size_bytes} bytes")
        evidence_keys.add(evidence.key)

    entity_keys = {}
    for entity in result.entities:
        _require(entity.stable_key not in entity_keys, f"duplicate entity key {entity.stable_key}")
        entity_keys[entity.stable_key] = entity
        _require(entity.path in (None, result.path), f"entity {entity.stable_key} belongs to another file")
        for key, role in entity.evidence:
            _require(tuple(key) in evidence_keys, f"entity {entity.stable_key} cites unknown evidence")
            _require(isinstance(role, EvidenceRole), "evidence role must be an EvidenceRole")
        capability = ENTITY_CAPABILITY.get(entity.entity_type)
        if entity.entity_type not in FRAMEWORK_ENTITY_TYPES:
            _require(capability is not None and result.capabilities.allows(capability),
                     f"{entity.entity_type.value} entity emitted without a supporting capability")
        if any(key in entity.metadata for key in DECORATOR_METADATA_KEYS):
            _require(result.capabilities.allows(Capability.DECORATORS),
                     "decorator/annotation metadata emitted while decorators are UNSUPPORTED")

    for entity in result.entities:
        _require(entity.parent_key is None or entity.parent_key in entity_keys,
                 f"entity {entity.stable_key} has an unknown parent")

    identities = set()
    for relationship in result.relationships:
        _require(relationship.identity not in identities, f"duplicate relationship {relationship.identity}")
        identities.add(relationship.identity)
        _require(relationship.source_key in entity_keys and relationship.target_key in entity_keys,
                 f"relationship {relationship.identity} references an entity outside this file's output")
        for key in relationship.evidence:
            _require(tuple(key) in evidence_keys, f"relationship {relationship.identity} cites unknown evidence")
        capability = RELATIONSHIP_CAPABILITY.get(relationship.relationship_type)
        _require(capability is not None and result.capabilities.allows(capability),
                 f"{relationship.relationship_type.value} emitted without a supporting capability")


# --- analyzer contracts ----------------------------------------------------------------------


class ParserAdapter(Protocol):
    """Wraps one grammar. Its trees never leave the analyzer that asked for them."""

    language: str

    @property
    def available(self) -> bool: ...

    @property
    def grammar_version(self) -> str: ...

    def parse(self, source: bytes) -> Any: ...


class LanguageAnalyzer(Protocol):
    """Turns one file's source into canonical facts through a ``FileFactsBuilder``."""

    language: str
    extractor: str
    capabilities: AnalyzerCapabilities

    @property
    def available(self) -> bool: ...

    @property
    def extractor_version(self) -> str: ...

    def analyze(self, source: bytes, builder: Any, token: CancellationToken) -> list[FileExtractionError]:
        """Emit facts into ``builder``; return recoverable syntax errors (the file is then PARTIAL)."""


@dataclass(frozen=True)
class ExtractionResult:
    """Repository-level outcome passed to the next pipeline stage."""

    analysis_id: UUID
    attempt_id: UUID
    commit_sha: str
    files_total: int
    files_by_status: Mapping[str, int]
    entities: int
    relationships: int
    evidence: int
    errors: int
    languages: Mapping[str, Mapping[str, Any]]
    capability_coverage: Mapping[str, Mapping[str, int]]
    extractor_versions: Mapping[str, str]
    duration_seconds: float
    # Files and entities per origin: first_party, generated, vendored.
    files_by_origin: Mapping[str, int] = field(default_factory=dict)
    entities_by_origin: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_metrics(cls, analysis_id: UUID, attempt_id: UUID, metrics: Mapping[str, Any]) -> ExtractionResult:
        """Rebuild a result from recorded metrics, to resume after extraction without redoing it."""
        return cls(
            analysis_id=analysis_id,
            attempt_id=attempt_id,
            commit_sha=metrics["verified_commit_sha"],
            files_total=metrics["files_total"],
            files_by_status=metrics["files_by_status"],
            entities=metrics["entities"],
            relationships=metrics["relationships"],
            evidence=metrics["evidence"],
            errors=metrics["errors"],
            languages=metrics["languages"],
            capability_coverage=metrics["capability_coverage"],
            extractor_versions=metrics["extractor_versions"],
            duration_seconds=metrics["duration_ms"] / 1000,
            files_by_origin=metrics.get("files_by_origin", {}),
            entities_by_origin=metrics.get("entities_by_origin", {}),
        )

    def as_metrics(self) -> dict[str, Any]:
        return {
            "verified_commit_sha": self.commit_sha,
            "files_total": self.files_total,
            "files_by_status": dict(self.files_by_status),
            "entities": self.entities,
            "relationships": self.relationships,
            "evidence": self.evidence,
            "errors": self.errors,
            "languages": {language: dict(stats) for language, stats in self.languages.items()},
            "capability_coverage": {c: dict(levels) for c, levels in self.capability_coverage.items()},
            "extractor_versions": dict(self.extractor_versions),
            "duration_ms": round(self.duration_seconds * 1000),
            "files_by_origin": dict(self.files_by_origin),
            "entities_by_origin": dict(self.entities_by_origin),
        }
