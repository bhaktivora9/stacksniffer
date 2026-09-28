"""SS-BE-202/203: benchmark structural extraction against hand-labelled gold manifests.

Every fixture repository under ``backend/tests/fixtures/extraction/<language>/<case>``
has a gold manifest ``gold/<language>/<case>.yaml`` written from the source (see
``gold/LABELLING.md``). The production extractor runs over each fixture twice, its
output is compared with the gold facts, and the evaluator reports, per language:

* entity (declaration) and relationship precision/recall/F1, per type;
* import-statement extraction, separate from import resolution;
* resolution recall split into same-file, cross-file and overall, and the
  precision of emitted resolved edges; unresolved placeholders never count as
  correct internal links;
* unresolved-edge accuracy (external, unresolvable and ambiguous targets);
* evidence path, line-range and source-integrity validity;
* capability-reporting accuracy, unsupported-fact leakage, malformed-file
  isolation, syntax-error locations and run-to-run determinism;
* every mismatch, with an error category;
* the SS-BE-202 quality gates.

    python -m backend.evaluation.extraction_metrics \
        [--json backend/evaluation/results/structural-extraction-baseline.json] \
        [--markdown docs/evaluation/structural-extraction-baseline.md]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

try:
    from services.extraction.contracts import (
        CancellationToken, Capability, CapabilityLevel, EntityType, ExtractionContext, FileExtraction, ParseStatus,
        RelationshipType,
    )
    from services.extraction.extractor import RepositoryExtractor
    from services.extraction.registry import AnalyzerRegistry
    from services.extraction.store import build_batch_payload
except ModuleNotFoundError:
    from backend.services.extraction.contracts import (
        CancellationToken, Capability, CapabilityLevel, EntityType, ExtractionContext, FileExtraction, ParseStatus,
        RelationshipType,
    )
    from backend.services.extraction.extractor import RepositoryExtractor
    from backend.services.extraction.registry import AnalyzerRegistry
    from backend.services.extraction.store import build_batch_payload

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPOSITORY_ROOT / "backend" / "tests" / "fixtures" / "extraction"
GOLD_DIR = FIXTURES_DIR / "gold"
RESULTS_PATH = REPOSITORY_ROOT / "backend" / "evaluation" / "results" / "structural-extraction-baseline.json"
REPORT_PATH = REPOSITORY_ROOT / "docs" / "evaluation" / "structural-extraction-baseline.md"
RESULT_SCHEMA_VERSION = 2
# Bump when the gold set, the scoring rules or the published format change meaningfully.
BASELINE_VERSION = 4  # 3: Scala; 4: Go and the method_set basis

DECLARATION_TYPES = ("CLASS", "INTERFACE", "FUNCTION", "METHOD")
RELATIONSHIP_TYPES = ("CONTAINS", "IMPORTS", "CALLS", "EXTENDS", "IMPLEMENTS")
REFERENCE_TYPES = ("IMPORTS", "CALLS", "EXTENDS", "IMPLEMENTS")  # edges that need resolution
INTERNAL = ("same_file", "cross_file")
UNRESOLVED = ("external", "unresolvable", "ambiguous")
_ORDINAL = re.compile(r"#\d+$")
_EVALUATION_SHA = "0" * 40

# Error categories. The first seven are SS-BE-202's; two more are kept distinct on purpose:
# a declaration lost to Tree-sitter error recovery is not a query gap, and leakage is a gate.
QUERY_OMISSION = "parser query gap"            # syntax the analyzer does not query (or over-matches)
INCORRECT_STABLE_KEY = "normalization error"   # a fact emitted under the wrong key, name or basis
RECEIVER_RESOLUTION = "receiver-resolution error"
AMBIGUOUS_SYMBOL = "ambiguity"
CROSS_FILE_MISSING = "cross-file limitation"
INCORRECT_EVIDENCE = "evidence-range error"
LABEL_DEFECT = "incorrect fixture label"
PARSER_LIMITATION = "parser error recovery"
UNSUPPORTED_CAPABILITY = "unsupported-fact leakage"
SPURIOUS_FACT = QUERY_OMISSION
SAME_FILE_RESOLVER = RECEIVER_RESOLUTION
CATEGORIES = (QUERY_OMISSION, INCORRECT_STABLE_KEY, RECEIVER_RESOLUTION, AMBIGUOUS_SYMBOL, CROSS_FILE_MISSING,
              INCORRECT_EVIDENCE, LABEL_DEFECT, PARSER_LIMITATION, UNSUPPORTED_CAPABILITY)

# How an internal edge is (or should be) resolved. The first seven are produced by the analyzers;
# the import/inference bases need repository-level resolution and are labelled so recall shows it.
BASES = ("lexical_scope", "enclosing_class", "base_class", "type_name", "constructor", "declared_type",
         "constructed_instance", "method_set", "import_path", "import_binding", "inferred_type")
RECEIVER_BASES = {"enclosing_class", "base_class", "declared_type", "constructed_instance", "inferred_type"}
# The certainty each basis warrants (see RESOLUTION_BASIS in the analyzers).
EXPECTED_CERTAINTY = {"lexical_scope": "MEDIUM", "enclosing_class": "MEDIUM", "base_class": "MEDIUM",
                      "type_name": "MEDIUM", "constructor": "MEDIUM", "declared_type": "LOW",
                      "constructed_instance": "LOW", "method_set": "LOW"}
ORIGINS = ("first_party", "generated", "vendored")
# The capability each fact type depends on, for grouping metrics by declared capability level.
FACT_CAPABILITY = {"CLASS": "declarations", "FUNCTION": "declarations", "METHOD": "declarations",
                   "INTERFACE": "interfaces", "CONTAINS": "declarations", "import statements": "imports",
                   "CALLS": "calls", "EXTENDS": "inheritance", "IMPLEMENTS": "interfaces",
                   "decorators": "decorators"}


# --- gold manifests --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Span(_Strict):
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self):
        if self.end_line < self.start_line:
            raise ValueError(f"span {self.file}:{self.start_line}-{self.end_line} ends before it starts")
        return self

    def as_tuple(self) -> tuple[str, int, int]:
        return (self.file, self.start_line, self.end_line)


class FileExpectation(_Strict):
    parse_status: ParseStatus
    syntax_error_lines: tuple[int, ...] = ()
    origin: Literal["first_party", "generated", "vendored"] = "first_party"


class EntityFact(_Strict):
    id: str
    kind: Literal["entity"]
    entity_type: Literal["CLASS", "INTERFACE", "FUNCTION", "METHOD"]
    qualified_name: str
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    decorators: tuple[str, ...] = ()
    note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.entity_type.lower()}:{self.file}::{self.qualified_name}"


class RelationshipFact(_Strict):
    id: str
    kind: Literal["relationship"]
    relationship_type: Literal["IMPORTS", "CALLS", "EXTENDS", "IMPLEMENTS"]
    source_key: str
    target_key: str
    resolution: Literal["same_file", "cross_file", "external", "unresolvable", "ambiguous"]
    evidence: tuple[Span, ...] = Field(min_length=1)
    module: str | None = None
    # How this internal edge should be resolved (required exactly when the target is internal).
    basis: Literal[BASES] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if (self.resolution in INTERNAL) != (self.basis is not None):
            raise ValueError(f"{self.id}: internal edges, and only internal edges, carry a resolution basis")
        external = self.target_key.startswith("external:")
        if external != (self.resolution in UNRESOLVED):
            raise ValueError(f"{self.id}: resolution {self.resolution} contradicts target {self.target_key}")
        if self.relationship_type == "IMPORTS" and not self.module:
            raise ValueError(f"{self.id}: import facts record the module as written")
        if self.resolution in INTERNAL:
            same = _key_file(self.source_key) == _key_file(self.target_key)
            if same != (self.resolution == "same_file"):
                raise ValueError(f"{self.id}: resolution {self.resolution} contradicts the target's file")
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.relationship_type, self.source_key, self.target_key)


class UnsupportedFact(_Strict):
    id: str
    kind: Literal["unsupported"]
    capability: Capability
    entity_type: str
    name: str
    file: str
    note: str | None = None


class Review(_Strict):
    status: Literal["pending_independent_review", "reviewed"]
    reviewer: str | None = None


class GoldCase(_Strict):
    gold_version: int
    fixture: str
    language: str
    split: Literal["development", "holdout"]
    description: str
    labelled_from: Literal["source"]
    review: Review
    files: dict[str, FileExpectation]
    facts: tuple[Union[EntityFact, RelationshipFact, UnsupportedFact], ...] = Field(discriminator=None)

    @model_validator(mode="after")
    def _valid(self):
        ids = [fact.id for fact in self.facts]
        duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
        if duplicates:
            raise ValueError(f"{self.fixture}: duplicate fact ids {duplicates}")
        keys = {f.key for f in self.entities}
        if len(keys) != len(self.entities):
            raise ValueError(f"{self.fixture}: two entity facts share a key")
        known = keys | {f"file:{path}" for path in self.files}
        for fact in self.relationships:
            for key in (fact.source_key, fact.target_key):
                if not key.startswith("external:") and key not in known:
                    raise ValueError(f"{fact.id}: {key} is not a labelled entity or fixture file")
            for span in fact.evidence:
                if span.file not in self.files:
                    raise ValueError(f"{fact.id}: evidence file {span.file} is not in the fixture")
        for fact in self.entities:
            if fact.file not in self.files or fact.end_line < fact.start_line:
                raise ValueError(f"{fact.id}: invalid file or line range")
        return self

    @property
    def entities(self) -> list[EntityFact]:
        return [f for f in self.facts if isinstance(f, EntityFact)]

    @property
    def relationships(self) -> list[RelationshipFact]:
        return [f for f in self.facts if isinstance(f, RelationshipFact)]

    @property
    def unsupported(self) -> list[UnsupportedFact]:
        return [f for f in self.facts if isinstance(f, UnsupportedFact)]

    @property
    def name(self) -> str:
        return self.fixture


def _key_file(key: str) -> str:
    return key.split(":", 1)[1].split("::")[0]


def _strip_ordinal(name: str) -> str:
    return _ORDINAL.sub("", name)


def derived_contains(case: GoldCase) -> dict[tuple[str, str, str], tuple[str, tuple[str, int, int]]]:
    """CONTAINS facts implied by the gold entities: the nearest labelled enclosing declaration, else the file."""
    by_file: dict[str, list[EntityFact]] = defaultdict(list)
    for entity in case.entities:
        by_file[entity.file].append(entity)
    contains = {}
    for entity in case.entities:
        own = _strip_ordinal(entity.qualified_name)
        parent_key, best = f"file:{entity.file}", -1
        for candidate in by_file[entity.file]:
            prefix = _strip_ordinal(candidate.qualified_name)
            if candidate is entity or not own.startswith(prefix + "."):
                continue
            if len(prefix) > best:
                parent_key, best = candidate.key, len(prefix)
        span = (entity.file, entity.start_line, entity.end_line)
        contains[("CONTAINS", parent_key, entity.key)] = (f"{entity.id}.contained", span)
    return contains


def load_gold(gold_dir: Path = GOLD_DIR) -> list[GoldCase]:
    cases = []
    for path in sorted(gold_dir.glob("*/*.yaml")):
        case = GoldCase.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        expected_path = gold_dir / case.language / f"{Path(case.fixture).name}.yaml"
        if path != expected_path or Path(case.fixture).parts[0] != case.language:
            raise ValueError(f"{path}: fixture {case.fixture} does not match its manifest location")
        cases.append(case)
    return cases


def load_capabilities(gold_dir: Path = GOLD_DIR) -> dict[str, dict[str, str]]:
    return yaml.safe_load((gold_dir / "capabilities.yaml").read_text(encoding="utf-8"))["languages"]


def load_triage(gold_dir: Path = GOLD_DIR) -> dict[str, dict[str, str]]:
    """Manual category overrides (e.g. a confirmed fixture/label defect), keyed by mismatch id."""
    path = gold_dir / "triage.yaml"
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("overrides", {})


# --- observation -----------------------------------------------------------------------------


def _external_name(entity) -> str:
    return f"external:{entity.name}"


@dataclass
class ObservedEdge:
    identity: tuple[str, str, str]
    evidence: set[tuple[str, int, int]]
    resolved: bool
    metadata: dict
    certainty: str | None = None


@dataclass
class Observation:
    files: dict[str, FileExtraction]
    entities: dict[str, Any]
    edges: dict[tuple[str, str, str], ObservedEdge]
    imports: dict[tuple[str, str], set[tuple[str, int, int]]]
    payload: dict
    failure: str | None = None


def extract_fixture(root: Path, registry: AnalyzerRegistry | None = None) -> tuple[list[FileExtraction], str | None]:
    files = tuple(sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                         if p.is_file() and "__pycache__" not in p.parts))
    collected: list[FileExtraction] = []

    class _Collect:
        def persist(self, batch):
            collected.extend(batch)

    context = ExtractionContext(UUID(int=0), UUID(int=0), root, _EVALUATION_SHA, CancellationToken(), files)
    try:
        RepositoryExtractor(registry or AnalyzerRegistry.default()).extract(context, _Collect())
    except Exception as exc:  # a repository-level failure is itself a measured outcome
        return collected, f"{type(exc).__name__}: {exc}"
    return collected, None


def observe(extractions: list[FileExtraction], failure: str | None = None) -> Observation:
    keys: dict[str, str] = {}
    entities: dict[str, Any] = {}
    for file in extractions:
        for entity in file.entities:
            if entity.entity_type is EntityType.EXTERNAL_SYMBOL:
                keys[entity.stable_key] = _external_name(entity)
            else:
                keys[entity.stable_key] = entity.stable_key
                entities[entity.stable_key] = entity
    edges: dict[tuple[str, str, str], ObservedEdge] = {}
    imports: dict[tuple[str, str], set] = defaultdict(set)
    for file in extractions:
        for relationship in file.relationships:
            rtype = relationship.relationship_type.value
            if rtype not in RELATIONSHIP_TYPES:
                continue
            target = keys[relationship.target_key]
            identity = (rtype, keys[relationship.source_key], target)
            spans = {(k[0], k[1], k[2]) for k in relationship.evidence}
            edge = edges.setdefault(identity, ObservedEdge(identity, set(), not target.startswith("external:"),
                                                          dict(relationship.metadata),
                                                          relationship.certainty.value))
            edge.evidence |= spans
            if rtype == "IMPORTS":
                module = relationship.metadata.get("module") or target.removeprefix("external:")
                imports[(identity[1], module)] |= spans
    return Observation({f.path: f for f in extractions}, entities, edges, dict(imports),
                       build_batch_payload(extractions), failure)


# --- scoring primitives ----------------------------------------------------------------------


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, tp=0, fp=0, fn=0) -> None:
        self.tp += tp
        self.fp += fp
        self.fn += fn

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "precision": round(self.precision, 4),
                "recall": round(self.recall, 4), "f1": round(self.f1, 4)}


@dataclass
class Ratio:
    good: int = 0
    total: int = 0

    def add(self, ok: bool) -> None:
        self.total += 1
        self.good += bool(ok)

    @property
    def rate(self) -> float:
        return self.good / self.total if self.total else 1.0

    def as_dict(self) -> dict:
        return {"valid": self.good, "total": self.total, "rate": round(self.rate, 4)}


@dataclass
class Mismatch:
    id: str
    case: str
    area: str
    outcome: str  # "missed", "unexpected" or "wrong"
    detail: str
    category: str
    fact_id: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class LanguageScore:
    language: str
    cases: list[str] = field(default_factory=list)
    entities: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    relationships: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    import_statements: Counts = field(default_factory=Counts)
    resolution: dict[str, dict[str, Counts]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(Counts)))
    resolved_precision: Counts = field(default_factory=Counts)
    resolved_by_basis: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    resolution_by_expected_basis: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    basis_accuracy: Ratio = field(default_factory=Ratio)
    certainty_accuracy: Ratio = field(default_factory=Ratio)
    entities_by_origin: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    relationships_by_origin: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    origin_classification: Ratio = field(default_factory=Ratio)
    unresolved: Counts = field(default_factory=Counts)
    unresolved_by_kind: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    evidence_path: Ratio = field(default_factory=Ratio)
    evidence_lines: Ratio = field(default_factory=Ratio)
    source_integrity: Ratio = field(default_factory=Ratio)
    decorators: Ratio = field(default_factory=Ratio)
    parse_status: Ratio = field(default_factory=Ratio)
    syntax_error_location: Ratio = field(default_factory=Ratio)
    malformed_files: int = 0
    malformed_isolated: int = 0
    repository_failures: int = 0
    leakage: list[str] = field(default_factory=list)
    determinism: Ratio = field(default_factory=Ratio)
    key_agreement: Ratio = field(default_factory=Ratio)
    capabilities: dict = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)
    extractor_versions: dict[str, str] = field(default_factory=dict)


# --- per-case scoring ------------------------------------------------------------------------


def _source_bytes(root: Path, path: str) -> bytes | None:
    try:
        return root.joinpath(*path.split("/")).read_bytes()
    except OSError:
        return None


def _check_source_integrity(score: LanguageScore, root: Path, extractions: list[FileExtraction]) -> None:
    """Re-open every evidence span: its bytes must hash to the recorded value and sit on the recorded lines."""
    cache: dict[str, bytes | None] = {}
    for file in extractions:
        for evidence in file.evidence:
            data = cache.setdefault(evidence.path, _source_bytes(root, evidence.path))
            ok = data is not None and evidence.start_byte is not None
            if ok:
                chunk = data[evidence.start_byte:evidence.end_byte]
                ok = hashlib.sha256(chunk).hexdigest() == evidence.content_hash
                ok = ok and data[:evidence.start_byte].count(b"\n") + 1 == evidence.start_line
            score.source_integrity.add(ok)


def _overlaps(a: set, b: set) -> bool:
    return any(x[0] == y[0] and x[1] <= y[2] and y[1] <= x[2] for x in a for y in b)


def _observed_origin(observation: Observation, path: str | None) -> str | None:
    file = observation.files.get(path) if path else None
    if file is None:
        return None
    return "vendored" if file.is_vendored else "generated" if file.is_generated else "first_party"


def _file_partial(observation: Observation, path: str) -> bool:
    file = observation.files.get(path)
    return file is not None and file.parse_status in (ParseStatus.PARTIAL, ParseStatus.FAILED)


def score_case(case: GoldCase, root: Path, score: LanguageScore, triage: dict, *,
               first: Observation, second: Observation) -> None:
    name = case.name
    score.cases.append(name)
    observation = first
    mismatches: list[Mismatch] = []

    def mismatch(area, outcome, detail, category, fact_id=None):
        mid = f"{name}:{area}:{outcome}:{detail}"
        override = triage.get(mid)
        mismatches.append(Mismatch(mid, name, area, outcome, detail,
                                   override["category"] if override else category, fact_id))

    # Determinism: two clean runs must produce identical canonical output.
    score.determinism.add(first.payload == second.payload and first.failure == second.failure)
    keys_a = {e["stable_key"] for e in first.payload["entities"]}
    keys_b = {e["stable_key"] for e in second.payload["entities"]}
    for key in keys_a | keys_b:
        score.key_agreement.add(key in keys_a and key in keys_b)

    # Repository-level isolation.
    malformed = [p for p, f in case.files.items() if f.parse_status in (ParseStatus.PARTIAL, ParseStatus.FAILED)]
    score.malformed_files += len(malformed)
    if observation.failure:
        score.repository_failures += 1
        mismatch("repository", "wrong", f"extraction failed: {observation.failure}", PARSER_LIMITATION)
    for path in malformed:
        others_ok = all(observation.files.get(p) is not None and observation.files[p].parse_status is f.parse_status
                        for p, f in case.files.items() if p != path)
        score.malformed_isolated += (not observation.failure) and others_ok

    for path, expected in case.files.items():
        actual = observation.files.get(path)
        ok = actual is not None and actual.parse_status is expected.parse_status
        score.parse_status.add(ok)
        if not ok:
            mismatch("parse_status", "wrong", f"{path}: expected {expected.parse_status.value}, "
                                              f"got {actual.parse_status.value if actual else None}", PARSER_LIMITATION)
        if expected.syntax_error_lines:
            reported = {e.start_line for e in actual.errors} if actual else set()
            for line in expected.syntax_error_lines:
                score.syntax_error_location.add(line in reported)

    _check_source_integrity(score, root, list(observation.files.values()))

    gold_origin = {path: spec.origin for path, spec in case.files.items()}
    for path, expected_origin in gold_origin.items():
        actual_origin = _observed_origin(observation, path)
        score.origin_classification.add(actual_origin == expected_origin)
        if actual_origin != expected_origin:
            mismatch("origin", "wrong", f"{path}: expected {expected_origin}, got {actual_origin}",
                     INCORRECT_STABLE_KEY)

    def origin_of(key_or_path: str | None) -> str:
        path = _key_file(key_or_path) if key_or_path and ":" in key_or_path else key_or_path
        return gold_origin.get(path) or _observed_origin(observation, path) or "first_party"

    # --- entities --------------------------------------------------------------------------
    gold_entities = {f.key: f for f in case.entities}
    observed_entities = {k: e for k, e in observation.entities.items()
                         if e.entity_type.value in DECLARATION_TYPES}
    missed_entities = {}
    for etype in DECLARATION_TYPES:
        expected = {k for k, f in gold_entities.items() if f.entity_type == etype}
        actual = {k for k, e in observed_entities.items() if e.entity_type.value == etype}
        score.entities[etype].add(len(expected & actual), len(actual - expected), len(expected - actual))
        score.entities["overall"].add(len(expected & actual), len(actual - expected), len(expected - actual))
        for key in expected | actual:
            fact = gold_entities.get(key)
            origin = origin_of(fact.file if fact else observed_entities[key].path)
            score.entities_by_origin[origin].add(tp=int(key in expected and key in actual),
                                                 fp=int(key not in expected), fn=int(key not in actual))
        for key in sorted(expected - actual):
            fact = gold_entities[key]
            twin = next((k for k in actual - expected if observed_entities[k].path == fact.file
                         and observed_entities[k].start_line == fact.start_line), None)
            category = (INCORRECT_STABLE_KEY if twin else
                        PARSER_LIMITATION if _file_partial(observation, fact.file) else QUERY_OMISSION)
            missed_entities[key] = category
            mismatch("entities", "missed", key + (f" (emitted as {twin})" if twin else ""), category, fact.id)
        for key in sorted(actual - expected):
            entity = observed_entities[key]
            twin = next((k for k in expected - actual if gold_entities[k].file == entity.path
                         and gold_entities[k].start_line == entity.start_line), None)
            category = (INCORRECT_STABLE_KEY if twin else
                        PARSER_LIMITATION if _file_partial(observation, entity.path) else SPURIOUS_FACT)
            mismatch("entities", "unexpected", key, category)
        for key in sorted(expected & actual):
            fact, entity = gold_entities[key], observed_entities[key]
            definition = next((k for k, role in entity.evidence if role.value == "DEFINITION"), entity.evidence[0][0])
            score.evidence_path.add(definition[0] == fact.file)
            lines_ok = (definition[0], definition[1], definition[2]) == (fact.file, fact.start_line, fact.end_line)
            score.evidence_lines.add(lines_ok and (entity.start_line, entity.end_line) == (fact.start_line, fact.end_line))
            if not lines_ok:
                mismatch("entity_evidence", "wrong", f"{key}: expected {fact.start_line}-{fact.end_line}, "
                                                     f"got {definition[1]}-{definition[2]}", INCORRECT_EVIDENCE, fact.id)
            metadata = dict(entity.metadata)
            decorators = tuple(metadata.get("decorators") or metadata.get("annotations") or ())
            score.decorators.add(decorators == tuple(fact.decorators))
            if decorators != tuple(fact.decorators):
                mismatch("decorators", "wrong", f"{key}: expected {list(fact.decorators)}, got {list(decorators)}",
                         QUERY_OMISSION, fact.id)

    # --- relationships ---------------------------------------------------------------------
    gold_edges: dict[tuple, dict] = {}
    for fact in case.relationships:
        entry = gold_edges.setdefault(fact.identity, {"facts": [], "evidence": set(), "resolution": fact.resolution,
                                                      "basis": fact.basis})
        entry["facts"].append(fact)
        entry["evidence"] |= {s.as_tuple() for s in fact.evidence}
    for identity, (fact_id, span) in derived_contains(case).items():
        gold_edges[identity] = {"facts": [], "id": fact_id, "evidence": {span}, "resolution": "same_file",
                                "basis": None}

    observed = observation.edges
    matched_observed: set = set()
    for identity, entry in sorted(gold_edges.items()):
        rtype = identity[0]
        fact_id = entry.get("id") or entry["facts"][0].id
        edge = observed.get(identity)
        score.relationships_by_origin[origin_of(identity[1])].add(tp=int(edge is not None), fn=int(edge is None))
        if edge is not None:
            matched_observed.add(identity)
            score.relationships[rtype].add(tp=1)
            score.relationships["overall"].add(tp=1)
            if rtype in REFERENCE_TYPES and entry["basis"] and edge.resolved:
                actual_basis = edge.metadata.get("resolution_basis")
                score.basis_accuracy.add(actual_basis == entry["basis"])
                if actual_basis != entry["basis"]:
                    mismatch("resolution_basis", "wrong", f"{' '.join(identity)}: expected {entry['basis']}, "
                             f"got {actual_basis}", INCORRECT_STABLE_KEY, fact_id)
                expected_certainty = EXPECTED_CERTAINTY.get(entry["basis"])
                if expected_certainty:
                    score.certainty_accuracy.add(edge.certainty == expected_certainty)
                    if edge.certainty != expected_certainty:
                        mismatch("certainty", "wrong", f"{' '.join(identity)}: expected {expected_certainty}, "
                                 f"got {edge.certainty}", INCORRECT_STABLE_KEY, fact_id)
            paths_ok = all(span[0] == _key_file(identity[1]) for span in edge.evidence)
            score.evidence_path.add(paths_ok)
            lines_ok = edge.evidence == entry["evidence"]
            score.evidence_lines.add(lines_ok)
            if not lines_ok:
                mismatch("relationship_evidence", "wrong", f"{' '.join(identity)}: expected "
                         f"{sorted(entry['evidence'])}, got {sorted(edge.evidence)}", INCORRECT_EVIDENCE, fact_id)
        else:
            score.relationships[rtype].add(fn=1)
            score.relationships["overall"].add(fn=1)
            category, counterpart = _categorize_missed_edge(identity, entry, observation, missed_entities)
            detail = " ".join(identity) + (f" (emitted as {counterpart})" if counterpart else "")
            mismatch("relationships", "missed", detail, category, fact_id)

        resolution = entry["resolution"]
        if rtype in REFERENCE_TYPES and resolution in INTERNAL:
            hit = edge is not None
            for scope in (resolution, "overall"):
                score.resolution[rtype][scope].add(tp=int(hit), fn=int(not hit))
            score.resolution_by_expected_basis[entry["basis"]].add(tp=int(hit), fn=int(not hit))
        if rtype in REFERENCE_TYPES and resolution in UNRESOLVED:
            hit = edge is not None and not edge.resolved
            score.unresolved.add(tp=int(hit), fn=int(not hit))
            score.unresolved_by_kind[resolution].add(tp=int(hit), fn=int(not hit))

    for identity, edge in sorted(observed.items()):
        rtype = identity[0]
        basis = edge.metadata.get("resolution_basis", "unrecorded")
        if identity in matched_observed:
            if rtype in REFERENCE_TYPES and edge.resolved:
                score.resolved_precision.add(tp=1)
                score.resolved_by_basis[basis].add(tp=1)
            continue
        score.relationships[rtype].add(fp=1)
        score.relationships["overall"].add(fp=1)
        score.relationships_by_origin[origin_of(identity[1])].add(fp=1)
        if rtype in REFERENCE_TYPES:
            (score.resolved_precision if edge.resolved else score.unresolved).add(fp=1)
            if edge.resolved:
                score.resolved_by_basis[basis].add(fp=1)
        category, counterpart = _categorize_unexpected_edge(identity, edge, gold_edges, observation, missed_entities)
        detail = " ".join(identity) + (f" (label: {counterpart})" if counterpart else "")
        mismatch("relationships", "unexpected", detail, category)

    # --- import statements (extraction, independent of where they resolve) ---------------------
    gold_imports: dict[tuple[str, str], tuple[str, set]] = {}
    for fact in case.relationships:
        if fact.relationship_type == "IMPORTS":
            key = (fact.source_key, fact.module)
            spans = gold_imports.get(key, (fact.id, set()))[1] | {s.as_tuple() for s in fact.evidence}
            gold_imports[key] = (fact.id, spans)
    expected, actual = set(gold_imports), set(observation.imports)
    score.import_statements.add(len(expected & actual), len(actual - expected), len(expected - actual))
    for key in sorted(expected - actual):
        mismatch("import_statements", "missed", f"{key[0]} imports {key[1]}", QUERY_OMISSION, gold_imports[key][0])
    for key in sorted(actual - expected):
        mismatch("import_statements", "unexpected", f"{key[0]} imports {key[1]}", SPURIOUS_FACT)

    # --- unsupported facts must stay unknown -----------------------------------------------------
    emitted = [(e.entity_type.value, e.name) for f in observation.files.values() for e in f.entities]
    for fact in case.unsupported:
        if (fact.entity_type, fact.name) in emitted:
            score.leakage.append(fact.id)
            mismatch("unsupported", "unexpected", f"{fact.entity_type} {fact.name}", UNSUPPORTED_CAPABILITY, fact.id)
    for file in observation.files.values():
        for relationship in file.relationships:
            from_capability = {"IMPLEMENTS": Capability.INTERFACES, "DEPENDS_ON": Capability.DEPENDENCIES,
                               "CALLS": Capability.CALLS, "EXTENDS": Capability.INHERITANCE}
            capability = from_capability.get(relationship.relationship_type.value)
            if capability and not file.capabilities.allows(capability):
                score.leakage.append(f"{file.path}:{relationship.identity}")

    for file in observation.files.values():
        if file.parse_status in (ParseStatus.PARSED, ParseStatus.PARTIAL):
            score.extractor_versions[file.language or "unknown"] = file.extractor_version
    score.mismatches.extend(mismatches)


def _categorize_missed_edge(identity, entry, observation, missed_entities) -> tuple[str, str | None]:
    rtype, source, target = identity
    resolution = entry["resolution"]
    if source in missed_entities:
        return missed_entities[source], None
    if target in missed_entities:
        return missed_entities[target], None
    counterparts = [e for i, e in observation.edges.items()
                    if i[0] == rtype and i != identity and _overlaps(e.evidence, entry["evidence"])
                    and (i[1] == source or rtype == "CALLS")]
    if rtype == "CONTAINS":
        return QUERY_OMISSION, None
    # Several calls can share a line: prefer the counterpart named like the labelled target.
    wanted = target.rsplit("::", 1)[-1].split("(")[0].rsplit(".", 1)[-1].removeprefix("external:")
    counterparts.sort(key=lambda e: (not e.identity[2].split("(")[0].endswith(wanted), e.identity))
    for edge in counterparts:
        other = edge.identity[2]
        if edge.identity[1] != source:
            continue
        if resolution in INTERNAL:
            if not edge.resolved:
                if edge.metadata.get("ambiguous_candidates"):
                    return AMBIGUOUS_SYMBOL, other
                if resolution == "cross_file":
                    return CROSS_FILE_MISSING, other
                return (RECEIVER_RESOLUTION if entry.get("basis") in RECEIVER_BASES else INCORRECT_STABLE_KEY), other
            return (RECEIVER_RESOLUTION if entry.get("basis") in RECEIVER_BASES else INCORRECT_STABLE_KEY), other
        return (INCORRECT_STABLE_KEY if not edge.resolved else SAME_FILE_RESOLVER), other
    if counterparts:  # same site, different source: the enclosing declaration was not recognised
        return QUERY_OMISSION, counterparts[0].identity[1]
    if resolution == "cross_file":
        return CROSS_FILE_MISSING, None
    file = _key_file(source)
    return (PARSER_LIMITATION if _file_partial(observation, file) else QUERY_OMISSION), None


def _categorize_unexpected_edge(identity, edge, gold_edges, observation, missed_entities) -> tuple[str, str | None]:
    rtype, source, target = identity
    def closeness(item):
        gold_target = item[0][2].rsplit("::", 1)[-1].split("(")[0].rsplit(".", 1)[-1].removeprefix("external:")
        return (not target.split("(")[0].endswith(gold_target), item[0])

    for gold_identity, entry in sorted(gold_edges.items(), key=closeness):
        if gold_identity[0] != rtype or gold_identity in observation.edges:
            continue
        if not _overlaps(edge.evidence, entry["evidence"]):
            continue
        if gold_identity[1] != source:
            return (missed_entities.get(gold_identity[1], QUERY_OMISSION), " ".join(gold_identity))
        category, _ = _categorize_missed_edge(gold_identity, entry, observation, missed_entities)
        return category, " ".join(gold_identity)
    if source in missed_entities or target in missed_entities:
        return missed_entities.get(source) or missed_entities.get(target), None
    if not source.startswith("file:") and source not in observation.entities:
        return QUERY_OMISSION, None
    file = _key_file(source)
    return (PARSER_LIMITATION if _file_partial(observation, file) else SPURIOUS_FACT), None


# --- capabilities ----------------------------------------------------------------------------


def score_capabilities(score: LanguageScore, registry: AnalyzerRegistry, expected: dict[str, str]) -> None:
    analyzer = next(a for a in registry.analyzers if a.language == score.language)
    declared = analyzer.capabilities.as_dict()
    matches = {c: declared[c] == expected.get(c) for c in declared}
    # A capability declared SUPPORTED must meet its recall gate on the fixtures.
    measured_recall = {
        "declarations": score.entities["overall"].recall,
        "imports": score.import_statements.recall,
        "calls": score.resolution["CALLS"]["overall"].recall,
        "inheritance": score.resolution["EXTENDS"]["overall"].recall,
        "interfaces": score.resolution["IMPLEMENTS"]["overall"].recall,
    }
    consistency = []
    for capability, level in declared.items():
        recall = measured_recall.get(capability)
        if level == CapabilityLevel.SUPPORTED.value and recall is not None and recall < 0.95:
            consistency.append(f"{capability} is declared SUPPORTED but measured recall is {recall:.3f}")
    score.capabilities = {
        "declared": declared,
        "expected": expected,
        "accuracy": round(sum(matches.values()) / len(matches), 4),
        "mismatched": sorted(c for c, ok in matches.items() if not ok),
        "consistency_issues": consistency,
    }


# --- gates -----------------------------------------------------------------------------------


GATES = (
    ("declaration_precision", ">=", 0.98, lambda s: s.entities["overall"].precision),
    ("declaration_recall", ">=", 0.95, lambda s: s.entities["overall"].recall),
    ("import_precision", ">=", 0.95, lambda s: s.import_statements.precision),
    ("import_recall", ">=", 0.95, lambda s: s.import_statements.recall),
    ("resolved_edge_precision", ">=", 0.90, lambda s: s.resolved_precision.precision),
    ("evidence_path_validity", ">=", 1.0, lambda s: min(s.evidence_path.rate, s.source_integrity.rate)),
    ("evidence_line_range_validity", ">=", 1.0, lambda s: s.evidence_lines.rate),
    ("deterministic_stable_keys", ">=", 1.0, lambda s: min(s.key_agreement.rate, s.determinism.rate)),
    ("unsupported_fact_leakage", "<=", 0, lambda s: len(s.leakage)),
    ("repository_failures_from_malformed_files", "<=", 0, lambda s: s.repository_failures),
)


def gates_for(score: LanguageScore) -> list[dict]:
    results = []
    for name, op, required, measure in GATES:
        actual = measure(score)
        passed = actual >= required if op == ">=" else actual <= required
        results.append({"gate": name, "required": f"{op} {required}", "actual": round(actual, 4), "passed": passed})
    return results


# --- orchestration ---------------------------------------------------------------------------


def evaluate(fixtures_dir: Path = FIXTURES_DIR, registry: AnalyzerRegistry | None = None) -> dict:
    registry = registry or AnalyzerRegistry.default()
    gold_dir = fixtures_dir / "gold"
    cases = load_gold(gold_dir)
    capabilities = load_capabilities(gold_dir)
    triage = load_triage(gold_dir)
    scores: dict[str, LanguageScore] = {}
    splits: dict[str, dict[str, LanguageScore]] = defaultdict(dict)
    for case in cases:
        root = fixtures_dir / case.fixture
        first = observe(*extract_fixture(root, registry))
        second = observe(*extract_fixture(root, registry))
        for target in (scores.setdefault(case.language, LanguageScore(case.language)),
                       splits[case.language].setdefault(case.split, LanguageScore(case.language))):
            score_case(case, root, target, triage, first=first, second=second)
    for language, score in scores.items():
        score_capabilities(score, registry, capabilities.get(language, {}))
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "baseline_version": BASELINE_VERSION,
        "gold_version": max(c.gold_version for c in cases),
        "review_status": sorted({c.review.status for c in cases}),
        "languages": {lang: _language_result(scores[lang], splits[lang]) for lang in sorted(scores)},
    }


def _by_capability_level(score: LanguageScore) -> dict:
    """Fact-type counts grouped by the declared level of the capability they depend on."""
    declared = score.capabilities.get("declared", {})
    groups: dict[str, dict] = {}
    sources = [(t, score.entities[t]) for t in DECLARATION_TYPES]
    sources += [(t, score.relationships[t]) for t in ("CONTAINS", "CALLS", "EXTENDS", "IMPLEMENTS")]
    sources.append(("import statements", score.import_statements))
    for fact_type, counts in sources:
        if not counts.tp + counts.fp + counts.fn:
            continue
        capability = FACT_CAPABILITY[fact_type]
        level = declared.get(capability, "UNSUPPORTED")
        group = groups.setdefault(level, {"capabilities": set(), "counts": Counts()})
        group["capabilities"].add(capability)
        group["counts"].add(counts.tp, counts.fp, counts.fn)
    return {level: {"capabilities": sorted(g["capabilities"]), **g["counts"].as_dict()}
            for level, g in sorted(groups.items())}


def _language_result(score: LanguageScore, splits: dict[str, LanguageScore]) -> dict:
    categories = Counter(m.category for m in score.mismatches)
    gates = gates_for(score)
    resolution = {}
    for rtype in ("CALLS", "EXTENDS", "IMPLEMENTS", "IMPORTS"):
        resolution[rtype] = {scope: score.resolution[rtype][scope].as_dict()
                             for scope in ("same_file", "cross_file", "overall")}
    return {
        "cases": score.cases,
        "extractor_versions": dict(sorted(score.extractor_versions.items())),
        "entities": {k: score.entities[k].as_dict() for k in ("overall", *DECLARATION_TYPES)},
        "relationships": {k: score.relationships[k].as_dict() for k in ("overall", *RELATIONSHIP_TYPES)},
        "import_statements": score.import_statements.as_dict(),
        "resolution": resolution,
        "resolved_edge_precision": score.resolved_precision.as_dict(),
        "resolution_recall_by_expected_basis": {b: score.resolution_by_expected_basis[b].as_dict()
                                                for b in BASES if b in score.resolution_by_expected_basis},
        "basis_accuracy": score.basis_accuracy.as_dict(),
        "certainty_accuracy": score.certainty_accuracy.as_dict(),
        "by_origin": {"entities": {o: score.entities_by_origin[o].as_dict() for o in ORIGINS},
                      "relationships": {o: score.relationships_by_origin[o].as_dict() for o in ORIGINS},
                      "classification": score.origin_classification.as_dict()},
        "by_capability_level": _by_capability_level(score),
        "resolved_edge_precision_by_basis": {k: v.as_dict() for k, v in sorted(score.resolved_by_basis.items())},
        "unresolved_edges": {"overall": score.unresolved.as_dict(),
                             **{k: v.as_dict() for k, v in sorted(score.unresolved_by_kind.items())}},
        "evidence": {"path_validity": score.evidence_path.as_dict(),
                     "line_range_validity": score.evidence_lines.as_dict(),
                     "source_integrity": score.source_integrity.as_dict()},
        "decorators": score.decorators.as_dict(),
        "parse_status": score.parse_status.as_dict(),
        "syntax_error_location": score.syntax_error_location.as_dict(),
        "malformed_isolation": {"malformed_files": score.malformed_files, "isolated": score.malformed_isolated,
                                "rate": round(score.malformed_isolated / score.malformed_files, 4)
                                if score.malformed_files else 1.0,
                                "repository_failures": score.repository_failures},
        "determinism": {"identical_runs": score.determinism.as_dict(), "stable_keys": score.key_agreement.as_dict()},
        "unsupported_leakage": {"count": len(score.leakage), "facts": score.leakage},
        "capabilities": score.capabilities,
        "splits": {name: {"entities": s.entities["overall"].as_dict(),
                          "relationships": s.relationships["overall"].as_dict(),
                          "calls_resolution": s.resolution["CALLS"]["overall"].as_dict()}
                   for name, s in sorted(splits.items())},
        "gates": gates,
        "gates_passed": all(g["passed"] for g in gates),
        "errors": {"by_category": {c: categories.get(c, 0) for c in CATEGORIES},
                   "items": [m.as_dict() for m in sorted(score.mismatches, key=lambda m: m.id)]},
    }


# --- report ----------------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _recall(counts: dict) -> str:
    return _pct(counts["recall"]) if counts["tp"] + counts["fn"] else "n/a"


def _prf(d: dict) -> str:
    return f"{_pct(d['precision'])} | {_pct(d['recall'])} | {_pct(d['f1'])} | {d['tp']} | {d['fp']} | {d['fn']}"


def render_markdown(result: dict) -> str:
    lines = [
        f"# Structural extraction baseline v{result['baseline_version']} (SS-BE-202, SS-BE-203)",
        "",
        "Generated by `python -m backend.evaluation.extraction_metrics --markdown "
        "docs/evaluation/structural-extraction-baseline.md`; do not edit by hand. Machine-readable results: "
        "`backend/evaluation/results/structural-extraction-baseline.json`.",
        "",
        f"Gold version {result['gold_version']}; review status: {', '.join(result['review_status'])}. "
        "Gold facts are hand-labelled from fixture source (`backend/tests/fixtures/extraction/gold/`) and "
        "describe what the code does, including facts the extractor cannot produce yet.",
        "",
        "## Quality gates",
        "",
        "| Gate | Required | " + " | ".join(result["languages"]) + " |",
        "|---|---|" + "---|" * len(result["languages"]),
    ]
    gate_names = [g["gate"] for g in next(iter(result["languages"].values()))["gates"]]
    for index, name in enumerate(gate_names):
        required = next(iter(result["languages"].values()))["gates"][index]["required"]
        cells = []
        for lang in result["languages"].values():
            gate = lang["gates"][index]
            cells.append(f"{gate['actual']} {'pass' if gate['passed'] else '**FAIL**'}")
        lines.append(f"| {name} | {required} | " + " | ".join(cells) + " |")

    for language, data in result["languages"].items():
        lines += ["", f"## {language.capitalize()}", "",
                  f"Cases: {', '.join(data['cases'])}. Extractor: "
                  + ", ".join(f"`{v}`" for v in data["extractor_versions"].values()) + ".", "",
                  "### Declarations and relationships", "",
                  "| Fact | Precision | Recall | F1 | TP | FP | FN |", "|---|---|---|---|---|---|---|"]
        for kind, rows in (("entities", data["entities"]), ("relationships", data["relationships"])):
            for name, counts in rows.items():
                label = f"{kind} ({name})" if name == "overall" else name
                lines.append(f"| {label} | {_prf(counts)} |")
        lines.append(f"| import statements | {_prf(data['import_statements'])} |")
        lines += ["", "### Resolution", "",
                  "Recall of edges whose true target is a declaration in the repository. An unresolved "
                  "placeholder never counts as a correct internal link.", "",
                  "| Edge | Same-file recall | Cross-file recall | Overall recall | Labelled (same / cross) |",
                  "|---|---|---|---|---|"]
        for rtype, scopes in data["resolution"].items():
            same, cross, overall = scopes["same_file"], scopes["cross_file"], scopes["overall"]
            labelled = f"{same['tp'] + same['fn']} / {cross['tp'] + cross['fn']}"
            lines.append(f"| {rtype} | {_recall(same)} | {_recall(cross)} | {_recall(overall)} | {labelled} |")
        rp = data["resolved_edge_precision"]
        ue = data["unresolved_edges"]
        by_basis = ", ".join(f"{b} {_pct(c['precision'])} ({c['tp']}/{c['tp'] + c['fp']})"
                             for b, c in data["resolved_edge_precision_by_basis"].items())
        lines += ["", f"Emitted resolved edges: precision {_pct(rp['precision'])} ({rp['tp']} correct, {rp['fp']} wrong). "
                  f"By resolution basis: {by_basis}. Scope-based bases carry MEDIUM certainty; receiver-based "
                  "bases (declared_type, constructed_instance) carry LOW.",
                  f"Unresolved edges (external, unresolvable, ambiguous targets): precision "
                  f"{_pct(ue['overall']['precision'])}, recall {_pct(ue['overall']['recall'])}. Their false "
                  f"positives ({ue['overall']['fp']}) are placeholders emitted where the true target is a "
                  "declaration in the repository, i.e. the misses in the cross-file column above.", "",
                  "### Evidence, capabilities and robustness", "",
                  "| Check | Result |", "|---|---|"]
        ev = data["evidence"]
        rows = [
            ("Evidence path validity", ev["path_validity"]),
            ("Evidence line-range validity", ev["line_range_validity"]),
            ("Evidence re-opened from source (hash and line)", ev["source_integrity"]),
            ("Decorators / annotations", data["decorators"]),
            ("Parse status", data["parse_status"]),
            ("Syntax error reported on the labelled line", data["syntax_error_location"]),
        ]
        for label, ratio in rows:
            lines.append(f"| {label} | {ratio['valid']}/{ratio['total']} ({_pct(ratio['rate'])}) |")
        mi = data["malformed_isolation"]
        caps = data["capabilities"]
        lines += [
            f"| Malformed files isolated | {mi['isolated']}/{mi['malformed_files']} ({_pct(mi['rate'])}); "
            f"repository failures: {mi['repository_failures']} |",
            f"| Identical output across two runs | {data['determinism']['identical_runs']['valid']}/"
            f"{data['determinism']['identical_runs']['total']} cases |",
            f"| Unsupported-fact leakage | {data['unsupported_leakage']['count']} |",
            f"| Capability reporting accuracy | {_pct(caps['accuracy'])}"
            + (f" (mismatched: {', '.join(caps['mismatched'])})" if caps["mismatched"] else "") + " |",
            "", "Declared capabilities: " + ", ".join(f"{k} {v}" for k, v in caps["declared"].items()) + ".",
        ]
        if caps["consistency_issues"]:
            lines += [""] + [f"- {issue}" for issue in caps["consistency_issues"]]
        lines += ["", "Development vs holdout (holdout cases were labelled after the analyzer changes they measure):", "",
                  "| Split | Entities P / R | Relationships P / R | Call resolution recall |", "|---|---|---|---|"]
        for split, s in data["splits"].items():
            lines.append(f"| {split} | {_pct(s['entities']['precision'])} / {_pct(s['entities']['recall'])} | "
                         f"{_pct(s['relationships']['precision'])} / {_pct(s['relationships']['recall'])} | "
                         f"{_pct(s['calls_resolution']['recall'])} |")
        lines += ["", "### By resolution basis", "",
                  "Recall by the basis each internal edge should resolve through (hand-labelled), and precision by "
                  "the basis the analyzer reported. Import and inference bases need repository-level resolution.", "",
                  "| Basis | Labelled edges | Recall | Emitted precision |", "|---|---|---|---|"]
        emitted = data["resolved_edge_precision_by_basis"]
        for basis in BASES:
            labelled = data["resolution_recall_by_expected_basis"].get(basis)
            if not labelled and basis not in emitted:
                continue
            count = labelled["tp"] + labelled["fn"] if labelled else 0
            precision = _pct(emitted[basis]["precision"]) if basis in emitted else "n/a"
            lines.append(f"| {basis} | {count} | {_recall(labelled) if labelled else 'n/a'} | {precision} |")
        ba, ca = data["basis_accuracy"], data["certainty_accuracy"]
        lines += ["", f"Correctly resolved edges reporting the expected basis: {ba['valid']}/{ba['total']} "
                  f"({_pct(ba['rate'])}); expected certainty: {ca['valid']}/{ca['total']} ({_pct(ca['rate'])}).",
                  "", "### By source origin", "",
                  "| Origin | Entities P / R | Relationships P / R |", "|---|---|---|"]
        for origin in ORIGINS:
            e, r = data["by_origin"]["entities"][origin], data["by_origin"]["relationships"][origin]
            if e["tp"] + e["fp"] + e["fn"] + r["tp"] + r["fp"] + r["fn"] == 0:
                continue
            lines.append(f"| {origin} | {_pct(e['precision'])} / {_recall(e)} | {_pct(r['precision'])} / {_recall(r)} |")
        oc = data["by_origin"]["classification"]
        lines += ["", f"Files classified with the labelled origin: {oc['valid']}/{oc['total']} ({_pct(oc['rate'])}).",
                  "", "### By declared capability level", "",
                  "| Level | Capabilities | Precision | Recall | F1 |", "|---|---|---|---|---|"]
        for level, group in data["by_capability_level"].items():
            lines.append(f"| {level} | {', '.join(group['capabilities'])} | {_pct(group['precision'])} | "
                         f"{_recall(group)} | {_pct(group['f1'])} |")
        lines += ["", "### Error categories", "", "| Category | Count |", "|---|---|"]
        for category, count in data["errors"]["by_category"].items():
            lines.append(f"| {category} | {count} |")
        if data["errors"]["items"]:
            lines += ["", "<details><summary>Every mismatch</summary>", "", "| Category | Area | Outcome | Detail |",
                      "|---|---|---|---|"]
            for item in data["errors"]["items"]:
                detail = item["detail"].replace("|", "\\|")
                lines.append(f"| {item['category']} | {item['area']} | {item['outcome']} | `{detail}` |")
            lines += ["", "</details>"]
    lines += ["", "## Known limitations and follow-ups", ""] + _limitations(result)
    return "\n".join(lines) + "\n"


FOLLOW_UPS = {
    CROSS_FILE_MISSING: ("PROPOSED: repository-level symbol resolution",
                         "Link imports, calls, inheritance and interface edges across files after all files are "
                         "persisted. Required before graph retrieval relies on cross-file edges."),
    RECEIVER_RESOLUTION: ("DEFECT (proposed): receiver resolution",
                          "Same-file calls through receivers the analyzer cannot type yet: Python annotated "
                          "parameters, and inferred types in every language (`val s = roll()`, lambda "
                          "parameters such as Scala's `_`). Declared types are resolved for Java and Scala."),
    PARSER_LIMITATION: ("DEFECT (proposed): declarations lost to parser error recovery",
                        "Tree-sitter can absorb the declarations after a broken header into the error node; "
                        "consider re-parsing the remainder of the file after the error region."),
    QUERY_OMISSION: ("DEFECT (proposed): parser query gap", "A construct the analyzer does not query, or over-matches."),
    AMBIGUOUS_SYMBOL: ("DEFECT (proposed): type-based overload selection",
                       "Same-arity overloads need argument types to choose between them."),
    INCORRECT_STABLE_KEY: ("DEFECT (proposed): normalization",
                           "A fact emitted under the wrong key, name, basis, certainty or origin."),
    INCORRECT_EVIDENCE: ("DEFECT (proposed): evidence ranges", "A fact cites the wrong source span."),
    LABEL_DEFECT: ("FIXTURE (proposed): label correction", "A confirmed labelling error, recorded in gold/triage.yaml."),
    UNSUPPORTED_CAPABILITY: ("DEFECT: unsupported-fact leakage", "A fact emitted for an UNSUPPORTED capability."),
}


def _limitations(result: dict) -> list[str]:
    out = []
    for language, data in result["languages"].items():
        for rtype, scopes in data["resolution"].items():
            cross = scopes["cross_file"]
            if cross["tp"] + cross["fn"]:
                out.append(f"- **{language} {rtype}**: cross-file recall {_recall(cross)} "
                           f"({cross['tp']} of {cross['tp'] + cross['fn']} labelled cross-file edges); "
                           f"same-file recall {_recall(scopes['same_file'])}.")
        failing = [g["gate"] for g in data["gates"] if not g["passed"]]
        out.append(f"- **{language} quality gates**: " + ("all pass." if not failing else
                   f"FAILING {', '.join(failing)}; the story cannot close until each has a follow-up defect."))
        declared = data["capabilities"]["declared"]
        out.append(f"- **{language} not claimed as supported**: "
                   + ", ".join(f"{c} ({level})" for c, level in declared.items() if level != "SUPPORTED") + ".")
    out += ["", "### Follow-up defects", "",
            "Opened from the categorized errors (counts across all languages):", "",
            "| Follow-up | Errors | Scope |", "|---|---|---|"]
    totals = Counter()
    for data in result["languages"].values():
        totals.update({c: n for c, n in data["errors"]["by_category"].items() if n})
    for category, count in totals.most_common():
        title, scope = FOLLOW_UPS.get(category, (category, ""))
        out.append(f"| {title} | {category}: {count} | {scope} |")
    if any(status != "reviewed" for status in result["review_status"]):
        out += ["", "The gold manifests await an independent reviewer (acceptance criterion); until then these "
                "numbers are the labeller's own."]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", type=Path, help="write machine-readable results here")
    parser.add_argument("--markdown", type=Path, help="write the Markdown report here")
    args = parser.parse_args(argv)
    result = evaluate()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result), encoding="utf-8")
    if not (args.json or args.markdown):
        print(render_markdown(result))
    for language, data in result["languages"].items():
        failing = [g["gate"] for g in data["gates"] if not g["passed"]]
        print(f"{language}: {'all gates pass' if not failing else 'FAILING ' + ', '.join(failing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
