"""The accuracy checks every language must pass; test_<language>_accuracy binds a language."""

from __future__ import annotations

from backend.evaluation import extraction_metrics as metrics
from extraction_benchmark_support import REGENERATE, committed_results, gate_report, gold_cases, language_result

GATE_NAMES = [gate[0] for gate in metrics.GATES]


def check_quality_gate(language: str, gate: str) -> None:
    result = next(g for g in language_result(language)["gates"] if g["gate"] == gate)
    assert result["passed"], f"{language} {gate}: {result['actual']} fails {result['required']}\n{gate_report(language)}"


def check_resolution_reported_by_locality(language: str) -> None:
    """Same-file, cross-file and overall recall are reported separately, never folded together."""
    data = language_result(language)
    calls = data["resolution"]["CALLS"]
    assert set(calls) == {"same_file", "cross_file", "overall"}
    for rtype in ("CALLS", "EXTENDS", "IMPLEMENTS", "IMPORTS"):
        scopes = data["resolution"][rtype]
        overall = scopes["overall"]
        assert overall["tp"] == scopes["same_file"]["tp"] + scopes["cross_file"]["tp"]
        assert overall["fn"] == scopes["same_file"]["fn"] + scopes["cross_file"]["fn"]
    # The fixtures must actually exercise cross-file resolution, or its recall would be vacuous.
    assert calls["cross_file"]["tp"] + calls["cross_file"]["fn"] > 0


def check_unresolved_placeholders_are_not_internal_links(language: str) -> None:
    """A placeholder emitted where the gold says 'internal target' is a miss, never a hit."""
    source, target = f"function:{language}/a::caller", f"function:{language}/b::callee"
    case = metrics.GoldCase.model_validate({
        "gold_version": 1, "fixture": f"{language}/synthetic", "language": language, "split": "development",
        "description": "synthetic", "labelled_from": "source", "review": {"status": "pending_independent_review"},
        "files": {f"{language}/a": {"parse_status": "PARSED"}, f"{language}/b": {"parse_status": "PARSED"}},
        "facts": [
            {"id": "caller", "kind": "entity", "entity_type": "FUNCTION", "qualified_name": "caller",
             "file": f"{language}/a", "start_line": 1, "end_line": 2},
            {"id": "callee", "kind": "entity", "entity_type": "FUNCTION", "qualified_name": "callee",
             "file": f"{language}/b", "start_line": 1, "end_line": 2},
            {"id": "call", "kind": "relationship", "relationship_type": "CALLS", "source_key": source,
             "target_key": target, "resolution": "cross_file", "basis": "import_binding",
             "evidence": [{"file": f"{language}/a", "start_line": 2, "end_line": 2}]},
        ],
    })
    from types import SimpleNamespace

    from backend.services.extraction.contracts import EntityType, EvidenceRole

    def declared(key, path):
        return SimpleNamespace(entity_type=EntityType.FUNCTION, path=path, start_line=1, end_line=2, metadata={},
                               evidence=(((path, 1, 2, "hash", "v1"), EvidenceRole.DEFINITION),))

    placeholder = ("CALLS", source, "external:callee")
    observation = metrics.Observation(
        files={}, entities={source: declared(source, f"{language}/a"), target: declared(target, f"{language}/b")},
        edges={placeholder: metrics.ObservedEdge(placeholder, {(f"{language}/a", 2, 2)}, False, {})},
        imports={}, payload={"entities": []},
    )
    score = metrics.LanguageScore(language)
    metrics.score_case(case, metrics.FIXTURES_DIR, score, {}, first=observation, second=observation)

    calls = score.resolution["CALLS"]
    assert (calls["cross_file"].tp, calls["cross_file"].fn) == (0, 1)
    assert score.resolved_precision.tp == 0 and score.unresolved.fp == 1
    categories = {m.category for m in score.mismatches if "CALLS" in m.detail}
    assert categories == {metrics.CROSS_FILE_MISSING}


def check_every_error_is_categorized(language: str) -> None:
    data = language_result(language)
    assert set(data["errors"]["by_category"]) == set(metrics.CATEGORIES)
    assert all(item["category"] in metrics.CATEGORIES for item in data["errors"]["items"])
    assert sum(data["errors"]["by_category"].values()) == len(data["errors"]["items"])


