from models.schemas import (
    AnalysisResult,
    ArtifactCount,
    ArtifactType,
    DetectedTech,
    RepoData,
)
from services.artifact_classifier import (
    artifact_for_manifest,
    assign_artifact_ownership,
    classify_artifacts,
)


def _repo(file_tree, file_contents, name="example"):
    return RepoData(
        owner="test",
        name=name,
        full_name=f"test/{name}",
        description=None,
        stars=0,
        forks=0,
        topics=[],
        license=None,
        default_branch="main",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        file_tree=file_tree,
        file_contents=file_contents,
    )


SINGLE_ARTIFACT_REPO = _repo(
    name="flask-api",
    file_tree=[
        "requirements.txt",
        "Dockerfile",
        "app/__init__.py",
        "app/main.py",
        "app/routes.py",
        "scripts/seed_db.py",
        "tests/test_routes.py",
        "README.md",
    ],
    file_contents={
        "requirements.txt": "flask==3.0.0\ngunicorn==21.2.0\npsycopg2==2.9.9\n",
        "Dockerfile": 'FROM python:3.12\nCMD ["gunicorn", "app.main:app"]\n',
    },
)

MULTI_ARTIFACT_REPO = _repo(
    name="influxdb",
    file_tree=[
        "Cargo.toml",
        "Dockerfile",
        "src/main.rs",
        "src/engine.rs",
        "ui/package.json",
        "ui/src/App.tsx",
        "ui/src/index.tsx",
        "README.md",
    ],
    file_contents={
        "Cargo.toml": (
            '[package]\nname = "influxdb"\n\n'
            "[dependencies]\ntokio = \"1\"\nrocksdb = \"0.21\"\n"
        ),
        "Dockerfile": 'FROM rust:1.75\nENTRYPOINT ["influxd"]\n',
        "ui/package.json": (
            '{"name": "influxdb-ui", "dependencies": '
            '{"react": "^18.0.0", "react-dom": "^18.0.0"}, '
            '"scripts": {"build": "vite build"}}'
        ),
    },
)


def test_single_artifact_is_not_oversplit():
    result = classify_artifacts(SINGLE_ARTIFACT_REPO)
    assert result.artifact_count == ArtifactCount.SINGLE
    assert len(result.artifacts) == 1
    assert {artifact.name for artifact in result.artifacts}.isdisjoint({"scripts", "tests"})


def test_single_artifact_is_the_root_service():
    classification = classify_artifacts(SINGLE_ARTIFACT_REPO)
    artifact = classification.artifacts[0]
    assert artifact.path == "/"
    assert artifact.primary is True
    assert artifact.subordinate_to is None
    assert artifact.type == ArtifactType.DEPLOYABLE_SERVICE
    assert artifact_for_manifest("", classification) == artifact.name
    assert artifact_for_manifest("app/routes.py", classification) == artifact.name


def test_fastapi_single_package_repo_marks_fastapi_artifact_primary():
    repo = _repo(
        name="fastapi",
        file_tree=[
            "pyproject.toml",
            "app/main.py",
        ],
        file_contents={
            "pyproject.toml": (
                '[project]\\nname = "fastapi"\\n'
                'dependencies = ["fastapi>=0.100", "uvicorn>=0.23"]\\n'
            ),
            "app/main.py": "from fastapi import FastAPI\\n",
        },
    )

    classification = classify_artifacts(repo)
    assert classification.artifact_count == ArtifactCount.SINGLE
    assert len(classification.artifacts) == 1
    artifact = classification.artifacts[0]
    assert artifact.name == "fastapi"
    assert artifact.path == "/"
    assert artifact.primary is True
    assert artifact.type == ArtifactType.DEPLOYABLE_SERVICE


def test_influxdb_is_service_with_subordinate_ui():
    result = classify_artifacts(MULTI_ARTIFACT_REPO)
    assert result.artifact_count == ArtifactCount.MULTI
    assert len(result.artifacts) == 2

    root = next(artifact for artifact in result.artifacts if artifact.primary)
    ui = next(artifact for artifact in result.artifacts if artifact.path == "/ui")
    assert root.path == "/"
    assert root.type == ArtifactType.DEPLOYABLE_SERVICE
    assert root.subordinate_to is None
    assert ui.type == ArtifactType.WEB_APPLICATION
    assert ui.subordinate_to == root.name


