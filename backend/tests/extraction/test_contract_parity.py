"""SS-BE-202/203: every analyzed language passes identical canonical-contract tests."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from enum import Enum
from uuid import uuid4

import pytest

from backend.evaluation import extraction_metrics as metrics
from backend.services.extraction.analyzers import JavaAnalyzer, PythonAnalyzer, ScalaAnalyzer
from backend.services.extraction.builder import FileFactsBuilder
from backend.services.extraction.contracts import (
    AnalyzerCapabilities, CanonicalEntity, CanonicalRelationship, CancellationToken, Capability, CapabilityLevel,
    FileExtraction, ParseStatus, RELATIONSHIP_CAPABILITY, SourceEvidence,
)
from backend.services.extraction.registry import AnalyzerRegistry
from backend.services.extraction.store import build_batch_payload
from extraction_benchmark_support import LANGUAGES, requires_grammars

pytestmark = requires_grammars

# A multi-file fixture with a malformed file, per language.
FIXTURE = {"python": "python/shop_app", "java": "java/orders_service", "scala": "scala/broker"}
ANALYZER = {"python": PythonAnalyzer, "java": JavaAnalyzer, "scala": ScalaAnalyzer}
DUPLICATES = {
    "python": ("dup.py", b"def handler():\n    return 1\n\n\ndef handler():\n    return 2\n", "function", "handler"),
    "java": ("Dup.java", b"class Dup {\n  void go() {\n    new Runnable() { public void run() {} };\n"
                         b"    new Runnable() { public void run() {} };\n  }\n}\n", "method", "run"),
    "scala": ("Dup.scala", b"class Dup {\n  def go(): Unit = {\n    new Runnable { def run(): Unit = () }\n"
                           b"    new Runnable { def run(): Unit = () }\n  }\n}\n", "method", "run"),
}


def extract(language: str) -> list[FileExtraction]:
    files, failure = metrics.extract_fixture(metrics.FIXTURES_DIR / FIXTURE[language])
    assert failure is None, failure
    return files


def analyze(language: str, path: str, source: bytes, analyzer=None) -> FileExtraction:
    analyzer = analyzer or ANALYZER[language]()
    builder = FileFactsBuilder(path=path, language=language, source=source, commit_sha="0" * 40,
                               analyzer=analyzer.extractor, extractor=analyzer.extractor,
                               extractor_version=analyzer.extractor_version, capabilities=analyzer.capabilities)
    errors = analyzer.analyze(source, builder, CancellationToken())
    return builder.build(ParseStatus.PARTIAL if errors else ParseStatus.PARSED, errors)


def value_shape(value) -> str:
    if isinstance(value, Enum):
        return type(value).__name__
    if isinstance(value, (tuple, list)):
        return "sequence"
    return type(value).__name__


def record_shapes(files: list[FileExtraction]) -> dict[str, set]:
    shapes: dict[str, set] = {}
    for file in files:
        for record in (file, *file.entities, *file.relationships, *file.evidence):
            fields = shapes.setdefault(type(record).__name__, set())
            for f in dataclasses.fields(record):
                fields.add((f.name, value_shape(getattr(record, f.name))))
    return shapes


# --- identical model types -------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_emits_only_canonical_model_types(language):
    for file in extract(language):
        assert type(file) is FileExtraction
        assert all(type(e) is CanonicalEntity for e in file.entities)
        assert all(type(r) is CanonicalRelationship for r in file.relationships)
        assert all(type(e) is SourceEvidence for e in file.evidence)


@pytest.mark.parametrize("language", [language for language in LANGUAGES if language != "python"])
def test_every_language_fills_the_model_with_the_same_value_types(language):
    python, other = record_shapes(extract("python")), record_shapes(extract(language))
    assert python.keys() == other.keys()
    for record in python:
        # A field may be None in one language's sample and set in the other; types must otherwise agree.
        for name in {n for n, _ in python[record]} | {n for n, _ in other[record]}:
            types_p = {t for n, t in python[record] if n == name} - {"NoneType"}
            types_o = {t for n, t in other[record] if n == name} - {"NoneType"}
            assert not (types_p and types_o) or types_p == types_o, (record, name, types_p, types_o)


# --- determinism -----------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_two_runs_produce_identical_canonical_output(language):
    first, second = extract(language), extract(language)
    assert build_batch_payload(first) == build_batch_payload(second)
    assert [e.stable_key for f in first for e in f.entities] == [e.stable_key for f in second for e in f.entities]


@pytest.mark.parametrize("language", LANGUAGES)
def test_duplicate_suffixes_follow_source_order(language):
    path, source, prefix, name = DUPLICATES[language]
    runs = [analyze(language, path, source) for _ in range(2)]
    assert [e.stable_key for e in runs[0].entities] == [e.stable_key for e in runs[1].entities]
    duplicates = sorted((e for e in runs[0].entities if e.stable_key.startswith(prefix) and e.name == name),
                        key=lambda e: e.start_line)
    assert len(duplicates) == 2
    assert not duplicates[0].stable_key.endswith("#2") and duplicates[1].stable_key.endswith("#2")


# --- no parser objects, valid evidence -------------------------------------------------------


def assert_plain(value, where):
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            assert_plain(item, where)
        return
    if isinstance(value, dict) or hasattr(value, "items"):
        for key, item in value.items():
            assert isinstance(key, str), where
            assert_plain(item, where)
        return
    if dataclasses.is_dataclass(value):
        for f in dataclasses.fields(value):
            assert_plain(getattr(value, f.name), f"{where}.{f.name}")
        return
    raise AssertionError(f"{where} holds {type(value).__module__}.{type(value).__name__}")


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_tree_sitter_objects_enter_canonical_output(language):
    files = extract(language)
    for file in files:
        for record in (*file.entities, *file.relationships, *file.evidence):
            assert_plain(record, type(record).__name__)
            assert "tree_sitter" not in repr(record)
        assert_plain(dict(file.metadata), "file.metadata")
    json.dumps(build_batch_payload(files))


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_entity_and_relationship_opens_valid_evidence(language):
    root = metrics.FIXTURES_DIR / FIXTURE[language]
    for file in extract(language):
        source = root.joinpath(*file.path.split("/")).read_bytes()
        evidence = {e.key: e for e in file.evidence}
        cited = [k for e in file.entities for k, _ in e.evidence] + [k for r in file.relationships for k in r.evidence]
        assert file.entities and cited
        for key in cited:
            item = evidence[tuple(key)]
            assert item.path == file.path
            assert 1 <= item.start_line <= item.end_line <= file.line_count
            assert hashlib.sha256(source[item.start_byte:item.end_byte]).hexdigest() == item.content_hash
            assert source[:item.start_byte].count(b"\n") + 1 == item.start_line


# --- capabilities ----------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_unsupported_capabilities_emit_no_facts(language):
    for file in extract(language):
        for relationship in file.relationships:
            capability = RELATIONSHIP_CAPABILITY[relationship.relationship_type]
            assert file.capabilities.allows(capability), relationship.identity


@pytest.mark.parametrize("language", LANGUAGES)
def test_downgrading_a_capability_blocks_its_facts(language):
    base = ANALYZER[language]

    class WithoutCalls(base):
        capabilities = AnalyzerCapabilities({**base.capabilities.levels, Capability.CALLS: CapabilityLevel.UNSUPPORTED})

    registry = AnalyzerRegistry([WithoutCalls()])
    files, failure = metrics.extract_fixture(metrics.FIXTURES_DIR / FIXTURE[language], registry)
    assert all(r.relationship_type.value != "CALLS" for f in files for r in f.relationships)
    analyzed = [f for f in files if f.language == language]
    # Files that contain calls are rejected whole rather than silently trimmed ...
    assert any(f.parse_status is ParseStatus.FAILED and f.errors[0].code == "contract_violation" for f in analyzed)
    if all(f.parse_status is ParseStatus.FAILED for f in analyzed):
        # ... and when that rejects every file, the repository fails instead of completing empty.
        assert failure and "every file" in failure
    else:
        assert failure is None


# --- overloads, companions, malformed isolation ------------------------------------------------


def test_java_overloads_remain_distinguishable():
    keys = [e.stable_key for f in extract("java") for e in f.entities if e.name == "add"]
    assert len(keys) == len(set(keys)) == 3
    assert {k.rsplit(".", 1)[-1] for k in keys} == {"add(LineItem)", "add(String,int)", "add(String,long)"}


def test_scala_companion_objects_and_their_classes_stay_distinct():
    keys = {e.stable_key for f in extract("scala") for e in f.entities}
    utils = "core/src/main/scala/kafka/utils/CoreUtils.scala"
    assert {f"interface:{utils}::kafka.utils.Logging", f"class:{utils}::kafka.utils.Logging$"} <= keys
    # The trait and its companion both declare warn(String): two different methods.
    assert {f"method:{utils}::kafka.utils.Logging.warn(String)",
            f"method:{utils}::kafka.utils.Logging$.warn(String)"} <= keys


@pytest.mark.parametrize("language", LANGUAGES)
def test_one_malformed_file_does_not_fail_the_repository(language):
    case = next(c for c in metrics.load_gold() if c.fixture == FIXTURE[language])
    files = {f.path: f for f in extract(language)}
    assert set(files) == set(case.files)
    malformed = [p for p, f in case.files.items() if f.parse_status is ParseStatus.PARTIAL]
    assert malformed
    for path, expected in case.files.items():
        assert files[path].parse_status is expected.parse_status, path


# --- persistence -----------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_batch_payload_has_no_duplicate_natural_keys(language):
    payload = build_batch_payload(extract(language))
    identities = {
        "source_files": lambda r: r["path"],
        "evidence": lambda r: (r["path"], r["start_line"], r["end_line"], r["content_hash"], r["extractor_version"]),
        "entities": lambda r: r["stable_key"],
        "edges": lambda r: (r["source_key"], r["relationship_type"], r["target_key"], r["extractor_version"]),
    }
    for table, identity in identities.items():
        keys = [identity(row) for row in payload[table]]
        assert len(keys) == len(set(keys)), table


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("language", LANGUAGES)
def test_repeat_persistence_creates_no_duplicate_records(language):
    from backend.services.analysis_persistence import create_or_reuse_analysis
    from backend.services.extraction.store import write_batch
    from backend.services.postgres import make_connection_factory

    conn = make_connection_factory(os.environ["TEST_DATABASE_URL"])()
    try:
        name = f"parity-{uuid4().hex[:8]}"
        analysis_id = create_or_reuse_analysis(
            conn, canonical_repository_key=f"github:test-owner/{name}", owner_name="test-owner",
            repository_name=name, clone_url=f"https://github.com/test-owner/{name}.git", requested_reference="main",
            resolved_commit_sha="0" * 40, structural_pipeline_version="structural-test",
        ).analysis_id
        tables = ("source_file", "entity", "evidence", "repository_edge")

        def counts():
            return {t: conn.fetch_scalar(f"SELECT count(*) FROM core.{t} WHERE analysis_id = %s", str(analysis_id))
                    for t in tables}

        write_batch(conn, analysis_id, build_batch_payload(extract(language)))
        first = counts()
        write_batch(conn, analysis_id, build_batch_payload(extract(language)))
        assert counts() == first and first["entity"] > 0
    finally:
        conn.rollback()
        conn.close()


# --- receivers never link by name --------------------------------------------------------------

# Each file declares a method named like the calls made on receivers of unknown or external type.
RECEIVERS = {
    "python": ("svc.py", b"""class Repo:
    def save(self):
        return 1

    def via_parameter(self, other):
        other.save()

    def via_attribute(self):
        self.repo.save()

    def via_self(self):
        self.save()

    def via_name(self):
        save()

    def via_instance(self):
        Repo().save()


