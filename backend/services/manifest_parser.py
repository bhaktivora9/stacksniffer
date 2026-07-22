"""
backend/services/manifest_parser.py

Phase 1 of the new detection pipeline: PURE STRUCTURAL EXTRACTION.

Responsibility:
  - Parse manifests (pyproject.toml, package.json, pom.xml, go.mod, etc.)
  - Extract raw dependency names with scope, origin, version_spec
  - Exclude self-references (repo declaring its own package as a dependency)
  - Return unclassified dep list for Phase 2 (Gemini classification)

What this file does NOT do:
  - Classify packages into tech categories (that is Phase 2 — Gemini)
  - Map package names to canonical tech names (Gemini knows psycopg2 = PostgreSQL)
  - Score confidence (Gemini assigns confidence based on package + scope + context)
  - Emit DetectedTech objects (Phase 2 output, not Phase 1)

Output of parse_manifest_dependencies():
  {
    "raw_deps": [
      {
        "name":         "fastapi",          # normalized package name
        "raw_name":     "fastapi>=0.95.0",  # original string from manifest
        "scope":        "required",         # required | optional | dev | test
        "origin":       "product",          # product | build | test | example
        "matched_file": "pyproject.toml",   # which manifest declared it
        "version_spec": ">=0.95.0",         # version constraint
      },
      ...
    ],
    "project_names":      {"fastapi"},           # repo's own package name(s)
    "manifests_selected": [{path, origin, parsed}],
    "flags":              ["PARTIAL_MANIFEST_COVERAGE"],
  }

Java equivalent: ContentAnalyzerService + PatternDetectionService (extraction only)
"""
from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# ── Manifest identification ───────────────────────────────────────────────────

MANIFEST_LIMIT = 25

_RECOGNIZED_EXACT = {
    "pyproject.toml", "setup.py", "setup.cfg", "package.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "composer.json",
}
_REQUIREMENTS_RE = re.compile(r"^requirements.*\.txt$", re.IGNORECASE)

# ── Origin classification ─────────────────────────────────────────────────────
# Determines whether a manifest belongs to the product, tests, build, or examples.
# "product" manifests feed Phase 2. Others are lower-confidence context.

_SOURCE_ROOTS  = {"packages", "libs", "crates", "src", "modules", "plugins"}
_EXAMPLE_ROOTS = {"examples", "example", "bench", "evals", "samples",
                  "templates", "docs_src", "demo"}
_TEST_ROOTS    = {"qa"}
_BUILD_ROOTS   = {"distribution", "docker", ".github", ".buildkite", "ci", "scripts"}

# ── Parsing regexes ───────────────────────────────────────────────────────────

_GRADLE_DEP_RE = re.compile(
    r"\b(implementation|api|testImplementation|compileOnly|runtimeOnly|testCompileOnly)\s+"
    r"['\"]([^:'\"]+):([^:'\"]+):?([^'\"]*)['\"]"
)
_DEP_NAME_RE      = re.compile(r"^\s*(@?[A-Za-z0-9_.@/\-]+)")
_VERSION_SPLIT_RE = re.compile(r"\s*(===|==|~=|!=|<=|>=|<|>|=)\s*")


# ── Self-reference detection ──────────────────────────────────────────────────

def _repo_names(repo_full_name: str) -> set[str]:
    """Extract normalized name tokens from repo path (e.g. tiangolo/fastapi → {fastapi, tiangolo})."""
    names = set()
    for part in repo_full_name.split("/"):
        norm = _normalize_name(part)
        if norm:
            names.add(norm)
    return names


