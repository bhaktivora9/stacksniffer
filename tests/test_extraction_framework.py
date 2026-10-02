"""Analyzer framework: selection, canonical contract, determinism, isolation, cancellation."""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from backend.services.extraction.analyzers import FileLevelAnalyzer, JavaAnalyzer, PythonAnalyzer
from backend.services.extraction.builder import FileFactsBuilder, Span
from backend.services.extraction.contracts import (
    AnalyzerCapabilities,
    CanonicalContractError,
    CanonicalEntity,
    CancellationToken,
    Capability,
    CapabilityLevel,
    Certainty,
    EntityType,
    EvidenceRole,
    ExtractionCancelled,
    ExtractionContext,
    ParseStatus,
    RelationshipType,
    SourceEvidence,
)
from backend.services.extraction.extractor import ExtractionSystemicFailure, RepositoryExtractor
from backend.services.extraction.registry import AnalyzerRegistry, detect_language
from backend.services.analysis_lifecycle import AWAITING_STAGE
from backend.services.extraction.stage import StructuralExtractionStage
from backend.services.extraction.store import build_batch_payload

pytestmark = pytest.mark.skipif(not PythonAnalyzer().available or not JavaAnalyzer().available,
                                reason="tree-sitter grammars are not installed")

SHA = "c" * 40

PYTHON = b"""import os.path as osp
from .models import User

class Base:
    pass

class Service(Base, external.Mixin):
    @cached
    def run(self):
        self.helper()
        build()
        osp.join("a", "b")

    def helper(self):
        return 1

def build():
    return Service()
"""

JAVA = b"""package com.acme;
import java.util.List;

@Component
public class Repo extends Base implements Store {
    public void save(int id) { validate(id); list.add(id); }
    private void validate(int id) {}
    private void overloaded(int a) {}
    private void overloaded(String a) {}
    void callOverloaded() { overloaded(1); }
}
class Base {}
"""


class RecordingStore:
    def __init__(self, on_persist=None):
        self.batches = []
        self.on_persist = on_persist

    def persist(self, files):
        self.batches.append(list(files))
        if self.on_persist:
            self.on_persist(len(self.batches))


def snapshot(tmp_path, files):
    root = tmp_path / "source"
    for path, content in files.items():
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    root.mkdir(exist_ok=True)
    return ExtractionContext(uuid4(), uuid4(), root, SHA, CancellationToken(), tuple(sorted(files)))


def extract(context, registry=None, store=None, batch_size=100):
    store = store or RecordingStore()
    result = RepositoryExtractor(registry or AnalyzerRegistry.default(), batch_size=batch_size).extract(context, store)
    files = {f.path: f for batch in store.batches for f in batch}
    return result, files, store


# --- selection -------------------------------------------------------------------------------


def test_language_detection_uses_path_and_extension():
    assert detect_language("src/app.py") == "python"
    assert detect_language("src/Main.java") == "java"
    assert detect_language("web/index.TSX") == "typescript"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language(".gitignore") is None
    assert detect_language("LICENSE") is None


def test_registry_selects_the_analyzer_for_each_language():
    registry = AnalyzerRegistry.default()
    assert registry.select("a.py").analyzer.language == "python"
    assert registry.select("A.java").analyzer.language == "java"
    assert registry.select("Main.scala").analyzer.language == "scala"
    assert registry.select("main.go").analyzer.language == "go"
    rust = registry.select("main.rs")
    assert (rust.language, rust.analyzer, rust.fallback_reason) == ("rust", registry.fallback, "no_analyzer")
    assert registry.select("NOTES").fallback_reason == "unknown_language"


def test_missing_grammar_falls_back_to_file_level():
    class MissingGrammar:
        language, available = "python", False

    registry = AnalyzerRegistry([MissingGrammar()])
    selection = registry.select("a.py")
    assert selection.analyzer is registry.fallback
    assert selection.fallback_reason == "grammar_unavailable"


def test_unsupported_languages_get_file_level_analysis_only(tmp_path):
    result, files, _ = extract(snapshot(tmp_path, {"main.rs": b"fn main() {}\n"}))

    rust = files["main.rs"]
    assert rust.parse_status is ParseStatus.UNSUPPORTED
    assert [e.entity_type for e in rust.entities] == [EntityType.FILE]
    assert rust.relationships == ()
    assert all(level is CapabilityLevel.UNSUPPORTED for level in rust.capabilities.levels.values())
    assert result.languages["rust"]["fallback_reason"] == "no_analyzer"


