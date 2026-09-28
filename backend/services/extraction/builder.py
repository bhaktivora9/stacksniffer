"""Collects one file's facts and turns them into validated canonical records.

Analyzers describe locations with ``Span`` (plain integers), never with parser
nodes, so nothing parser-specific can reach the records built here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .origin import Origin
from .contracts import (
    AnalyzerCapabilities,
    CanonicalEntity,
    CanonicalRelationship,
    Certainty,
    EntityType,
    EvidenceKey,
    EvidenceRole,
    FileExtraction,
    FileExtractionError,
    ParseStatus,
    RelationshipType,
    SourceEvidence,
    validate_file_extraction,
)

FILE_EXTRACTOR = "stacksniffer-file"
_CERTAINTY_RANK = {Certainty.LOW: 0, Certainty.MEDIUM: 1, Certainty.HIGH: 2, Certainty.EXACT: 3}
FILE_EXTRACTOR_VERSION = "file/1"


@dataclass(frozen=True)
class Span:
    """A source range: half-open bytes and one-based inclusive lines."""

    start_byte: int
    end_byte: int
    start_line: int
    end_line: int


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_count(source: bytes) -> int:
    if not source:
        return 1
    return source.count(b"\n") + (0 if source.endswith(b"\n") else 1)


def file_key(path: str) -> str:
    return f"file:{path}"


def external_key(language: str | None, kind: str, name: str) -> str:
    return f"external:{language or 'unknown'}:{kind}:{name}"


class FileFactsBuilder:
    """Deduplicates evidence, assigns deterministic stable keys, and validates on build."""

    def __init__(
        self,
        *,
        path: str,
        language: str | None,
        source: bytes,
        commit_sha: str,
        analyzer: str,
        extractor: str,
        extractor_version: str,
        capabilities: AnalyzerCapabilities,
    ):
        self.path = path
        self.language = language
        self.source = source
        self.commit_sha = commit_sha
        self.analyzer = analyzer
        self.extractor = extractor
        self.extractor_version = extractor_version
        self.capabilities = capabilities
        self._line_count = line_count(source)
        self._evidence: dict[EvidenceKey, SourceEvidence] = {}
        self._entities: dict[str, dict[str, Any]] = {}
        self._relationships: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.file_key = file_key(path)
        whole_file = Span(0, len(source), 1, self._line_count)
        self._add_entity(
            key=self.file_key, entity_type=EntityType.FILE, name=path.rsplit("/", 1)[-1], qualified_name=path,
            path=path, parent_key=None, span=whole_file, role=EvidenceRole.DEFINITION,
            metadata={"language": language}, extractor=FILE_EXTRACTOR, extractor_version=FILE_EXTRACTOR_VERSION,
        )

    # -- evidence ---------------------------------------------------------------------------

    def evidence(self, span: Span, *, extractor: str | None = None, extractor_version: str | None = None) -> EvidenceKey:
        start, end = max(0, span.start_byte), min(len(self.source), span.end_byte)
        start_line = max(1, span.start_line)
        end_line = min(self._line_count, max(start_line, span.end_line))
        record = SourceEvidence(
            path=self.path,
            start_line=start_line,
            end_line=end_line,
            start_byte=start,
            end_byte=max(start, end),
            content_hash=content_hash(self.source[start:end]),
            commit_sha=self.commit_sha,
            extractor=extractor or self.extractor,
            extractor_version=extractor_version or self.extractor_version,
        )
        # Identical spans collapse to one evidence row, matching the schema's uniqueness.
        return self._evidence.setdefault(record.key, record).key

    # -- entities ---------------------------------------------------------------------------

    def entity(
        self,
        entity_type: EntityType,
        name: str,
        span: Span,
        *,
        parent_key: str | None = None,
        qualified_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Declare an entity defined in this file and return its stable key.

        Keys are ``<type>:<path>::<qualified name>``; a repeated declaration of
        the same name gets ``#2``, ``#3``... in source order, so keys are
        deterministic for identical input.
        """
        base = f"{entity_type.value.lower()}:{self.path}::{qualified_name or name}"
        key, ordinal = base, 1
        while key in self._entities:
            ordinal += 1
            key = f"{base}#{ordinal}"
        self._add_entity(
            key=key, entity_type=entity_type, name=name, qualified_name=qualified_name, path=self.path,
            parent_key=parent_key or self.file_key, span=span, role=EvidenceRole.DEFINITION,
            metadata=metadata or {}, extractor=self.extractor, extractor_version=self.extractor_version,
        )
        self.relationship(RelationshipType.CONTAINS, parent_key or self.file_key, key, span, certainty=Certainty.EXACT)
        return key

    def external(self, kind: str, name: str, span: Span) -> str:
        """An unresolved target. It stays unresolved: no guess at which entity it means."""
        key = external_key(self.language, kind, name)
        if key in self._entities:
            self._entities[key]["evidence"].append((self.evidence(span), EvidenceRole.USAGE))
            return key
        self._add_entity(
            key=key, entity_type=EntityType.EXTERNAL_SYMBOL, name=name, qualified_name=name, path=None,
            parent_key=None, span=span, role=EvidenceRole.USAGE, lines=False,
            metadata={"kind": kind, "language": self.language, "resolution": "unresolved"},
            extractor=self.extractor, extractor_version=self.extractor_version,
        )
        return key

    def _add_entity(self, *, key, entity_type, name, qualified_name, path, parent_key, span, role, metadata,
                    extractor, extractor_version, lines=True) -> None:
        evidence = self.evidence(span, extractor=extractor, extractor_version=extractor_version)
        self._entities[key] = {
            "stable_key": key,
            "entity_type": entity_type,
            "name": name,
            "qualified_name": qualified_name,
            "path": path,
            "parent_key": parent_key,
            "start_line": evidence[1] if lines else None,
            "end_line": evidence[2] if lines else None,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "metadata": dict(metadata),
            "evidence": [(evidence, role)],
        }

    def has_entity(self, key: str) -> bool:
        return key in self._entities

    # -- relationships ----------------------------------------------------------------------

    def relationship(
        self,
        relationship_type: RelationshipType,
        source_key: str,
        target_key: str,
        span: Span,
        *,
        certainty: Certainty,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if source_key == target_key:
            return  # the schema forbids self-edges; recursion stays implicit
        identity = (source_key, relationship_type.value, target_key, self.extractor_version)
        evidence = self.evidence(span)
        existing = self._relationships.get(identity)
        if existing is not None:
            if evidence not in existing["evidence"]:
                existing["evidence"].append(evidence)
            # Several sites can establish the same edge; the edge is as certain as its strongest
            # site, independent of source order, and records every basis that supports it.
            bases = sorted({*existing["metadata"].get("resolution_bases", []),
                            *filter(None, [(metadata or {}).get("resolution_basis")])})
            if bases:
                existing["metadata"]["resolution_bases"] = bases
            if _CERTAINTY_RANK[certainty] > _CERTAINTY_RANK[existing["certainty"]]:
                existing["certainty"] = certainty
                if (metadata or {}).get("resolution_basis"):
                    existing["metadata"]["resolution_basis"] = metadata["resolution_basis"]
            return
        metadata = dict(metadata or {})
        if metadata.get("resolution_basis"):
            metadata["resolution_bases"] = [metadata["resolution_basis"]]
        self._relationships[identity] = {
            "source_key": source_key,
            "relationship_type": relationship_type,
            "target_key": target_key,
            "certainty": certainty,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "metadata": dict(metadata or {}),
            "evidence": [evidence],
        }

    # -- output -----------------------------------------------------------------------------

    def build(
        self,
        parse_status: ParseStatus,
        errors: list[FileExtractionError] | tuple = (),
        *,
        metadata: Mapping[str, Any] | None = None,
        origin: Origin | None = None,
    ) -> FileExtraction:
        entities = []
        for data in self._entities.values():
            unique = list(dict.fromkeys(data["evidence"]))
            entities.append(CanonicalEntity(**{**data, "evidence": tuple(unique)}))
        relationships = [
            CanonicalRelationship(**{**data, "evidence": tuple(data["evidence"])})
            for data in self._relationships.values()
        ]
        result = FileExtraction(
            path=self.path,
            language=self.language,
            analyzer=self.analyzer,
            extractor_version=self.extractor_version,
            content_hash=content_hash(self.source),
            size_bytes=len(self.source),
            line_count=self._line_count,
            parse_status=parse_status,
            capabilities=self.capabilities,
            entities=tuple(entities),
            relationships=tuple(relationships),
            evidence=tuple(self._evidence.values()),
            errors=tuple(errors),
            metadata={**(metadata or {}), **({"origin_reason": origin.reason} if origin and origin.reason else {})},
            is_generated=bool(origin and origin.is_generated),
            is_vendored=bool(origin and origin.is_vendored),
        )
        validate_file_extraction(result)
        return result