def is_self_reference(package_name: str, self_names: set[str]) -> bool:
    """
    Return True if package_name refers to the repo being analyzed.
    Uses token-boundary matching — 'ray' must not match 'array'.

    package_name: normalized package name e.g. 'fastapi', 'langchain-core'
    self_names:   normalized repo path tokens + project name tokens
    """
    if not self_names or not package_name:
        return False

    normalized = package_name.lower().replace("_", "-").replace(" ", "-")

    # Exact match
    if normalized in self_names:
        return True

    # Token-level match — split on hyphens
    # "apache-kafka" ∩ {"apache","kafka"} → True
    # "array" ∩ {"ray"} → {} → False (correct)
    pkg_tokens = set(normalized.split("-"))
    for name in self_names:
        if not name:
            continue
        name_tokens = set(name.split("-"))
        if pkg_tokens & name_tokens:
            return True

    return False


def filter_self_references_from_inferences(
    ai_inferred_techs: list[dict],
    repo_full_name: str,
    parsed_project_names: set[str] = None,
) -> tuple[list[dict], list[str]]:
    """
    Remove self-referencing techs from Gemini ai_inferred_techs.

    Gemini re-injects self-references (e.g. HuggingFace for huggingface/transformers)
    that Phase 1 already excluded. Called in analyze.py after run_full_ai_pipeline().

    Returns (filtered_inferences, excluded_names).
    """
    repo_names = _repo_names(repo_full_name)
    self_names = repo_names | {n for n in (parsed_project_names or set()) if n}

    filtered: list[dict] = []
    excluded: list[str]  = []

    for tech in ai_inferred_techs:
        name = tech.get("tech") or tech.get("name", "")
        if is_self_reference(name, self_names):
            excluded.append(name)
        else:
            filtered.append(tech)

    return filtered, excluded


# ── Manifest selection ────────────────────────────────────────────────────────

def is_recognized_manifest(path: str) -> bool:
    basename = Path(path).name
    return basename in _RECOGNIZED_EXACT or bool(_REQUIREMENTS_RE.match(basename))


def manifest_origin(path: str) -> str:
    """Classify a manifest path as product | build | test | example."""
    parts  = Path(path).as_posix().split("/")
    lowered = [p.lower() for p in parts]
    first   = lowered[0] if lowered else ""
    joined  = "/".join(lowered)

    if first in _BUILD_ROOTS:
        return "build"
    if (first in _TEST_ROOTS
            or first.startswith("test")
            or "fixture" in joined
            or "testfixtures" in joined):
        return "test"
    if first in _EXAMPLE_ROOTS or first.startswith("benchmark"):
        return "example"
    if len(parts) == 1 or first in _SOURCE_ROOTS:
        return "product"
    return "product"


def select_manifests(
    file_tree: list[str],
    limit: int = MANIFEST_LIMIT,
) -> tuple[list[dict], list[str]]:
    """
    Select manifests to parse from the file tree.
    Prioritises product manifests (root + one per top-level dir).
    Returns (manifest_list, flags).
    """
    manifests = [
        {"path": path, "origin": manifest_origin(path), "parsed": False}
        for path in file_tree
        if is_recognized_manifest(path)
    ]
    product  = [m for m in manifests if m["origin"] == "product"]
    selected = product
    flags: list[str] = []

    if len(product) > limit:
        flags.append("PARTIAL_MANIFEST_COVERAGE")
        root   = [m for m in product if "/" not in m["path"]]
        by_top: dict[str, dict] = {}
        for item in product:
            parts = item["path"].split("/")
            if len(parts) > 1 and parts[0] not in by_top:
                by_top[parts[0]] = item
        selected_paths = {m["path"] for m in (root + list(by_top.values()))[:limit]}
        selected = [m for m in product if m["path"] in selected_paths]

    selected_paths = {m["path"] for m in selected}
    for item in manifests:
        item["parsed"] = item["path"] in selected_paths

    return manifests, flags


def selected_product_manifest_paths(
    file_tree: list[str],
    limit: int = MANIFEST_LIMIT,
) -> list[str]:
    manifests, _ = select_manifests(file_tree, limit)
    return [m["path"] for m in manifests if m["origin"] == "product" and m["parsed"]]