# --- canonical contract ----------------------------------------------------------------------


def test_every_adapter_produces_the_same_validated_contract(tmp_path):
    _, files, _ = extract(snapshot(tmp_path, {"svc.py": PYTHON, "Repo.java": JAVA, "run.sh": b"echo hi\n"}))

    for file in files.values():
        assert isinstance(file.parse_status, ParseStatus)
        assert set(file.capabilities.levels) == set(Capability)
        assert all(isinstance(e, CanonicalEntity) for e in file.entities)
        # The payload that reaches the database is plain JSON, whatever the language.
        json.dumps(build_batch_payload([file]))
    python_types = {e.entity_type for e in files["svc.py"].entities}
    java_types = {e.entity_type for e in files["Repo.java"].entities}
    assert {EntityType.FILE, EntityType.CLASS, EntityType.METHOD, EntityType.FUNCTION} <= python_types
    assert {EntityType.FILE, EntityType.CLASS, EntityType.METHOD} <= java_types


def test_capabilities_must_be_declared_explicitly():
    with pytest.raises(CanonicalContractError, match="missing"):
        AnalyzerCapabilities({Capability.DECLARATIONS: CapabilityLevel.SUPPORTED})


def test_parser_objects_cannot_enter_canonical_records():
    import tree_sitter_python
    from tree_sitter import Language, Parser

    node = Parser(Language(tree_sitter_python.language())).parse(b"x = 1\n").root_node
    key = ("a.py", 1, 1, "h", "v1")
    with pytest.raises(CanonicalContractError, match="not plain data"):
        CanonicalEntity("k", EntityType.CLASS, "A", None, "a.py", None, 1, 1, "x", "v1",
                        ((key, EvidenceRole.DEFINITION),), metadata={"node": node})
    with pytest.raises(CanonicalContractError):
        CanonicalEntity("k", EntityType.CLASS, node, None, "a.py", None, 1, 1, "x", "v1",
                        ((key, EvidenceRole.DEFINITION),))
    with pytest.raises(CanonicalContractError):
        SourceEvidence("a.py", node, 1, None, None, "h", SHA, "x", "v1")


def test_entities_and_relationships_require_evidence():
    with pytest.raises(CanonicalContractError, match="no evidence"):
        CanonicalEntity("k", EntityType.CLASS, "A", None, "a.py", None, 1, 1, "x", "v1", ())


def builder_for(analyzer, source=b"x = 1\n", path="a.py"):
    return FileFactsBuilder(path=path, language="python", source=source, commit_sha=SHA, analyzer=analyzer.extractor,
                            extractor=analyzer.extractor, extractor_version=analyzer.extractor_version,
                            capabilities=analyzer.capabilities)


def test_unsupported_capabilities_cannot_be_claimed():
    builder = builder_for(PythonAnalyzer())  # interfaces are UNSUPPORTED for Python
    builder.entity(EntityType.INTERFACE, "Proto", Span(0, 5, 1, 1))
    with pytest.raises(CanonicalContractError, match="INTERFACE"):
        builder.build(ParseStatus.PARSED)

    builder = builder_for(FileLevelAnalyzer())
    target = builder.external("call", "print", Span(0, 5, 1, 1))
    builder.relationship(RelationshipType.CALLS, builder.file_key, target, Span(0, 5, 1, 1), certainty=Certainty.LOW)
    with pytest.raises(CanonicalContractError, match="CALLS"):
        builder.build(ParseStatus.PARSED)


def test_an_analyzer_that_claims_unsupported_facts_has_its_file_rejected(tmp_path):
    class OverclaimingAnalyzer(PythonAnalyzer):
        capabilities = AnalyzerCapabilities({**PythonAnalyzer.capabilities.levels,
                                             Capability.CALLS: CapabilityLevel.UNSUPPORTED})

    # constants.py makes no calls, so the repository survives the rejection of svc.py.
    result, files, _ = extract(snapshot(tmp_path, {"svc.py": PYTHON, "constants.py": b"LIMIT = 10\n"}),
                               AnalyzerRegistry([OverclaimingAnalyzer()]))

    rejected = files["svc.py"]
    assert rejected.parse_status is ParseStatus.FAILED
    assert rejected.errors[0].code == "contract_violation"
    assert [e.entity_type for e in rejected.entities] == [EntityType.FILE]  # nothing half-built survives