def save():
    return 2
""", {
        "other.save": None, "self.repo.save": None,
        "self.save": ("method:svc.py::Repo.save", "enclosing_class", "MEDIUM"),
        "save": ("function:svc.py::save", "lexical_scope", "MEDIUM"),
        "Repo().save": ("method:svc.py::Repo.save", "constructed_instance", "LOW"),
    }),
    "java": ("Repo.java", b"""import java.util.List;
class Repo {
    private final List<Integer> list = null;
    void add(int x) {}
    void viaExternalField() { list.add(1); }
    void viaVarLocal() { var local = new java.util.ArrayList<Integer>(); local.add(2); }
    void viaLambda() { java.util.stream.Stream.of(1).forEach(x -> x.add(3)); }
    void viaUnqualified() { add(4); }
    void viaThis() { this.add(5); }
    void viaTypedParameter(Repo other) { other.add(6); }
}
""", {
        "list.add": None, "local.add": None, "x.add": None,
        "add": ("method:Repo.java::Repo.add(int)", "enclosing_class", "MEDIUM"),
        "this.add": ("method:Repo.java::Repo.add(int)", "enclosing_class", "MEDIUM"),
        "other.add": ("method:Repo.java::Repo.add(int)", "declared_type", "LOW"),
    }),
    "scala": ("Repo.scala", b"""import java.util.List