def test_manifest_ownership_resolves_ui_but_not_shared_workspace_root():
    result = classify_artifacts(MULTI_ARTIFACT_REPO)
    ui = next(artifact for artifact in result.artifacts if artifact.path == "/ui")
    root = next(artifact for artifact in result.artifacts if artifact.path == "/")
    assert artifact_for_manifest("ui/package.json", result) == ui.name
    assert artifact_for_manifest("Cargo.toml", result) == root.name

    monorepo = _repo(
        name="monorepo",
        file_tree=[
            "package.json",
            "packages/api/package.json",
            "packages/api/server.js",
            "packages/web/package.json",
        ],
        file_contents={
            "package.json": '{"name": "root", "workspaces": ["packages/*"]}',
            "packages/api/package.json": '{"name": "api", "dependencies": {"express": "^4"}}',
            "packages/web/package.json": '{"name": "web", "dependencies": {"react": "^18"}}',
        },
    )

    classification = classify_artifacts(monorepo)
    assert classification.artifact_count == ArtifactCount.MULTI
    assert {artifact.name for artifact in classification.artifacts} == {"api", "web"}
    assert artifact_for_manifest("package.json", classification) is None
    assert artifact_for_manifest("packages/api/package.json", classification) == "api"
    assert artifact_for_manifest("packages/api/src/server.js", classification) is None


def test_backend_manifest_dependencies_and_dockerfile_share_backend_ownership():
    repo = _repo(
        name="full-stack-fastapi-template",
        file_tree=[
            "backend/pyproject.toml",
            "backend/Dockerfile",
            "backend/app/main.py",
            "frontend/package.json",
            "frontend/src/App.tsx",
        ],
        file_contents={
            "backend/pyproject.toml": (
                '[project]\nname = "backend"\n'
                'dependencies = ["sqlmodel>=0.0.21", "pytest"]'
            ),
            "backend/Dockerfile": 'CMD ["fastapi", "run", "app/main.py"]',
            "frontend/package.json": (
                '{"name":"frontend","dependencies":{"react":"^18"}}'
            ),
        },
    )
    classification = classify_artifacts(repo)
    technologies = [
        DetectedTech(
            name="SQLModel",
            confidence=0.85,
            detection_source="manifest_table",
            technology_role="databases",
            matched_file="backend/pyproject.toml",
        ),
        DetectedTech(
            name="pytest",
            confidence=0.85,
            detection_source="manifest_table",
            technology_role="testing",
            matched_file="backend/pyproject.toml",
        ),
        DetectedTech(
            name="Docker",
            confidence=1.0,
            detection_source="file_signal",
            technology_role="infra",
            matched_file="backend/Dockerfile",
        ),
        DetectedTech(
            name="Python",
            confidence=0.99,
            detection_source="file_signal",
            technology_role="languages",
            matched_file="backend/app/main.py",
        ),
    ]

    assign_artifact_ownership(technologies, classification)

    assert [tech.belongs_to_artifact for tech in technologies[:3]] == [
        "backend",
        "backend",
        "backend",
    ]
    assert technologies[3].belongs_to_artifact is None


def test_duplicate_manifest_names_are_stably_path_qualified():
    repo = _repo(
        name="workspace",
        file_tree=[
            "services/api/package.json",
            "services/api/server.js",
            "services/worker/package.json",
            "services/worker/server.js",
        ],
        file_contents={
            "services/api/package.json": (
                '{"name":"app","dependencies":{"express":"^4"}}'
            ),
            "services/worker/package.json": (
                '{"name":"app","dependencies":{"fastify":"^4"}}'
            ),
        },
    )

    classification = classify_artifacts(repo)

    assert [artifact.name for artifact in classification.artifacts] == [
        "app",
        "app@services-worker",
    ]
    assert (
        artifact_for_manifest(
            "services/worker/package.json",
            classification,
        )
        == "app@services-worker"
    )


def test_internal_cargo_libraries_collapse_into_single_binary_artifact():
    repo = _repo(
        name="database",
        file_tree=[
            "Cargo.toml",
            "crates/server/Cargo.toml",
            "crates/server/src/main.rs",
            "crates/storage/Cargo.toml",
            "crates/storage/src/lib.rs",
            "crates/query/Cargo.toml",
            "crates/query/src/lib.rs",
            "Dockerfile",
        ],
        file_contents={
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]',
            "crates/server/Cargo.toml": '[package]\nname = "database-server"',
            "crates/storage/Cargo.toml": '[package]\nname = "storage"',
            "crates/query/Cargo.toml": '[package]\nname = "query"',
            "Dockerfile": 'ENTRYPOINT ["database-server"]',
        },
    )

    classification = classify_artifacts(repo)

    assert classification.artifact_count == ArtifactCount.SINGLE
    assert classification.artifacts[0].name == "database"
    assert classification.artifacts[0].path == "/"
    assert classification.artifacts[0].type == ArtifactType.DEPLOYABLE_SERVICE
    assert (
        artifact_for_manifest("crates/storage/Cargo.toml", classification)
        == "database"
    )