def test_capability_coverage_is_reported_per_language_and_repository(tmp_path):
    result, _, _ = extract(snapshot(tmp_path, {"a.py": PYTHON, "B.java": JAVA, "c.rs": b"fn c() {}\n"}))

    assert result.languages["python"]["capabilities"]["interfaces"] == "UNSUPPORTED"
    assert result.languages["python"]["capabilities"]["calls"] == "PARTIAL"
    assert result.languages["java"]["capabilities"]["interfaces"] == "PARTIAL"
    assert result.languages["rust"]["capabilities"]["declarations"] == "UNSUPPORTED"
    assert result.capability_coverage["dependencies"] == {"UNSUPPORTED": 3}
    assert result.capability_coverage["declarations"] == {"SUPPORTED": 2, "UNSUPPORTED": 1}
    json.dumps(result.as_metrics())


# --- determinism and evidence ----------------------------------------------------------------


def test_stable_keys_and_payload_are_deterministic_across_runs(tmp_path):
    files = {"svc.py": PYTHON, "Repo.java": JAVA, "dup.py": b"def f():\n  pass\ndef f():\n  pass\n"}
    first = extract(snapshot(tmp_path / "one", files))[1]
    second = extract(snapshot(tmp_path / "two", files))[1]

    assert build_batch_payload(list(first.values())) == build_batch_payload(list(second.values()))
    keys = [e.stable_key for e in first["dup.py"].entities]
    assert "function:dup.py::f" in keys and "function:dup.py::f#2" in keys


def test_evidence_ranges_are_valid_and_hash_the_exact_source(tmp_path):
    sources = {"svc.py": PYTHON, "Repo.java": JAVA}
    _, files, _ = extract(snapshot(tmp_path, sources))

    for path, file in files.items():
        source = sources[path]
        lines = source.decode().splitlines()
        by_key = {e.key: e for e in file.evidence}
        for evidence in file.evidence:
            assert 1 <= evidence.start_line <= evidence.end_line <= len(lines)
            span = source[evidence.start_byte:evidence.end_byte]
            assert hashlib.sha256(span).hexdigest() == evidence.content_hash
            assert evidence.commit_sha == SHA and evidence.extractor_version
            # Byte range and line range describe the same span.
            assert source[:evidence.start_byte].count(b"\n") + 1 == evidence.start_line
        for entity in file.entities:
            assert entity.evidence, entity.stable_key
            if entity.start_line is not None:
                definition = by_key[entity.evidence[0][0]]
                assert (entity.start_line, entity.end_line) == (definition.start_line, definition.end_line)
        for relationship in file.relationships:
            assert relationship.evidence and all(key in by_key for key in relationship.evidence)


def test_class_and_method_ranges_cover_their_bodies(tmp_path):
    _, files, _ = extract(snapshot(tmp_path, {"svc.py": PYTHON}))
    entities = {e.qualified_name: e for e in files["svc.py"].entities}
    assert (entities["Service"].start_line, entities["Service"].end_line) == (7, 15)
    # Ranges include decorators (line 8 is `@cached`), matching Java annotations.
    assert (entities["Service.run"].start_line, entities["Service.run"].end_line) == (8, 12)
    assert dict(entities["Service.run"].metadata)["decorators"] == ["cached"]


# --- resolution ------------------------------------------------------------------------------


def relationships(file, relationship_type):
    return {(r.source_key, r.target_key): r for r in file.relationships if r.relationship_type is relationship_type}


def test_unresolved_external_targets_stay_unresolved(tmp_path):
    _, files, _ = extract(snapshot(tmp_path, {"svc.py": PYTHON, "Repo.java": JAVA}))
    python, java = files["svc.py"], files["Repo.java"]
    entities = {e.stable_key: e for e in python.entities}

    imports = relationships(python, RelationshipType.IMPORTS)
    assert ("file:svc.py", "external:python:module:os.path") in imports
    assert ("file:svc.py", "external:python:module:.models") in imports  # relative import not guessed
    external = entities["external:python:type:external.Mixin"]
    assert external.entity_type is EntityType.EXTERNAL_SYMBOL
    assert dict(external.metadata)["resolution"] == "unresolved"
    assert external.path is None

    calls = relationships(python, RelationshipType.CALLS)
    assert calls[("method:svc.py::Service.run", "method:svc.py::Service.helper")].certainty is Certainty.MEDIUM
    assert calls[("method:svc.py::Service.run", "external:python:call:osp.join")].certainty is Certainty.LOW

    java_calls = relationships(java, RelationshipType.CALLS)
    ambiguous = java_calls[("method:Repo.java::com.acme.Repo.callOverloaded()", "external:java:call:overloaded")]
    assert dict(ambiguous.metadata) == {"resolution": "unresolved", "ambiguous_candidates": 2}
    implements = relationships(java, RelationshipType.IMPLEMENTS)
    assert ("class:Repo.java::com.acme.Repo", "external:java:type:Store") in implements
    assert relationships(java, RelationshipType.EXTENDS)[
        ("class:Repo.java::com.acme.Repo", "class:Repo.java::com.acme.Base")].certainty is Certainty.MEDIUM


