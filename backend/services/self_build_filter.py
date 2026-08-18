"""Remove a repository's own build-logic modules from extracted dependencies."""

from __future__ import annotations

_NAME_SIGNALS = (
    "build-tools", "build-conventions", "build-logic", "buildsrc",
    "-conventions", "gradle-plugin", "gradle-runner", "reaper", "internal-test",
)
_PATH_SIGNALS = (
    "build-tools", "build-conventions", "build-logic", "buildsrc", "/gradle/",
)


def _split_coordinate(name: str) -> tuple[str, str]:
    if ":" not in name:
        return "", name
    group, _, artifact = name.rpartition(":")
    return group, artifact


def _norm_namespace(namespace: str) -> str:
    parts = namespace.lower().strip().split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else namespace.lower().strip()


def derive_repo_namespaces(
    root_group: str | None, repo_full_name: str | None,
) -> set[str]:
    """Return the union of declared and repository-derived namespaces.

    A declared Gradle group is useful evidence, but it is not always a Maven
    namespace (large builds may use a short/internal value). It must therefore
    not suppress the owner/name fallback.
    """
    namespaces: set[str] = set()
    if root_group:
        namespaces.add(_norm_namespace(root_group))
    if repo_full_name and "/" in repo_full_name:
        owner, name = repo_full_name.split("/", 1)
        name = name.lower()
        namespaces.update(f"{tld}.{name}" for tld in ("org", "com", "io", "net"))
        namespaces.add(_norm_namespace(f"{owner.lower()}.{name}"))
    return {namespace for namespace in namespaces if namespace}


def _get(dep, key: str):
    return dep.get(key) if isinstance(dep, dict) else getattr(dep, key, None)


def is_self_build_module(dep, repo_namespaces: set[str]) -> bool:
    """Require both an own-namespace match and a build-logic signal."""
    group, artifact = _split_coordinate(_get(dep, "name") or "")
    if not group or _norm_namespace(group) not in repo_namespaces:
        return False
    coordinate = f"{group} {artifact}".lower()
    matched_file = (_get(dep, "matched_file") or "").lower()
    return (
        any(signal in coordinate for signal in _NAME_SIGNALS)
        or any(signal in matched_file for signal in _PATH_SIGNALS)
    )


def filter_self_build_modules(
    deps: list, *, root_group: str | None, repo_full_name: str | None, on_drop=None,
) -> list:
    """Return dependencies with the repository's own build tooling removed."""
    namespaces = derive_repo_namespaces(root_group, repo_full_name)
    if not namespaces:
        return deps
    kept = []
    for dep in deps:
        if is_self_build_module(dep, namespaces):
            if on_drop is not None:
                on_drop(dep)
        else:
            kept.append(dep)
    return kept