# ── Main extraction function ──────────────────────────────────────────────────

def parse_manifest_dependencies(
    file_contents: dict[str, str],
    file_tree: list[str],
    repo_full_name: str = "",
) -> dict:
    """
    Phase 1: Pure structural extraction.

    Parses all recognized manifests and returns a flat list of raw dependencies
    with scope, origin, and source file. No classification, no canonical names,
    no DetectedTech objects.

    The output feeds Phase 2 (classify_dependencies in ai_pipeline.py) where
    Gemini maps package names to tech categories.

    Returns:
      raw_deps:           list of dep dicts (name, scope, origin, matched_file, version_spec)
      project_names:      set of this repo's own package names (for self-ref exclusion)
      manifests_selected: list of {path, origin, parsed} for UI audit
      flags:              parse warnings (MANIFEST_PARSE_FAILED, PARTIAL_MANIFEST_COVERAGE, etc.)
    """
    manifests_selected, flags = select_manifests(file_tree)
    selected_paths = {
        m["path"] for m in manifests_selected
        if m["origin"] == "product" and m["parsed"]
    }

    parsed_project_names: set[str] = set()
    raw_deps_all: list[dict] = []

    for path in selected_paths:
        content = file_contents.get(path)
        if content is None:
            # Try loose key match (path may differ in prefix)
            for key, value in file_contents.items():
                if key == path:
                    content = value
                    break
        if content is None:
            continue

        deps, project_names, parse_flags = _parse_by_type(path, content)
        parsed_project_names.update(
            _normalize_name(name) for name in project_names if name
        )
        flags.extend(parse_flags)

        for dep in deps:
            raw_deps_all.append({
                **dep,
                "origin":       "product",
                "matched_file": path,
            })

    # Self-reference exclusion
    repo_names = _repo_names(repo_full_name)
    self_names = {n for n in parsed_project_names if n} | repo_names

    excluded_self_refs: list[str] = []
    raw_deps: list[dict] = []

    for dep in raw_deps_all:
        pkg = dep.get("name", "")
        if not pkg:
            continue
        if _is_internal_spec(dep.get("version_spec", "")):
            continue
        if is_self_reference(pkg, self_names):
            excluded_self_refs.append(pkg)
            continue
        raw_deps.append(dep)

    return {
        "raw_deps":           raw_deps,
        "project_names":      parsed_project_names,
        "manifests_selected": manifests_selected,
        "flags": sorted(set([
            *flags,
            *[f"SELF_REFERENCE_EXCLUDED:{name}" for name in excluded_self_refs],
        ])),
    }


# ── Parser implementations ────────────────────────────────────────────────────

def _parse_by_type(
    path: str,
    content: str,
) -> tuple[list[dict], set[str], list[str]]:
    basename = Path(path).name
    if basename == "pyproject.toml":     return _parse_pyproject(content)
    if basename == "package.json":       return _parse_package_json(content)
    if basename == "Cargo.toml":         return _parse_cargo_toml(content)
    if basename == "go.mod":             return _parse_go_mod(content)
    if basename == "pom.xml":            return _parse_pom(content)
    if basename in ("build.gradle", "build.gradle.kts"):
                                         return _parse_gradle(content)
    if _REQUIREMENTS_RE.match(basename): return _parse_requirements(path, content)
    if basename == "setup.py":           return _parse_setup_py(content)
    if basename == "setup.cfg":          return _parse_setup_cfg(content)
    if basename == "Gemfile":            return _parse_gemfile(content)
    if basename == "composer.json":      return _parse_composer_json(content)
    return [], set(), []