# --- failure isolation -----------------------------------------------------------------------


def test_one_malformed_file_does_not_fail_the_repository(tmp_path):
    result, files, _ = extract(snapshot(tmp_path, {
        "good.py": PYTHON,
        "broken.py": b"def ok():\n    pass\n\ndef broken(:\n    x =\n",
        "Broken.java": b"class A { void f( { }\n",
    }))

    assert files["good.py"].parse_status is ParseStatus.PARSED
    broken = files["broken.py"]
    assert broken.parse_status is ParseStatus.PARTIAL
    assert broken.errors and broken.errors[0].start_line is not None
    assert "function:broken.py::ok" in {e.stable_key for e in broken.entities}  # valid code still extracted
    assert files["Broken.java"].parse_status is ParseStatus.PARTIAL
    assert result.files_by_status == {"PARSED": 1, "PARTIAL": 2}
    assert result.errors >= 2


def test_an_analyzer_crash_is_isolated_to_its_file(tmp_path):
    class CrashesOnBoom(PythonAnalyzer):
        def analyze(self, source, builder, token):
            if b"boom" in source:
                raise RecursionError("analyzer bug")
            return super().analyze(source, builder, token)

    result, files, _ = extract(snapshot(tmp_path, {"a.py": PYTHON, "boom.py": b"boom = 1\n"}),
                               AnalyzerRegistry([CrashesOnBoom()]))

    assert files["a.py"].parse_status is ParseStatus.PARSED
    assert files["boom.py"].parse_status is ParseStatus.FAILED
    assert files["boom.py"].errors[0].code == "analyzer_error"
    assert result.files_by_status == {"PARSED": 1, "FAILED": 1}


class CrashesOnMarker(PythonAnalyzer):
    def analyze(self, source, builder, token):
        if b"CRASH" in source:
            raise RuntimeError("analyzer bug")
        return super().analyze(source, builder, token)


def test_a_small_repository_where_every_file_crashes_fails(tmp_path):
    files = {f"m{i}.py": b"CRASH = 1\n" for i in range(6)}
    with pytest.raises(ExtractionSystemicFailure, match="every file: 6 of 6"):
        extract(snapshot(tmp_path, files), AnalyzerRegistry([CrashesOnMarker()]))


def test_a_high_crash_rate_fails_larger_repositories(tmp_path):
    files = {f"ok{i}.py": b"x = 1\n" for i in range(15)} | {f"bad{i}.py": b"CRASH = 1\n" for i in range(6)}
    with pytest.raises(ExtractionSystemicFailure, match="crash rate 29% exceeds 25%"):
        extract(snapshot(tmp_path, files), AnalyzerRegistry([CrashesOnMarker()]))


def test_isolated_crashes_below_the_rate_do_not_fail_the_repository(tmp_path):
    files = {f"ok{i}.py": b"x = 1\n" for i in range(18)} | {f"bad{i}.py": b"CRASH = 1\n" for i in range(2)}
    result, _, _ = extract(snapshot(tmp_path, files), AnalyzerRegistry([CrashesOnMarker()]))
    assert result.files_by_status == {"PARSED": 18, "FAILED": 2}


def test_vendored_and_generated_crashes_never_count_toward_the_threshold(tmp_path):
    files = ({"app.py": b"x = 1\n"} | {f"vendor/lib{i}.py": b"CRASH = 1\n" for i in range(30)}
             | {f"api/gen{i}_pb2.py": b"CRASH = 1\n" for i in range(30)})
    result, extracted, _ = extract(snapshot(tmp_path, files), AnalyzerRegistry([CrashesOnMarker()]))
    assert result.files_by_status["FAILED"] == 60
    assert result.files_by_origin == {"first_party": 1, "generated": 30, "vendored": 30}
    assert extracted["vendor/lib0.py"].is_vendored and extracted["api/gen0_pb2.py"].is_generated


def test_missing_snapshot_is_a_systemic_failure(tmp_path):
    context = ExtractionContext(uuid4(), uuid4(), tmp_path / "gone", SHA, CancellationToken(), ("a.py",))
    with pytest.raises(ExtractionSystemicFailure):
        extract(context)


