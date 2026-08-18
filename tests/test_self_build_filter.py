from backend.services.manifest_parser import parse_manifest_dependencies
from backend.services.self_build_filter import (
    derive_repo_namespaces,
    filter_self_build_modules,
    is_self_build_module,
)


ES = {"root_group": "org.elasticsearch", "repo_full_name": "elastic/elasticsearch"}


def dep(name, matched_file=None):
    return {"name": name, "matched_file": matched_file}


def test_elasticsearch_build_tooling_is_dropped():
    dependencies = [
        dep("org.elasticsearch:build-conventions", "build-tools-internal/build.gradle"),
        dep("org.elasticsearch.gradle:build-tools", "build-tools-internal/build.gradle"),
        dep("org.elasticsearch.gradle:reaper", "build-tools-internal/build.gradle"),
    ]
    assert filter_self_build_modules(dependencies, **ES) == []


def test_gradle_subgroup_matches_repo_root():
    namespaces = derive_repo_namespaces("org.elasticsearch", "elastic/elasticsearch")
    assert is_self_build_module(dep("org.elasticsearch.gradle:build-tools"), namespaces)


def test_noncanonical_gradle_group_does_not_disable_repo_namespace_fallback():
    namespaces = derive_repo_namespaces("elasticsearch-build", "elastic/elasticsearch")
    assert "org.elasticsearch" in namespaces
    assert is_self_build_module(
        dep(
            "org.elasticsearch.gradle:build-tools",
            "build-tools-internal/build.gradle",
        ),
        namespaces,
    )


def test_own_real_libraries_are_kept():
    dependencies = [
        dep("org.elasticsearch:elasticsearch-core", "server/build.gradle"),
        dep("org.elasticsearch:server", "server/build.gradle"),
    ]
    assert filter_self_build_modules(dependencies, **ES) == dependencies


def test_third_party_dependencies_are_kept():
    dependencies = [
        dep("com.someone:build-tools", "server/build.gradle"),
        dep("org.apache.lucene:lucene-core", "server/build.gradle"),
        dep("com.carrotsearch:hppc", "server/build.gradle"),
        dep("org.apache.logging.log4j:log4j-core", "server/build.gradle"),
    ]
    assert filter_self_build_modules(dependencies, **ES) == dependencies


def test_bare_name_and_unknown_namespace_are_kept():
    assert filter_self_build_modules([dep("lodash")], **ES) == [dep("lodash")]
    dependency = dep("org.elasticsearch.gradle:build-tools", "build-tools/build.gradle")
    assert filter_self_build_modules(
        [dependency], root_group=None, repo_full_name=None,
    ) == [dependency]


def test_manifest_pipeline_extracts_root_group_and_filters_before_classification():
    files = {
        "build.gradle": "group = 'org.elasticsearch'\n",
        "build-tools-internal/build.gradle": """
            implementation 'org.elasticsearch.gradle:build-tools:1.0'
        """,
        "server/build.gradle": """
            implementation 'org.elasticsearch:elasticsearch-core:1.0'
            implementation 'org.apache.lucene:lucene-core:9.0'
        """,
    }
    result = parse_manifest_dependencies(
        files, list(files), repo_full_name="unrelated/repository-name",
    )
    assert [item["name"] for item in result["raw_deps"]] == [
        "org.elasticsearch:elasticsearch-core",
        "org.apache.lucene:lucene-core",
    ]
    assert "SELF_BUILD_MODULE_EXCLUDED:org.elasticsearch.gradle:build-tools" in result["flags"]


def test_manifest_pipeline_uses_repo_fallback_despite_short_root_gradle_group():
    files = {
        "build.gradle": "group = 'elasticsearch-build'\n",
        "build-tools-internal/build.gradle": """
            implementation 'org.elasticsearch:build-conventions:1.0'
            implementation 'org.elasticsearch.gradle:build-tools:1.0'
            implementation 'org.elasticsearch.gradle:reaper:1.0'
        """,
    }
    result = parse_manifest_dependencies(
        files, list(files), repo_full_name="elastic/elasticsearch",
    )
    assert result["raw_deps"] == []
    assert {
        "SELF_BUILD_MODULE_EXCLUDED:org.elasticsearch:build-conventions",
        "SELF_BUILD_MODULE_EXCLUDED:org.elasticsearch.gradle:build-tools",
        "SELF_BUILD_MODULE_EXCLUDED:org.elasticsearch.gradle:reaper",
    }.issubset(result["flags"])


def test_drop_callback_receives_only_dropped_dependency():
    dropped = []
    dependencies = [
        dep("org.elasticsearch.gradle:reaper", "build-tools/build.gradle"),
        dep("org.apache.lucene:lucene-core", "server/build.gradle"),
    ]
    filter_self_build_modules(dependencies, on_drop=dropped.append, **ES)
    assert [item["name"] for item in dropped] == ["org.elasticsearch.gradle:reaper"]