def test_containerized_cli_entrypoint_does_not_imply_a_service():
    repo = _repo(
        name="repo-tool",
        file_tree=["Cargo.toml", "src/main.rs", "Dockerfile"],
        file_contents={
            "Cargo.toml": '[package]\nname = "repo-tool"',
            "Dockerfile": 'ENTRYPOINT ["repo-tool"]',
        },
    )

    classification = classify_artifacts(repo)

    assert classification.artifacts[0].type == ArtifactType.CLI_TOOL


def test_exposed_port_promotes_a_containerized_binary_to_service():
    repo = _repo(
        name="repo-server",
        file_tree=["Cargo.toml", "src/main.rs", "Dockerfile"],
        file_contents={
            "Cargo.toml": '[package]\nname = "repo-server"',
            "Dockerfile": 'EXPOSE 8080\nENTRYPOINT ["repo-server"]',
        },
    )

    classification = classify_artifacts(repo)

    assert classification.artifacts[0].type == ArtifactType.DEPLOYABLE_SERVICE


def test_pure_library_workspace_collapses_to_genuine_library():
    repo = _repo(
        name="serde-style-workspace",
        file_tree=[
            "Cargo.toml",
            "crates/core/Cargo.toml",
            "crates/core/src/lib.rs",
            "crates/derive/Cargo.toml",
            "crates/derive/src/lib.rs",
        ],
        file_contents={
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]',
            "crates/core/Cargo.toml": '[package]\nname = "core"',
            "crates/derive/Cargo.toml": '[package]\nname = "derive"',
        },
    )

    classification = classify_artifacts(repo)

    assert classification.artifact_count == ArtifactCount.SINGLE
    assert classification.artifacts[0].path == "/"
    assert classification.artifacts[0].type == ArtifactType.LIBRARY


def test_analysis_result_has_a_top_level_home_for_classification():
    field = AnalysisResult.model_fields["repository_classification"]
    assert field.is_required() is False


def test_detected_tech_ownership_is_wired_from_manifest_only_in_multi_repo():
    classification = classify_artifacts(MULTI_ARTIFACT_REPO)
    technologies = [
        DetectedTech(
            name="React",
            confidence=0.98,
            detection_source="manifest",
            technology_role="frameworks",
            matched_file="ui/package.json",
        ),
        DetectedTech(
            name="TypeScript",
            confidence=0.70,
            detection_source="file_signal",
            technology_role="library",
            matched_file="ui/src/App.tsx",
        ),
    ]

    assign_artifact_ownership(technologies, classification)

    ui = next(artifact for artifact in classification.artifacts if artifact.path == "/ui")
    assert technologies[0].belongs_to_artifact == ui.name
    assert technologies[1].belongs_to_artifact is None


def test_single_artifact_owns_technologies_without_manifest_evidence():
    classification = classify_artifacts(SINGLE_ARTIFACT_REPO)
    tech = DetectedTech(
        name="Python",
        confidence=0.95,
        detection_source="language_bytes",
        technology_role="languages",
    )

    assign_artifact_ownership([tech], classification)

    assert tech.belongs_to_artifact == classification.artifacts[0].name


def test_namesake_package_beats_incidental_web_and_docs_outputs():
    repo = _repo(
        name="next.js",
        file_tree=[
            "package.json",
            "packages/next/package.json",
            "packages/next/index.js",
            "packages/bundle-analyzer/package.json",
            "packages/bundle-analyzer/src/App.tsx",
            "docs/package.json",
        ],
        file_contents={
            "package.json": '{"private":true,"workspaces":["packages/*"]}',
            "packages/next/package.json": '{"name":"next"}',
            "packages/bundle-analyzer/package.json": (
                '{"name":"bundle-analyzer","dependencies":{"react":"18"}}'
            ),
            "docs/package.json": '{"name":"next-docs","dependencies":{"react":"18"}}',
        },
    )

    classification = classify_artifacts(repo)
    primary = next(artifact for artifact in classification.artifacts if artifact.primary)
    assert primary.name == "next"
    assert primary.path == "/packages/next"