def _parse_pyproject(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    deps: list[dict] = []
    project = data.get("project", {})
    project_names = {project.get("name", "")}

    # [project] dependencies → required
    deps.extend(_dep_strings(project.get("dependencies", []), "required"))

    # [project.optional-dependencies] → optional or test/dev by group name
    for group, values in project.get("optional-dependencies", {}).items():
        scope = _group_scope(group, default="optional")
        deps.extend(_dep_strings(values, scope))

    # [dependency-groups] (uv / PEP 735) — include-group resolved
    dependency_groups = data.get("dependency-groups", {})
    for group in dependency_groups:
        deps.extend(_pyproject_group_values(
            dependency_groups,
            group,
            _group_scope(group, default="dev"),
            seen=set(),
        ))

    # [tool.poetry]
    poetry = data.get("tool", {}).get("poetry", {})
    if poetry.get("name"):
        project_names.add(poetry["name"])
    deps.extend(_poetry_deps(poetry.get("dependencies", {}),     "required"))
    deps.extend(_poetry_deps(poetry.get("dev-dependencies", {}), "dev"))
    for group_data in poetry.get("group", {}).values():
        group_deps = group_data.get("dependencies", {})
        deps.extend(_poetry_deps(group_deps, "dev"))

    return deps, project_names, []


def _pyproject_group_values(
    groups: dict,
    group: str,
    scope: str,
    seen: set[str],
) -> list[dict]:
    deps: list[dict] = []
    if group in seen:
        return deps
    seen.add(group)
    values = groups.get(group, [])
    if not isinstance(values, list):
        return deps
    for item in values:
        if isinstance(item, str):
            deps.append(_dep(item, scope))
        elif isinstance(item, dict) and "include-group" in item:
            deps.extend(_pyproject_group_values(
                groups,
                str(item["include-group"]),
                scope,
                seen,
            ))
    return deps


def _parse_package_json(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    deps: list[dict] = []
    for key, scope in [
        ("dependencies",         "required"),
        ("peerDependencies",     "required"),
        ("optionalDependencies", "optional"),
        ("devDependencies",      "dev"),
    ]:
        for name, spec in (data.get(key) or {}).items():
            if _is_internal_spec(str(spec)):
                continue
            deps.append({"name": name, "raw_name": name,
                         "scope": scope, "version_spec": str(spec)})

    return deps, {data.get("name", "")}, []


def _parse_cargo_toml(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    deps: list[dict] = []
    for key, scope in [
        ("dependencies",       "required"),
        ("dev-dependencies",   "test"),
        ("build-dependencies", "dev"),
    ]:
        deps.extend(_toml_table_deps(data.get(key, {}), scope))

    return deps, {data.get("package", {}).get("name", "")}, []


def _parse_go_mod(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps: list[dict] = []
    project_names: set[str] = set()
    in_require = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            project_names.add(stripped.split()[1].split("/")[-1])
        if stripped.startswith("require ("):
            in_require = True
            continue
        if in_require and stripped == ")":
            in_require = False
            continue
        if in_require or stripped.startswith("require "):
            raw   = stripped.replace("require ", "", 1).split("//")[0].strip()
            parts = raw.split()
            if len(parts) >= 2:
                scope = "optional" if "// indirect" in line else "required"
                deps.append({
                    "name":         parts[0],
                    "raw_name":     parts[0],
                    "scope":        scope,
                    "version_spec": parts[1],
                })

    return deps, project_names, []


def _parse_pom(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    ns     = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    prefix = "m:" if ns else ""
    project_names = {
        (root.findtext(f"{prefix}artifactId", default="", namespaces=ns) or "").strip()
    }
    deps: list[dict] = []

    for node in root.findall(f".//{prefix}dependency", ns):
        group    = (node.findtext(f"{prefix}groupId",    default="", namespaces=ns) or "").strip()
        artifact = (node.findtext(f"{prefix}artifactId", default="", namespaces=ns) or "").strip()
        scope_v  = (node.findtext(f"{prefix}scope",      default="", namespaces=ns) or "").strip().lower()
        version  = (node.findtext(f"{prefix}version",    default="", namespaces=ns) or "").strip()
        if not artifact:
            continue
        scope    = ("test" if scope_v == "test"
                    else "dev" if scope_v in {"provided", "system"}
                    else "required")
        raw_name = f"{group}:{artifact}" if group else artifact
        deps.append({
            "name":         raw_name,   # group:artifact — _normalize_name takes group
            "raw_name":     raw_name,
            "scope":        scope,
            "version_spec": version,
        })

    return deps, project_names, []


def _parse_gradle(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps: list[dict] = []
    for conf, group, artifact, version in _GRADLE_DEP_RE.findall(content):
        scope    = ("test"     if conf.lower().startswith("test")
                    else "dev" if conf == "compileOnly"
                    else "required")
        raw_name = f"{group}:{artifact}"
        deps.append({
            "name":         raw_name,
            "raw_name":     raw_name,
            "scope":        scope,
            "version_spec": version,
        })
    flags = ["HEURISTIC_MANIFEST_PARSE"] if deps else []
    return deps, set(), flags


def _parse_requirements(path: str, content: str) -> tuple[list[dict], set[str], list[str]]:
    basename = Path(path).name.lower()
    scope    = ("dev"  if any(w in basename for w in ("dev", "lint", "docs", "build"))
                else "test" if "test" in basename
                else "required")
    deps: list[dict] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "--")):
            continue
        deps.append(_dep(stripped.split("#", 1)[0].strip(), scope))
    return deps, set(), []


def _parse_setup_py(content: str) -> tuple[list[dict], set[str], list[str]]:
    flags: list[str]  = []
    deps: list[dict]  = []
    project_names: set[str] = set()
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    variables = _static_assignments(tree, flags)
    for call in [node for node in ast.walk(tree) if isinstance(node, ast.Call)]:
        func_name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")
        if func_name != "setup":
            continue
        for kw in call.keywords:
            if kw.arg == "name":
                name = _literal_string(kw.value, variables)
                if name:
                    project_names.add(name)
            elif kw.arg == "install_requires":
                deps.extend(_dep_strings(
                    _literal_list(kw.value, variables, flags), "required"
                ))
            elif kw.arg == "extras_require":
                extras = _literal_dict(kw.value, variables, flags)
                for group, values in extras.items():
                    deps.extend(_dep_strings(
                        values, _group_scope(group, default="optional")
                    ))

    return deps, project_names, sorted(set(flags))


def _parse_setup_cfg(content: str) -> tuple[list[dict], set[str], list[str]]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(content)
    except configparser.Error:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    deps: list[dict] = []
    project_names    = {parser.get("metadata", "name", fallback="")}

    if parser.has_option("options", "install_requires"):
        deps.extend(_dep_strings(
            parser.get("options", "install_requires").splitlines(), "required"
        ))
    if parser.has_section("options.extras_require"):
        for group, value in parser.items("options.extras_require"):
            deps.extend(_dep_strings(
                value.splitlines(), _group_scope(group, default="optional")
            ))

    return deps, project_names, []


def _parse_gemfile(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps: list[dict] = []
    for line in content.splitlines():
        m = re.match(
            r"\s*gem\s+['\"]([^'\"]+)['\"](?:,\s*['\"]([^'\"]+)['\"])?", line
        )
        if m:
            deps.append({
                "name":         m.group(1),
                "raw_name":     m.group(1),
                "scope":        "required",
                "version_spec": m.group(2) or "",
            })
    return deps, set(), []


def _parse_composer_json(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]

    deps: list[dict] = []
    for key, scope in [("require", "required"), ("require-dev", "dev")]:
        for name, spec in (data.get(key) or {}).items():
            if name.lower() == "php":
                continue
            deps.append({
                "name":         name,
                "raw_name":     name,
                "scope":        scope,
                "version_spec": str(spec),
            })

    return deps, {data.get("name", "").split("/")[-1]}, []


# ── Helper utilities ──────────────────────────────────────────────────────────

def _static_assignments(tree: ast.AST, flags: list[str]) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in (tree.body if isinstance(tree, ast.Module) else []):
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                if isinstance(node.value, (ast.DictComp, ast.ListComp, ast.Call)):
                    flags.append("DYNAMIC_DEPS_UNPARSED")
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
    return values


def _literal_string(node: ast.AST, variables: dict[str, object]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and isinstance(variables.get(node.id), str):
        return variables[node.id]
    return ""


def _literal_list(
    node: ast.AST,
    variables: dict[str, object],
    flags: list[str],
) -> list[str]:
    if isinstance(node, ast.Name):
        value = variables.get(node.id)
        return value if isinstance(value, list) else []
    try:
        value = ast.literal_eval(node)
    except Exception:
        flags.append("DYNAMIC_DEPS_UNPARSED")
        return []
    return value if isinstance(value, list) else []


def _literal_dict(
    node: ast.AST,
    variables: dict[str, object],
    flags: list[str],
) -> dict:
    if isinstance(node, ast.Name):
        value = variables.get(node.id)
        return value if isinstance(value, dict) else {}
    try:
        value = ast.literal_eval(node)
    except Exception:
        flags.append("DYNAMIC_DEPS_UNPARSED")
        return {}
    return value if isinstance(value, dict) else {}


def _dep_strings(values: object, scope: str) -> list[dict]:
    if not isinstance(values, list):
        return []
    return [_dep(v, scope) for v in values if isinstance(v, str)]


def _poetry_deps(values: object, scope: str) -> list[dict]:
    if not isinstance(values, dict):
        return []
    return [
        {"name": name, "raw_name": name, "scope": scope, "version_spec": str(spec)}
        for name, spec in values.items()
        if name.lower() != "python"
    ]


def _toml_table_deps(values: object, scope: str) -> list[dict]:
    if not isinstance(values, dict):
        return []
    return [
        {"name": name, "raw_name": name, "scope": scope, "version_spec": str(spec)}
        for name, spec in values.items()
        if not (isinstance(spec, dict) and ("path" in spec or "git" in spec))
    ]


def _dep(value: str, scope: str) -> dict:
    """Parse a raw dep string into {name, raw_name, scope, version_spec}."""
    cleaned = value.strip().strip(",")
    name    = _normalize_name(cleaned)
    return {"name": name, "raw_name": cleaned, "scope": scope, "version_spec": cleaned}


def _group_scope(group: str, default: str) -> str:
    lower = group.lower()
    if any(t in lower for t in ("test", "tests")):
        return "test"
    if any(t in lower for t in ("dev", "doc", "docs", "lint", "build", "bench")):
        return "dev"
    return default


def _normalize_name(value: str) -> str:
    """
    Normalize a raw package name to a stable lookup key.

    - Strip version specifiers, extras, env markers
    - Lowercase, replace _ with -
    - Preserve @ prefix for scoped npm: @nestjs/core → @nestjs/core
    - For Java group:artifact[:version] → take GROUP (first colon segment)
      Rationale: group identifies the library family for Gemini classification.
      org.springframework.boot:spring-boot-starter-web → org.springframework.boot
    """
    text = value.strip()
    text = text.split(";", 1)[0].strip()    # strip env markers
    text = text.split("[", 1)[0].strip()    # strip extras
    text = _VERSION_SPLIT_RE.split(text, 1)[0].strip()  # strip version

    if not text:
        return ""

    # Java group:artifact[:version] → take group only
    if ":" in text and not text.startswith("@"):
        return text.split(":")[0].lower().replace("_", "-")

    # Standard package name (including scoped npm @scope/pkg)
    match = _DEP_NAME_RE.match(text)
    if not match:
        return ""
    return match.group(1).lower().replace("_", "-")


def _is_internal_spec(spec: str) -> bool:
    lower = str(spec).strip().lower()
    return lower.startswith(("workspace:", "file:", "link:"))