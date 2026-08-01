"""Deterministic manifest-based artifact classification.

This module identifies consumable/deployable units, not arbitrary build units.
A recognized manifest creates a build-unit candidate; independent output
evidence promotes it to an artifact. Internal modules collapse to the output.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from backend.models.schemas import (
    Artifact,
    ArtifactCount,
    ArtifactType,
    DetectedTech,
    RepoData,
    RepositoryClassification,
)

_MANIFESTS = {
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "pipfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "cargo.toml",
    "go.mod",
    "composer.json",
    "gemfile",
}
_DEPLOYMENT_DESCRIPTORS = {
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "chart.yaml",
    "skaffold.yaml",
}

_WEB_DEPENDENCIES = {
    "react",
    "react-dom",
    "vue",
    "svelte",
    "next",
    "nuxt",
    "@angular/core",
    "vite",
}
_NODE_SERVERS = {"express", "fastify", "koa", "@nestjs/core", "hapi"}
_PYTHON_SERVERS = {"flask", "django", "fastapi", "starlette", "sanic", "gunicorn", "uvicorn"}


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def _directory(path: str) -> str:
    parent = str(PurePosixPath(_norm_path(path)).parent)
    return "" if parent == "." else parent


def _artifact_path(directory: str) -> str:
    return "/" if not directory else f"/{directory}"


def _content(repo: RepoData, path: str) -> str:
    normalized = _norm_path(path)
    for candidate, content in repo.file_contents.items():
        if _norm_path(candidate) == normalized:
            return content or ""
    return ""


def _package_json(content: str) -> dict:
    try:
        value = json.loads(content or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _manifest_name(repo: RepoData, directory: str, manifests: list[str]) -> str:
    package_path = next(
        (path for path in manifests if PurePosixPath(path).name.lower() == "package.json"),
        None,
    )
    if package_path:
        name = _package_json(_content(repo, package_path)).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip().split("/")[-1]

    for path in manifests:
        basename = PurePosixPath(path).name.lower()
        content = _content(repo, path)
        patterns = {
            "cargo.toml": r"(?m)^\s*name\s*=\s*[\"']([^\"']+)",
            "pyproject.toml": r"(?m)^\s*name\s*=\s*[\"']([^\"']+)",
            "go.mod": r"(?m)^\s*module\s+(\S+)",
        }
        match = re.search(patterns.get(basename, r"(?!x)x"), content)
        if match:
            return match.group(1).rstrip("/").split("/")[-1]

    return PurePosixPath(directory).name if directory else repo.name


def _unique_artifact_name(base: str, directory: str, used: set[str]) -> str:
    """Return a stable artifact identity when manifests reuse a package name."""
    if base not in used:
        return base

    path_label = directory.replace("/", "-") or "root"
    candidate = f"{base}@{path_label}"
    suffix = 2
    while candidate in used:
        candidate = f"{base}@{path_label}-{suffix}"
        suffix += 1
    return candidate


def _files_under(paths: set[str], directory: str) -> set[str]:
    prefix = f"{directory}/" if directory else ""
    return {path for path in paths if path.startswith(prefix)}


def _infer_type(
    repo: RepoData,
    directory: str,
    manifests: list[str],
    all_paths: set[str],
    build_unit_dirs: set[str] | None = None,
) -> ArtifactType:
    subtree = _files_under(all_paths, directory)
    for child in build_unit_dirs or set():
        if child == directory:
            continue
        if directory and not child.startswith(f"{directory}/"):
            continue
        child_prefix = f"{child}/"
        subtree = {path for path in subtree if not path.startswith(child_prefix)}
    relative = {
        path[len(directory) + 1 :] if directory else path
        for path in subtree
    }
    contents = "\n".join(_content(repo, path).lower() for path in manifests)
    basenames = {PurePosixPath(path).name.lower() for path in manifests}

    package_path = next(
        (path for path in manifests if PurePosixPath(path).name.lower() == "package.json"),
        None,
    )
    package = _package_json(_content(repo, package_path)) if package_path else {}
    dependencies = {
        str(name).lower()
        for section in ("dependencies", "devDependencies", "peerDependencies")
        for name in (package.get(section) or {})
    }
    scripts = package.get("scripts") or {}

    if "electron" in dependencies or any("electron" in str(value).lower() for value in scripts.values()):
        return ArtifactType.DESKTOP_APPLICATION
    if "@tauri-apps/api" in dependencies or any("tauri" in path.lower() for path in subtree):
        return ArtifactType.DESKTOP_APPLICATION
    if "androidmanifest.xml" in {PurePosixPath(path).name.lower() for path in subtree}:
        return ArtifactType.MOBILE_APPLICATION
    if any(path.lower().endswith((".xcodeproj/project.pbxproj", "pubspec.yaml")) for path in subtree):
        return ArtifactType.MOBILE_APPLICATION
    if package.get("bin") or re.search(r"(?m)^\s*\[project\.scripts\]\s*$", contents):
        return ArtifactType.CLI_TOOL
    if dependencies & _WEB_DEPENDENCIES:
        return ArtifactType.WEB_APPLICATION
    if any("vite build" in str(value).lower() for value in scripts.values()):
        return ArtifactType.WEB_APPLICATION

    docker_here = "Dockerfile" in relative or "dockerfile" in {p.lower() for p in relative}
    has_main = any(
        PurePosixPath(path).name.lower()
        in {"main.py", "app.py", "main.go", "main.rs", "server.js", "server.ts"}
        for path in relative
    )
    server_signal = bool(
        dependencies & _NODE_SERVERS
        or any(server in contents for server in _PYTHON_SERVERS)
    )
    docker_content = "\n".join(
        _content(repo, path).lower()
        for path in subtree
        if PurePosixPath(path).name.lower() == "dockerfile"
    )
    daemon_entrypoint = bool(
        re.search(r"\b(cmd|entrypoint)\b", docker_content)
        and re.search(r"(server|serve|gunicorn|uvicorn|influxd|daemon)", docker_content)
    )
    if docker_here and (server_signal or has_main or daemon_entrypoint):
        return ArtifactType.DEPLOYABLE_SERVICE
    if server_signal and has_main:
        return ArtifactType.DEPLOYABLE_SERVICE

    if directory.lower().split("/")[-1] in {"docs", "documentation", "website"}:
        return ArtifactType.DOCUMENTATION
    if any(word in contents for word in ("pytest", "junit")):
        # Tests in a product manifest do not change the produced artifact.
        pass
    if "pom.xml" in basenames and "<packaging>pom</packaging>" not in contents:
        return ArtifactType.LIBRARY
    return ArtifactType.UNKNOWN


def _is_root_workspace(repo: RepoData, root_manifests: list[str], child_dirs: set[str]) -> bool:
    if not child_dirs:
        return False
    package_path = next(
        (path for path in root_manifests if PurePosixPath(path).name.lower() == "package.json"),
        None,
    )
    if package_path and _package_json(_content(repo, package_path)).get("workspaces"):
        return True

    for path in root_manifests:
        basename = PurePosixPath(path).name.lower()
        content = _content(repo, path).lower()
        if basename == "cargo.toml" and re.search(r"(?m)^\s*\[workspace\]\s*$", content):
            return True
        if basename == "pom.xml" and (
            "<packaging>pom</packaging>" in content or "<modules>" in content
        ):
            return True
    return False


def _independent_output_type(
    repo: RepoData,
    directory: str,
    manifests: list[str],
    all_paths: set[str],
    build_unit_dirs: set[str],
) -> ArtifactType | None:
    """Return a product output type, or None for an internal build module."""
    inferred = _infer_type(
        repo,
        directory,
        manifests,
        all_paths,
        build_unit_dirs,
    )
    if inferred not in {ArtifactType.UNKNOWN, ArtifactType.LIBRARY}:
        return inferred

    subtree = _files_under(all_paths, directory)
    relative = {
        path[len(directory) + 1 :] if directory else path
        for path in subtree
    }
    contents = "\n".join(_content(repo, path).lower() for path in manifests)
    basenames = {PurePosixPath(path).name.lower() for path in manifests}

    # Ecosystems only define how they declare an output. The promotion rule
    # itself remains ecosystem-independent.
    if "cargo.toml" in basenames and (
        "[[bin]]" in contents or "src/main.rs" in relative
    ):
        return ArtifactType.CLI_TOOL
    if basenames & {"build.gradle", "build.gradle.kts"} and (
        re.search(r"\b(application|org\.springframework\.boot)\b", contents)
        or re.search(r"\bmainclass(?:name)?\b", contents)
    ):
        return ArtifactType.DEPLOYABLE_SERVICE
    if "pom.xml" in basenames and (
        "spring-boot-maven-plugin" in contents
        or "<mainclass>" in contents
    ):
        return ArtifactType.DEPLOYABLE_SERVICE
    if "go.mod" in basenames and "main.go" in relative:
        return ArtifactType.CLI_TOOL
    return None


def _has_repo_deployment_entrypoint(repo: RepoData, all_paths: set[str]) -> bool:
    for path in all_paths:
        if PurePosixPath(path).name.lower() not in _DEPLOYMENT_DESCRIPTORS:
            continue
        content = _content(repo, path).lower()
        if re.search(r"\b(cmd|entrypoint|command)\b", content):
            return True
    return False


def classify_artifacts(repo: RepoData) -> RepositoryClassification:
    """Promote manifest build units only when they declare a product output."""
    all_paths = {_norm_path(path) for path in repo.file_tree}
    all_paths.update(_norm_path(path) for path in repo.file_contents)

    manifests_by_dir: dict[str, list[str]] = {}
    for path in sorted(all_paths):
        if PurePosixPath(path).name.lower() in _MANIFESTS:
            manifests_by_dir.setdefault(_directory(path), []).append(path)
    has_manifests = bool(manifests_by_dir)

    root_manifests = manifests_by_dir.get("", [])
    child_dirs = {directory for directory in manifests_by_dir if directory}
    root_is_aggregator = _is_root_workspace(repo, root_manifests, child_dirs)

    build_unit_dirs = sorted(
        (directory for directory in manifests_by_dir if directory or not root_is_aggregator),
        key=lambda directory: (directory != "", directory.count("/"), directory),
    )

    if not build_unit_dirs:
        build_unit_dirs = [""]
        manifests_by_dir[""] = []

    output_units = [
        (
            directory,
            _independent_output_type(
                repo,
                directory,
                manifests_by_dir[directory],
                all_paths,
                set(build_unit_dirs),
            ),
        )
        for directory in build_unit_dirs
    ]
    output_units = [
        (directory, artifact_type)
        for directory, artifact_type in output_units
        if artifact_type is not None
    ]

    # Collapse internal modules into a repository-wide floor. With one output,
    # this also makes sibling build-module dependencies unambiguously owned by
    # that output. With no output, the repository is a genuine library unit.
    if not output_units:
        artifact_dirs_and_types = [
            ("", ArtifactType.LIBRARY if has_manifests else ArtifactType.UNKNOWN)
        ]
        naming_directories = [""]
    elif len(output_units) == 1:
        directory, artifact_type = output_units[0]
        if (
            artifact_type == ArtifactType.CLI_TOOL
            and _has_repo_deployment_entrypoint(repo, all_paths)
        ):
            artifact_type = ArtifactType.DEPLOYABLE_SERVICE
        artifact_dirs_and_types = [("", artifact_type)]
        naming_directories = [directory]
    else:
        artifact_dirs_and_types = output_units
        naming_directories = [directory for directory, _ in output_units]

    artifacts: list[Artifact] = []
    used_names: set[str] = set()
    for index, (directory, artifact_type) in enumerate(artifact_dirs_and_types):
        naming_directory = naming_directories[index]
        manifests = manifests_by_dir.get(naming_directory, [])
        name = _unique_artifact_name(
            _manifest_name(repo, naming_directory, manifests),
            naming_directory,
            used_names,
        )
        used_names.add(name)
        artifacts.append(
            Artifact(
                name=name,
                type=artifact_type,
                path=_artifact_path(directory),
                primary=index == 0,
            )
        )

    # A bundled UI under a real root product is structurally subordinate. Do
    # not infer peer/service relationships among workspace members.
    root = next((artifact for artifact in artifacts if artifact.path == "/"), None)
    if root:
        for artifact in artifacts:
            if artifact is not root and artifact.type == ArtifactType.WEB_APPLICATION:
                artifact.subordinate_to = root.name

    count = ArtifactCount.SINGLE if len(artifacts) == 1 else ArtifactCount.MULTI
    return RepositoryClassification(artifact_count=count, artifacts=artifacts)


def artifact_for_manifest(
    manifest_path: str,
    classification: RepositoryClassification,
) -> str | None:
    """Resolve manifest ownership by the deepest matching artifact subtree.

    A root-level manifest in a multi-artifact repository is shared/ambiguous
    unless the root itself is an explicit artifact.
    """
    # With one produced unit, ownership is unambiguous even for technologies
    # detected from source/config signals rather than a manifest.
    if classification.artifact_count == ArtifactCount.SINGLE:
        return classification.artifacts[0].name

    path = _norm_path(manifest_path)
    if PurePosixPath(path).name.lower() not in _MANIFESTS:
        return None

    candidates: list[tuple[int, Artifact]] = []
    for artifact in classification.artifacts:
        directory = artifact.path.strip("/")
        if not directory:
            candidates.append((0, artifact))
            continue
        if path == directory or path.startswith(f"{directory}/"):
            candidates.append((len(directory.split("/")), artifact))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1].name


def _artifact_for_deployment_descriptor(
    path: str,
    classification: RepositoryClassification,
) -> str | None:
    """Resolve an artifact-local deployment descriptor without guessing.

    Unlike arbitrary source/config signals, a Dockerfile or deployment
    manifest inside an artifact subtree describes that produced unit. A
    root-level descriptor in a multi-artifact repository remains ambiguous.
    """
    normalized = _norm_path(path)
    if PurePosixPath(normalized).name.lower() not in _DEPLOYMENT_DESCRIPTORS:
        return None

    candidates = [
        artifact
        for artifact in classification.artifacts
        if artifact.path != "/"
        and normalized.startswith(f"{artifact.path.strip('/')}/")
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda artifact: len(artifact.path.strip("/").split("/")),
    ).name


def assign_artifact_ownership(
    technologies: list[DetectedTech],
    classification: RepositoryClassification,
) -> None:
    """Populate artifact ownership in place after detection has been merged."""
    for tech in technologies:
        matched_file = tech.matched_file or ""
        tech.belongs_to_artifact = (
            artifact_for_manifest(matched_file, classification)
            or _artifact_for_deployment_descriptor(matched_file, classification)
        )