def check_fixture_coverage(language: str) -> None:
    """Positive, negative, ambiguous, malformed and cross-file cases all exist, as the story requires."""
    cases = gold_cases(language)
    facts = [f for case in cases for f in case.facts]
    resolutions = {f.resolution for f in facts if isinstance(f, metrics.RelationshipFact)}
    assert {"same_file", "cross_file", "external", "unresolvable"} <= resolutions
    if language in ("python", "go"):
        # No overloading: ambiguity is a name bound twice, by alternative imports (Python) or by
        # build-tagged files (Go).
        assert "ambiguous" in resolutions
    else:
        # JVM overloads are always statically determined, so ambiguity means same-name, same-arity
        # overloads that only argument types can tell apart; at least one call must target one.
        methods = [f for f in facts if isinstance(f, metrics.EntityFact) and f.entity_type == "METHOD"]
        shapes = [_shape(f.qualified_name) for f in methods]
        overloaded = {f.key for f in methods if shapes.count(_shape(f.qualified_name)) > 1}
        assert any(isinstance(f, metrics.RelationshipFact) and f.target_key in overloaded for f in facts)
    assert any(isinstance(f, metrics.UnsupportedFact) for f in facts), "no unsupported (negative) facts"
    assert any(f.parse_status.value == "PARTIAL" for case in cases for f in case.files.values()), "no malformed file"
    assert {"development", "holdout"} <= {c.split for c in cases}
    assert all(c.labelled_from == "source" for c in cases)


def _shape(qualified_name: str) -> tuple[str, int]:
    """A method's name and first-parameter-list arity; commas inside type arguments do not count."""
    name, _, parameters = qualified_name.partition("(")
    parameters = parameters.replace("=>", "")  # Scala by-name and function types are not brackets
    depth, arity, seen = 0, 0, False
    for char in parameters:
        if char == ")" and depth == 0:
            break
        depth += char in "[<("
        depth -= char in "]>)"
        if char == "," and depth == 0:
            arity += 1
        seen = seen or not char.isspace()
    return name, arity + seen


def check_capability_reporting(language: str) -> None:
    capabilities = language_result(language)["capabilities"]
    assert capabilities["accuracy"] == 1.0, capabilities["mismatched"]
    assert capabilities["consistency_issues"] == []


def check_malformed_files_isolated(language: str) -> None:
    isolation = language_result(language)["malformed_isolation"]
    assert isolation["malformed_files"] > 0
    assert isolation["rate"] == 1.0 and isolation["repository_failures"] == 0


def check_published_baseline_is_current(language: str) -> None:
    committed = committed_results()["languages"].get(language)
    assert committed == language_result(language), f"published baseline is stale; run:\n  {REGENERATE}"


def check_basis_and_certainty_are_evaluated(language: str) -> None:
    """Every correctly resolved internal edge is checked against its hand-labelled basis and certainty."""
    data = language_result(language)
    basis, certainty = data["basis_accuracy"], data["certainty_accuracy"]
    assert basis["total"] >= 10 and certainty["total"] >= 10
    assert basis["rate"] == 1.0, [m for m in data["errors"]["items"] if m["area"] == "resolution_basis"]
    assert certainty["rate"] == 1.0, [m for m in data["errors"]["items"] if m["area"] == "certainty"]
    # Recall is published per expected basis, including the ones that need cross-file resolution
    # (import_path for languages importing declarations, import_binding for Go's package imports).
    assert {"import_path", "import_binding"} & set(data["resolution_recall_by_expected_basis"])


def check_origin_is_reported_separately(language: str) -> None:
    data = language_result(language)
    by_origin = data["by_origin"]
    for origin in ("first_party", "generated", "vendored"):
        counts = by_origin["entities"][origin]
        assert counts["tp"] + counts["fn"] > 0, f"no labelled {origin} entities"
    assert by_origin["classification"]["rate"] == 1.0


def check_capability_levels_are_reported(language: str) -> None:
    levels = language_result(language)["by_capability_level"]
    assert {"SUPPORTED", "PARTIAL"} <= set(levels)
    assert "declarations" in levels["SUPPORTED"]["capabilities"] and "calls" in levels["PARTIAL"]["capabilities"]


def check_typed_receivers_and_origins_are_labelled(language: str) -> None:
    cases = gold_cases(language)
    origins = {spec.origin for case in cases for spec in case.files.values()}
    assert {"first_party", "generated", "vendored"} <= origins
    typed = [f for case in cases for f in case.relationships
             if f.basis == "declared_type" and f.resolution == "same_file"]
    assert typed, "no same-file typed-receiver call is labelled"
