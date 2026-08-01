from backend.models.schemas import (
    AnalysisResult,
    ArtifactCount,
    ArtifactType,
    DetectedTech,
    RepoData,
)
from backend.services.artifact_classifier import (
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
    assert classification.artifacts[0].name == "database-server"
    assert classification.artifacts[0].path == "/"
    assert classification.artifacts[0].type == ArtifactType.DEPLOYABLE_SERVICE
    assert (
        artifact_for_manifest("crates/storage/Cargo.toml", classification)
        == "database-server"
    )


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
