from copy import deepcopy

from models.schemas import DetectedTech
from services.dep_fallback import (
    _CARGO,
    _GO,
    _MAVEN_GROUP,
    _NPM,
    _PYPI,
    build_base_detections,
    collect_unresolved_tail,
    classify_dep,
    enrich_with_classifications,
)


def _base(name, ecosystem):
    return build_base_detections([{"name": name, "ecosystem": ecosystem}])[0]


def test_every_seed_entry_carries_role_metadata():
    for table in (_NPM, _PYPI, _MAVEN_GROUP, _CARGO, _GO):
        assert table
        assert all(
            set(entry) == {
                "technology_role",
                "layer",
                "multi_role",
                "secondary_roles",
            }
            for entry in table.values()
        )


def test_multi_role_seed_assignments_are_provisional_across_ecosystems():
    cases = [
        ("redis", "npm"),
        ("redis", "pypi"),
        ("redis", "cargo"),
        ("redis.clients:jedis", "maven"),
        ("github.com/redis/go-redis", "go"),
        ("elasticsearch", "pypi"),
        ("@elastic/elasticsearch", "npm"),
        ("vite", "npm"),
    ]
    for name, ecosystem in cases:
        result = _base(name, ecosystem)
        assert result["architectural_layer"]["assignment_method"] == "provisional"
        assert result["architectural_layer"]["disambiguation_pending"] is True
        assert result["multi_role"] is True
        assert result["secondary_roles"]


def test_invariant_seed_assignments_remain_deterministic():
    cases = [
        ("org.postgresql:postgresql", "maven", "databases"),
        ("org.apache.kafka:kafka-clients", "maven", "messaging"),
    ]
    for name, ecosystem, layer in cases:
        result = _base(name, ecosystem)
        assert result["technology_role"] == layer
        assert result["architectural_layer"]["assignment_method"] == "deterministic"
        assert result["multi_role"] is False


def test_ai_enrichment_does_not_erase_provisional_provenance():
    base = [_base("redis", "npm")]
    enriched = enrich_with_classifications(
        base, [{"name": "redis", "technology_role": "databases", "confidence": 0.9}]
    )
    assert enriched[0]["architectural_layer"]["assignment_method"] == "provisional"
    assert enriched[0]["multi_role"] is True


def test_ai_enrichment_merges_normalized_name_twin_into_manifest_detection():
    base = build_base_detections([
        {"name": "typing-extensions", "ecosystem": "pypi", "scope": "required"}
    ])

    enriched = enrich_with_classifications(
        base,
        [
            {
                "name": "Typing Extensions",
                "technology_role": "library",
                "confidence": 0.92,
                "architectural_layer": "language_runtime",
            },
            {
                "name": "typing_extensions",
                "technology_role": "library",
                "confidence": 0.91,
                "architectural_layer": "language_runtime",
            },
        ],
    )

    assert len(enriched) == 1
    assert enriched[0]["name"] == "typing-extensions"
    assert enriched[0]["detection_source"] == "manifest_passthrough"
    assert enriched[0]["architectural_layer"] is None


def test_ai_enrichment_strips_missing_separators_without_replacing_manifest_record():
    base = build_base_detections([
        {"name": "perplexityai", "ecosystem": "pypi", "scope": "required"},
        {"name": "sentence-transformers", "ecosystem": "pypi", "scope": "required"},
    ])
    deterministic_records = deepcopy(base)

    enriched = enrich_with_classifications(
        base,
        [
            {"name": "Perplexity AI", "technology_role": "ai_ml", "confidence": 0.95},
            {"name": "Sentence Transformers", "technology_role": "ai_ml", "confidence": 0.95},
        ],
    )

    assert [entry["name"] for entry in enriched] == [
        "perplexityai",
        "sentence-transformers",
    ]
    assert enriched == deterministic_records


def test_assignment_metadata_survives_detected_tech_serialization():
    source = _base("redis", "npm")
    tech = DetectedTech(
        name=source["name"],
        technology_role=source["technology_role"],
        confidence=source["confidence"],
        detection_source=source["detection_source"],
        multi_role=source["multi_role"],
        secondary_roles=source["secondary_roles"],
        architectural_layer=source["architectural_layer"],
        usage_scope=source["usage_scope"],
    )
    assert tech.model_dump(mode="json")["architectural_layer"]["assignment_method"] == "provisional"
    assert tech.model_dump()["secondary_roles"] == ["cache", "messaging"]


def test_unseeded_opentelemetry_is_not_claimed_as_deterministic():
    _, _, tier = classify_dep("opentelemetry", "pypi")
    assert tier == "heuristic"


