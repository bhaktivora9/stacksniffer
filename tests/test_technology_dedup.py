from backend.models.schemas import DetectedTech
from backend.services.technology_dedup import dedup_records, dedup_stack


def record(name, *, role="library", source="manifest_passthrough",
           matched_file="build.gradle", version=None):
    return DetectedTech(
        name=name, technology_role=role, confidence=0.8,
        detection_source=source, matched_file=matched_file, version=version,
    )


def ai(name, role="frameworks"):
    return record(name, role=role, source="ai_inferred", matched_file=None)


def test_bare_lucene_twin_merges_into_manifest_coordinate():
    manifest = record("org.apache.lucene:lucene-core", version="9.12")
    assert dedup_records([manifest, ai("Lucene")]) == [manifest]


def test_all_lucene_submodules_survive_while_bare_twin_is_removed():
    modules = [
        record(f"org.apache.lucene:{artifact}")
        for artifact in (
            "lucene-core", "lucene-facet", "lucene-grouping", "lucene-join",
            "lucene-analysis-common",
        )
    ]
    assert dedup_records([*modules, ai("Lucene")]) == modules


def test_log4j_siblings_survive_while_family_twin_is_removed():
    modules = [
        record("org.apache.logging.log4j:log4j-api"),
        record("org.apache.logging.log4j:log4j-core"),
    ]
    assert dedup_records([*modules, ai("Log4j", "observability")]) == modules


def test_exact_artifact_display_twins_merge():
    for coordinate, display_name in (
        ("com.carrotsearch:hppc", "HPPC"),
        ("org.hdrhistogram:HdrHistogram", "HdrHistogram"),
    ):
        manifest = record(coordinate)
        assert dedup_records([ai(display_name), manifest]) == [manifest]


def test_manifest_wins_and_preserves_version_when_ai_arrives_first():
    manifest = record("com.carrotsearch:hppc", version="0.10.0")
    result = dedup_records([ai("HPPC"), manifest])
    assert result == [manifest]
    assert result[0].version == "0.10.0"


def test_separator_variants_merge_within_one_analysis():
    manifest = record("typing-extensions")
    assert dedup_records([manifest, ai("Typing Extensions")]) == [manifest]


def test_react_and_react_dom_remain_distinct():
    react = record("react", matched_file="package.json")
    react_dom = record("react-dom", matched_file="package.json")
    assert dedup_records([react, react_dom]) == [react, react_dom]


def test_global_stack_dedup_catches_cross_bucket_twin():
    manifest = record("org.apache.lucene:lucene-core", role="library")
    twin = ai("Lucene", role="frameworks")
    merges = []
    result = dedup_stack(
        {"library": [manifest], "frameworks": [twin], "databases": []},
        on_merge=merges.append,
    )
    assert result == {"library": [manifest], "frameworks": [], "databases": []}
    assert merges[0]["removed"] == "Lucene"


def test_same_family_coordinates_with_different_artifacts_do_not_merge():
    core = record("org.apache.lucene:lucene-core")
    facet = record("org.apache.lucene:lucene-facet")
    assert dedup_records([core, facet]) == [core, facet]


def test_unrelated_technologies_stay_separate():
    records = [record("org.apache.lucene:lucene-core"), ai("FastAPI")]
    assert dedup_records(records) == records


def test_equal_provenance_prefers_higher_confidence():
    lower = ai("Perplexity AI")
    higher = ai("perplexityai")
    higher.confidence = 0.95
    assert dedup_records([lower, higher]) == [higher]


def test_global_dedup_preserves_emergent_role_bucket():
    telemetry = ai("Custom Telemetry", role="observability")
    result = dedup_stack({"library": [], "observability": [telemetry]})
    assert result == {"library": [], "observability": [telemetry]}


def test_dict_records_are_supported():
    manifest = {
        "name": "sentence-transformers",
        "technology_role": "ai_ml",
        "confidence": 0.7,
        "detection_source": "manifest_passthrough",
        "matched_file": "requirements.txt",
        "version_spec": "==3.0.0",
    }
    twin = {
        "name": "Sentence Transformers",
        "technology_role": "ai_ml",
        "confidence": 0.95,
        "detection_source": "ai_inferred",
        "matched_file": None,
    }
    assert dedup_records([twin, manifest]) == [manifest]