def test_conventional_server_module_is_primary_product_fallback():
    repo = _repo(
        name="elasticsearch",
        file_tree=[
            "build.gradle",
            "server/build.gradle",
            "server/src/main/java/org/elasticsearch/bootstrap/Elasticsearch.java",
            "distribution/tools/cli/build.gradle",
            "distribution/tools/cli/src/main/java/Main.java",
            "docs/package.json",
        ],
        file_contents={
            "build.gradle": "group = 'elasticsearch-build'",
            "server/build.gradle": "apply plugin: 'elasticsearch.internal-java'",
            "distribution/tools/cli/build.gradle": (
                "plugins { id 'application' }\nmainClass = 'Main'"
            ),
            "docs/package.json": '{"name":"elasticsearch-docs","dependencies":{"react":"18"}}',
        },
    )

    classification = classify_artifacts(repo)
    primary = next(artifact for artifact in classification.artifacts if artifact.primary)
    assert primary.name == "server"
    assert primary.path == "/server"
    assert primary.type == ArtifactType.DEPLOYABLE_SERVICE

    technologies = [
        DetectedTech(
            name="org.apache.lucene:lucene-core",
            confidence=0.85,
            detection_source="manifest_table",
            technology_role="library",
            matched_file="build.gradle",
        ),
        DetectedTech(
            name="Lucene",
            confidence=0.8,
            detection_source="ai_inferred",
            technology_role="library",
        ),
        DetectedTech(
            name="Java",
            confidence=1.0,
            detection_source="github_linguist",
            technology_role="languages",
        ),
    ]
    assign_artifact_ownership(technologies, classification)
    assert [tech.belongs_to_artifact for tech in technologies] == [
        "server", "server", None,
    ]


def test_elasticsearch_fixture_remote_cannot_become_primary_artifact():
    fixture = (
        "build-tools-internal/src/integTest/resources/org/elasticsearch/"
        "gradle/internal/fake_git/remote"
    )
    repo = _repo(
        name="elasticsearch",
        file_tree=[
            "build.gradle",
            "server/build.gradle",
            "server/src/main/java/org/elasticsearch/bootstrap/Elasticsearch.java",
            f"{fixture}/build.gradle",
        ],
        file_contents={
            "build.gradle": "group = 'elasticsearch-build'",
            "server/build.gradle": "apply plugin: 'elasticsearch.internal-java'",
            f"{fixture}/build.gradle": "plugins { id 'java-library' }",
        },
    )

    classification = classify_artifacts(repo)
    primary = next(artifact for artifact in classification.artifacts if artifact.primary)
    assert primary.name == "server"
    assert primary.path == "/server"
    assert all("fake_git" not in artifact.path for artifact in classification.artifacts)
    assert all(artifact.name != "remote" for artifact in classification.artifacts)


def test_non_product_filter_is_segment_exact_and_keeps_real_modules():
    repo = _repo(
        name="product",
        file_tree=[
            "server/build.gradle",
            "src/latest/build.gradle",
            "qa/smoke/build.gradle",
            "examples/demo/build.gradle",
        ],
        file_contents={
            "server/build.gradle": "apply plugin: 'internal-java'",
            "src/latest/build.gradle": "plugins { id 'application' }",
            "qa/smoke/build.gradle": "plugins { id 'application' }",
            "examples/demo/build.gradle": "plugins { id 'application' }",
        },
    )

    classification = classify_artifacts(repo)
    paths = {artifact.path for artifact in classification.artifacts}
    assert "/server" in paths
    assert "/src/latest" in paths
    assert all(not path.startswith(("/qa/", "/examples/")) for path in paths)


def test_namesake_api_library_outranks_incidental_migrator_binary():
    repo = _repo(
        name="slf4j",
        file_tree=[
            "pom.xml",
            "slf4j-api/pom.xml",
            "slf4j-api/src/main/java/org/slf4j/Logger.java",
            "slf4j-simple/pom.xml",
            "slf4j-migrator/pom.xml",
            "slf4j-migrator/src/main/java/org/slf4j/migrator/Main.java",
        ],
        file_contents={
            "pom.xml": "<packaging>pom</packaging><modules></modules>",
            "slf4j-api/pom.xml": "<packaging>jar</packaging>",
            "slf4j-simple/pom.xml": "<packaging>jar</packaging>",
            "slf4j-migrator/pom.xml": (
                "<packaging>jar</packaging><mainClass>"
                "org.slf4j.migrator.Main</mainClass>"
            ),
        },
    )

    classification = classify_artifacts(repo)
    primary = next(artifact for artifact in classification.artifacts if artifact.primary)

    assert primary.name == "slf4j-api"
    assert primary.path == "/slf4j-api"
    assert primary.type == ArtifactType.LIBRARY