def test_maven_uses_longest_curated_group_prefix_before_tail_collection():
    raw = [
        {
            "name": "org.springframework.security:spring-security-core",
            "ecosystem": "maven",
        },
        {
            "name": "com.example:unknown-client",
            "ecosystem": "maven",
        },
    ]

    base = build_base_detections(raw)
    spring = next(dep for dep in base if dep["name"].startswith("org.springframework"))
    assert spring["technology_role"] == "frameworks"
    assert spring["fallback_tier"] == "table"
    assert spring["architectural_layer"]["assignment_method"] == "deterministic"

    tail = collect_unresolved_tail(raw, base)
    assert [dep["name"] for dep in tail] == ["com.example:unknown-client"]


def test_unknown_test_dependency_keeps_scope_without_inventing_a_testing_role():
    raw = [
        {
            "name": "opaque-helper",
            "ecosystem": "pypi",
            "scope": "test",
        }
    ]
    base = build_base_detections(raw)
    assert base[0]["technology_role"] == "library"
    assert base[0]["architectural_layer"] is None
    assert base[0]["usage_scope"] == "test"
    assert [dep["name"] for dep in collect_unresolved_tail(raw, base)] == [
        "opaque-helper"
    ]


def test_test_group_does_not_override_a_dependency_intrinsic_role_or_layer():
    results = build_base_detections([
        {"name": "numpy", "ecosystem": "pypi", "scope": "test"},
        {"name": "pytest", "ecosystem": "pypi", "scope": "test"},
    ])

    numpy, pytest = results
    assert numpy["technology_role"] == "ai_ml"
    assert numpy["architectural_layer"]["primary"] == "ai_ml"
    assert numpy["usage_scope"] == "test"

    # Pytest is testing because of its package identity, not its group.
    assert pytest["technology_role"] == "testing"
    assert pytest["architectural_layer"]["primary"] == "testing"
    assert pytest["usage_scope"] == "test"


def test_dev_scope_does_not_force_frontend_tooling_into_testing_layer():
    names_and_layers = [
        ("@tailwindcss/vite", "frontend"),
        ("@types/node", "language_runtime"),
        ("@types/react", "frontend"),
        ("typescript", "language_runtime"),
        ("vite", "infra"),
        ("@vitejs/plugin-react-swc", "infra"),
        ("tw-animate-css", "frontend"),
    ]

    results = build_base_detections([
        {"name": name, "ecosystem": "npm", "scope": "dev"}
        for name, _ in names_and_layers
    ])

    assert [result["usage_scope"] for result in results] == ["dev"] * len(results)
    assert [
        result["architectural_layer"]["primary"] for result in results
    ] == [layer for _, layer in names_and_layers]
    assert all(
        result["architectural_layer"]["primary"] != "testing"
        for result in results
    )


def test_tail_ai_layer_is_independent_of_technology_role_and_null_is_preserved():
    base = build_base_detections(
        [
            {"name": "unknown-broker", "ecosystem": "pypi"},
            {"name": "unclear-tool", "ecosystem": "pypi"},
        ]
    )
    enriched = enrich_with_classifications(
        base,
        [
            {
                "name": "unknown-broker",
                "technology_role": "messaging",
                "architectural_layer": "data",
                "layer_confidence": 0.71,
                "confidence": 0.8,
            },
            {
                "name": "unclear-tool",
                "technology_role": "library",
                "architectural_layer": None,
                "confidence": 0.6,
            },
        ],
    )
    by_name = {dep["name"]: dep for dep in enriched}
    assert by_name["unknown-broker"]["technology_role"] == "messaging"
    assert by_name["unknown-broker"]["architectural_layer"] == {
        "primary": "data",
        "secondary": [],
        "assignment_method": "ai_inferred",
        "confidence": 0.71,
        "disambiguation_pending": False,
    }
    assert by_name["unclear-tool"]["architectural_layer"] is None


def test_null_layer_resolution_signal_survives_enrichment():
    base = build_base_detections(
        [{"name": "unclear-tool", "ecosystem": "pypi"}]
    )
    enriched = enrich_with_classifications(
        base,
        [
            {
                "name": "unclear-tool",
                "technology_role": "library",
                "architectural_layer": None,
                "layer_resolution": "llm_null",
                "confidence": 0.6,
            }
        ],
    )
    assert enriched[0]["architectural_layer"] is None
    assert enriched[0]["layer_inference_status"] == "llm_null"


def test_runtime_dependency_uses_language_runtime_layer_from_seed():
    tokio = _base("tokio", "cargo")
    assert tokio["technology_role"] == "infra"
    assert tokio["architectural_layer"]["primary"] == "language_runtime"
    assert tokio["architectural_layer"]["assignment_method"] == "deterministic"


def test_cross_cutting_maven_artifacts_resolve_to_deterministic_null():
    for name in (
        "com.carrotsearch:hppc",
        "org.apache.commons:commons-math3",
        "com.ibm.icu:icu4j",
    ):
        result = _base(name, "maven")
        assert result["technology_role"] == "library"
        assert result["fallback_tier"] == "table"
        assert result["architectural_layer"] is None