def test_repository_code_is_never_executed(tmp_path):
    sentinel = tmp_path / "executed"
    payload = f"open({str(sentinel)!r}, 'w').write('ran')\n".encode()
    extract(snapshot(tmp_path, {"setup.py": payload, "conftest.py": payload, "__init__.py": payload}))
    assert not sentinel.exists()


# --- cancellation ----------------------------------------------------------------------------


def test_cancellation_before_a_file_writes_nothing(tmp_path):
    context = snapshot(tmp_path, {"a.py": PYTHON})
    context.cancellation_token.cancel("ownership_lost")
    store = RecordingStore()
    with pytest.raises(ExtractionCancelled):
        extract(context, store=store)
    assert store.batches == []


def test_cancellation_during_extraction_keeps_committed_batches_and_writes_no_more(tmp_path):
    context = snapshot(tmp_path, {f"m{i}.py": PYTHON for i in range(6)})
    store = RecordingStore(on_persist=lambda n: context.cancellation_token.cancel("ownership_lost"))

    with pytest.raises(ExtractionCancelled):
        extract(context, store=store, batch_size=2)
    assert [[f.path for f in batch] for batch in store.batches] == [["m0.py", "m1.py"]]


def test_cancellation_before_persistence_drops_the_pending_batch(tmp_path):
    class CancelsWhileAnalyzing(PythonAnalyzer):
        def analyze(self, source, builder, token):
            errors = super().analyze(source, builder, token)
            token.cancel("ownership_lost")  # after the last file of the batch, before its write
            return errors

    context = snapshot(tmp_path, {"only.py": PYTHON})
    store = RecordingStore()
    with pytest.raises(ExtractionCancelled):
        extract(context, AnalyzerRegistry([CancelsWhileAnalyzing()]), store=store)
    assert store.batches == []


def test_cancellation_between_parser_operations(tmp_path):
    analyzer = PythonAnalyzer()
    real_parse = analyzer.adapter.parse
    token = CancellationToken()

    def parse_then_cancel(source):
        tree = real_parse(source)
        token.cancel("ownership_lost")
        return tree

    analyzer.adapter.parse = parse_then_cancel
    with pytest.raises(ExtractionCancelled):
        analyzer.analyze(PYTHON, builder_for(analyzer, PYTHON), token)


def test_cancellation_is_not_recorded_as_a_file_failure(tmp_path):
    class CancelsMidFile(PythonAnalyzer):
        def analyze(self, source, builder, token):
            token.cancel("ownership_lost")
            token.raise_if_cancelled()

    with pytest.raises(ExtractionCancelled):
        extract(snapshot(tmp_path, {"a.py": PYTHON}), AnalyzerRegistry([CancelsMidFile()]))


# --- stage -----------------------------------------------------------------------------------


class Result:
    rowcount = 1


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, query, *args):
        self.statements.append((" ".join(query.split()), args))
        return Result()

    def fetch_all(self, query, *args):  # source retention finds no recorded files
        self.statements.append((" ".join(query.split()), args))
        return []

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_stage_records_metrics_advances_to_indexing_and_hands_off_the_result(tmp_path):
    conn, store, handed = FakeConnection(), RecordingStore(), []

    async def next_stage(result):
        handed.append(result)
        return "SUCCEEDED"

    stage = StructuralExtractionStage(lambda: conn, next_stage=next_stage, store_factory=lambda analysis_id, attempt_id: store)
    context = snapshot(tmp_path, {"svc.py": PYTHON})

    assert asyncio.run(stage(context)) == "SUCCEEDED"
    assert handed[0].files_total == 1 and handed[0].attempt_id == context.attempt_id
    metrics = next(args for query, args in conn.statements if "'extractions'" in query)
    assert metrics[0] == str(context.attempt_id)
    assert str(context.checkout_path) not in metrics[1]  # the temporary path is never persisted
    transition = next(args for query, args in conn.statements if query.startswith("UPDATE core.analysis SET status"))
    assert (transition[0], transition[3]) == ("INDEXING", "EXTRACTING")


def test_stage_without_a_next_stage_parks_the_analysis_instead_of_failing(tmp_path):
    store = RecordingStore()
    stage = StructuralExtractionStage(lambda: FakeConnection(), store_factory=lambda analysis_id, attempt_id: store)
    assert asyncio.run(stage(snapshot(tmp_path, {"svc.py": PYTHON}))) == AWAITING_STAGE
    assert len(store.batches) == 1