class Repo {
  private val list: List[Integer] = null
  def add(x: Int): Unit = {}
  def viaExternalField(): Unit = {
    list.add(1)
  }
  def viaInferredLocal(): Unit = {
    val local = new java.util.ArrayList[Integer]()
    local.add(2)
  }
  def viaLambda(): Unit = {
    Seq(1).foreach(x => x.add(3))
  }
  def viaUnqualified(): Unit = {
    add(4)
  }
  def viaThis(): Unit = {
    this.add(5)
  }
  def viaTypedParameter(other: Repo): Unit = {
    other.add(6)
  }
}
""", {
        "list.add": None, "local.add": None, "x.add": None,
        "add": ("method:Repo.scala::Repo.add(Int)", "enclosing_class", "MEDIUM"),
        "this.add": ("method:Repo.scala::Repo.add(Int)", "enclosing_class", "MEDIUM"),
        "other.add": ("method:Repo.scala::Repo.add(Int)", "declared_type", "LOW"),
    }),
}


def call_line(source: bytes, callee: str) -> int:
    """The line calling exactly ``callee`` (so `save` does not match `other.save`), skipping declarations."""
    pattern = re.compile(rf"(?<![\w.]){re.escape(callee)}\(")
    for number, line in enumerate(source.decode().splitlines(), 1):
        declaration = line.strip().startswith(("def ", "void ")) and "{ " not in line
        if pattern.search(line.split("{", 1)[-1] if "{" in line else line) and not declaration:
            return number
    raise AssertionError(callee)


@pytest.mark.parametrize("language", LANGUAGES)
def test_calls_on_receivers_of_unknown_type_never_link_by_name(language):
    path, source, expectations = RECEIVERS[language]
    file = analyze(language, path, source)
    evidence = {e.key: e for e in file.evidence}
    calls = {}
    for relationship in file.relationships:
        if relationship.relationship_type.value == "CALLS":
            for key in relationship.evidence:
                calls.setdefault(evidence[tuple(key)].start_line, []).append(relationship)

    for callee, expected in expectations.items():
        at_line = calls[call_line(source, callee)]
        if expected is None:
            assert all(dict(r.metadata)["resolution"] == "unresolved" and r.target_key.startswith("external:")
                       for r in at_line), (callee, [r.target_key for r in at_line])
            assert all(r.certainty.value == "LOW" for r in at_line)
        else:
            target, basis, certainty = expected
            linked = [r for r in at_line if r.target_key == target]
            assert linked, (callee, [r.target_key for r in at_line])
            assert dict(linked[0].metadata)["resolution_basis"] == basis
            assert linked[0].certainty.value == certainty


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_resolved_edge_records_its_basis(language):
    for file in extract(language):
        for relationship in file.relationships:
            metadata = dict(relationship.metadata)
            if metadata.get("resolution") == "same_file":
                assert metadata.get("resolution_basis"), relationship.identity


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_edge_is_as_certain_as_its_strongest_site_regardless_of_order(language):
    path, source = {
        "python": ("a.py", b"class A:\n    def m(self):\n        return 1\n\n"
                           b"    def run(self):\n        A().m()\n        self.m()\n"),
        "java": ("A.java", b"class A {\n  void m() {}\n  void run(A other) {\n    other.m();\n    m();\n  }\n}\n"),
        "scala": ("A.scala", b"class A {\n  def m(): Unit = {}\n  def run(other: A): Unit = {\n    other.m()\n"
                             b"    m()\n  }\n}\n"),
    }[language]
    edge = next(r for r in analyze(language, path, source).relationships
                if r.relationship_type.value == "CALLS" and "run" in r.source_key and not r.target_key.startswith("ext"))
    assert edge.certainty.value == "MEDIUM"  # the LOW receiver site comes first in the source
    assert len(edge.evidence) == 2
    assert dict(edge.metadata)["resolution_basis"] == "enclosing_class"
    assert set(dict(edge.metadata)["resolution_bases"]) == {"enclosing_class",
                                                           "constructed_instance" if language == "python" else "declared_type"}
