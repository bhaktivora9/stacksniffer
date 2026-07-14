from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from backend.models.schemas import DetectedTech, PatternMatch

MANIFEST_LIMIT = 25

_SOURCE_ROOTS = {"packages", "libs", "crates", "src", "modules", "plugins"}
_EXAMPLE_ROOTS = {
    "examples", "example", "bench", "evals", "samples", "templates",
    "docs_src", "demo",
}
_TEST_ROOTS = {"qa"}
_BUILD_ROOTS = {"distribution", "docker", ".github", ".buildkite", "ci", "scripts"}

_RECOGNIZED_EXACT = {
    "pyproject.toml", "setup.py", "setup.cfg", "package.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "composer.json",
}
_REQUIREMENTS_RE = re.compile(r"^requirements.*\.txt$", re.IGNORECASE)
_GRADLE_DEP_RE = re.compile(
    r"\b(implementation|api|testImplementation|compileOnly|runtimeOnly|testCompileOnly)\s+"
    r"['\"]([^:'\"]+):([^:'\"]+):?([^'\"]*)['\"]"
)
_DEP_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.@/\-]+)")
_VERSION_SPLIT_RE = re.compile(r"\s*(===|==|~=|!=|<=|>=|<|>|=|@)\s*")

_CANONICAL = {
    "fastapi": ("FastAPI", "frameworks"),
    "starlette": ("Starlette", "frameworks"),
    "pydantic": ("Pydantic", "library"),
    "django": ("Django", "frameworks"),
    "flask": ("Flask", "frameworks"),
    "react": ("React", "frameworks"),
    "next": ("Next.js", "frameworks"),
    "next.js": ("Next.js", "frameworks"),
    "vue": ("Vue", "frameworks"),
    "express": ("Express", "frameworks"),
    "@nestjs/core": ("NestJS", "frameworks"),
    "@nestjs/common": ("NestJS", "frameworks"),
    "spring-boot-starter": ("Spring Boot", "frameworks"),
    "spring-boot-starter-web": ("Spring Boot", "frameworks"),
    "org.springframework.boot": ("Spring Boot", "frameworks"),
    "torch": ("PyTorch", "ai_ml"),
    "torchvision": ("PyTorch", "ai_ml"),
    "torchaudio": ("PyTorch", "ai_ml"),
    "tensorflow": ("TensorFlow", "ai_ml"),
    "transformers": ("HuggingFace", "ai_ml"),
    "huggingface-hub": ("HuggingFace", "ai_ml"),
    "langchain": ("LangChain", "ai_ml"),
    "langchain-core": ("LangChain", "ai_ml"),
    "llama-index": ("LlamaIndex", "ai_ml"),
    "openai": ("OpenAI", "ai_ml"),
    "anthropic": ("Anthropic Claude", "ai_ml"),
    "google-generativeai": ("Gemini", "ai_ml"),
    "psycopg": ("PostgreSQL", "databases"),
    "psycopg2": ("PostgreSQL", "databases"),
    "asyncpg": ("PostgreSQL", "databases"),
    "pg": ("PostgreSQL", "databases"),
    "org.postgresql": ("PostgreSQL", "databases"),
    "pymongo": ("MongoDB", "databases"),
    "motor": ("MongoDB", "databases"),
    "mongoose": ("MongoDB", "databases"),
    "redis": ("Redis", "databases"),
    "ioredis": ("Redis", "databases"),
    "elasticsearch": ("Elasticsearch", "databases"),
    "mysql": ("MySQL", "databases"),
    "mysqlclient": ("MySQL", "databases"),
    "sqlite3": ("SQLite", "databases"),
    "chromadb": ("ChromaDB", "databases"),
    "weaviate-client": ("Weaviate", "databases"),
    "pinecone": ("Pinecone", "databases"),
    "kafka-python": ("Apache Kafka", "messaging"),
    "confluent-kafka": ("Apache Kafka", "messaging"),
    "org.apache.kafka": ("Apache Kafka", "messaging"),
    "pika": ("RabbitMQ", "messaging"),
    "celery": ("Celery", "messaging"),
    "pytest": ("pytest", "testing"),
    "jest": ("Jest", "testing"),
    "@playwright/test": ("Playwright", "testing"),
    "vitest": ("Vitest", "testing"),
    "junit": ("JUnit", "testing"),
    "junit-jupiter": ("JUnit", "testing"),
    "uvicorn": ("Uvicorn", "infra"),
    "sqlmodel": ("SQLModel", "library"),
    "black": ("Black", "testing"),
    "ruff": ("Ruff", "testing"),
    "mypy": ("mypy", "testing"),
    "pytest-asyncio": ("pytest", "testing"),
    "pytest-cov": ("pytest", "testing"),
    "pytest-xdist": ("pytest", "testing"),
    "pytest-timeout": ("pytest", "testing"),
    "pytest-sugar": ("pytest", "testing"),
}

_CANONICAL_PREFIXES = {
    "langchain-": ("LangChain", "ai_ml"),
    "llama-index-": ("LlamaIndex", "ai_ml"),
    "pytest-": ("pytest", "testing"),
    "jest-": ("Jest", "testing"),
    "@jest/": ("Jest", "testing"),
    "eslint-": ("ESLint", "testing"),
    "@eslint/": ("ESLint", "testing"),
}


def is_recognized_manifest(path: str) -> bool:
    basename = Path(path).name
    return basename in _RECOGNIZED_EXACT or bool(_REQUIREMENTS_RE.match(basename))


def manifest_origin(path: str) -> str:
    parts = Path(path).as_posix().split("/")
    lowered = [p.lower() for p in parts]
    first = lowered[0] if lowered else ""
    joined = "/".join(lowered)

    if first in _BUILD_ROOTS:
        return "build"
    if first in _TEST_ROOTS or first.startswith("test") or "fixture" in joined or "testfixtures" in joined:
        return "test"
    if first in _EXAMPLE_ROOTS or first.startswith("benchmark"):
        return "example"
    if len(parts) == 1 or first in _SOURCE_ROOTS:
        return "product"
    return "product"


def select_manifests(file_tree: list[str], limit: int = MANIFEST_LIMIT) -> tuple[list[dict], list[str]]:
    manifests = [
        {"path": path, "origin": manifest_origin(path), "parsed": False}
        for path in file_tree
        if is_recognized_manifest(path)
    ]
    product = [m for m in manifests if m["origin"] == "product"]
    selected = product
    flags: list[str] = []

    if len(product) > limit:
        flags.append("PARTIAL_MANIFEST_COVERAGE")
        root = [m for m in product if "/" not in m["path"]]
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


def selected_product_manifest_paths(file_tree: list[str], limit: int = MANIFEST_LIMIT) -> list[str]:
    manifests, _ = select_manifests(file_tree, limit)
    return [m["path"] for m in manifests if m["origin"] == "product" and m["parsed"]]


def parse_manifest_dependencies(
    file_contents: dict[str, str],
    file_tree: list[str],
    repo_full_name: str = "",
) -> dict:
    manifests_selected, flags = select_manifests(file_tree)
    selected_paths = {
        m["path"] for m in manifests_selected
        if m["origin"] == "product" and m["parsed"]
    }
    parsed_project_names: set[str] = set()
    raw_deps: list[dict] = []

    for path in selected_paths:
        content = file_contents.get(path)
        if content is None:
            for key, value in file_contents.items():
                if key == path:
                    content = value
                    break
        if content is None:
            continue

        deps, project_names, parse_flags = _parse_by_type(path, content)
        parsed_project_names.update(_normalize_name(name) for name in project_names if name)
        flags.extend(parse_flags)
        raw_deps.extend({**dep, "origin": "product", "matched_file": path} for dep in deps)

    repo_names = _repo_names(repo_full_name)
    self_names = {name for name in parsed_project_names if name} | repo_names
    aggregated: dict[str, dict] = {}
    frequency: dict[str, set[str]] = defaultdict(set)
    matched_keywords: dict[str, set[str]] = defaultdict(set)
    excluded_self_refs: list[str] = []

    for dep in raw_deps:
        original_name = dep.get("raw_name") or dep["name"]
        original_normalized = _normalize_name(original_name)
        if not original_normalized:
            continue
        if _is_internal_spec(dep.get("version_spec", "")):
            continue

        normalized = _normalize_name(dep["name"])
        if not normalized:
            continue
        canonical_name, category = _canonicalize(normalized)
        confidence = _confidence(dep["scope"], dep["origin"])
        if _is_self_dependency(canonical_name, self_names):
            excluded_self_refs.append(canonical_name)
            continue
        if category == "library" and canonical_name == normalized and confidence < 0.80:
            continue

        aggregate_key = canonical_name.lower()
        candidate = {
            "normalized": normalized,
            "name": canonical_name,
            "category": category,
            "scope": dep["scope"],
            "origin": dep["origin"],
            "matched_file": dep["matched_file"],
            "version_spec": dep.get("version_spec", ""),
            "confidence": confidence,
        }
        frequency[aggregate_key].add(dep["matched_file"])
        matched_keywords[aggregate_key].add(original_normalized)
        if aggregate_key not in aggregated or confidence > aggregated[aggregate_key]["confidence"]:
            aggregated[aggregate_key] = candidate

    detections: dict[str, list[DetectedTech]] = defaultdict(list)
    matches: list[PatternMatch] = []
    for aggregate_key, dep in aggregated.items():
        manifest_frequency = len(frequency[aggregate_key])
        keyword_list = ", ".join(sorted(matched_keywords[aggregate_key]))
        tech = DetectedTech(
            name=dep["name"],
            confidence=dep["confidence"],
            detection_source="manifest",
            category=dep["category"],
            scope=dep["scope"],
            origin=dep["origin"],
            matched_file=dep["matched_file"],
            version_spec=dep["version_spec"],
            manifest_frequency=manifest_frequency,
        )
        detections[dep["category"]].append(tech)
        matches.append(PatternMatch(
            tech=dep["name"],
            category=dep["category"],
            matched_file=dep["matched_file"],
            matched_keyword=keyword_list,
            confidence=dep["confidence"],
            scope=dep["scope"],
            origin=dep["origin"],
            version_spec=dep["version_spec"],
            manifest_frequency=manifest_frequency,
        ))

    return {
        "detections": dict(detections),
        "pattern_matches": matches,
        "manifests_selected": manifests_selected,
        "flags": sorted(set([
            *flags,
            *[f"SELF_REFERENCE_EXCLUDED:{name}" for name in excluded_self_refs],
        ])),
    }


def _parse_by_type(path: str, content: str) -> tuple[list[dict], set[str], list[str]]:
    basename = Path(path).name
    if basename == "pyproject.toml":
        return _parse_pyproject(content)
    if basename == "package.json":
        return _parse_package_json(content)
    if basename == "Cargo.toml":
        return _parse_cargo_toml(content)
    if basename == "go.mod":
        return _parse_go_mod(content)
    if basename == "pom.xml":
        return _parse_pom(content)
    if basename in ("build.gradle", "build.gradle.kts"):
        return _parse_gradle(content)
    if _REQUIREMENTS_RE.match(basename):
        return _parse_requirements(path, content)
    if basename == "setup.py":
        return _parse_setup_py(content)
    if basename == "setup.cfg":
        return _parse_setup_cfg(content)
    if basename == "Gemfile":
        return _parse_gemfile(content)
    if basename == "composer.json":
        return _parse_composer_json(content)
    return [], set(), []


def _parse_pyproject(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]
    deps = []
    project = data.get("project", {})
    project_names = {project.get("name", "")}
    deps.extend(_dep_strings(project.get("dependencies", []), "required"))
    for group, values in project.get("optional-dependencies", {}).items():
        scope = _group_scope(group, default="optional")
        deps.extend(_dep_strings(values, scope))
    dependency_groups = data.get("dependency-groups", {})
    for group in dependency_groups:
        deps.extend(_pyproject_group_values(
            dependency_groups,
            group,
            _group_scope(group, default="dev"),
            seen=set(),
        ))
    poetry = data.get("tool", {}).get("poetry", {})
    if poetry.get("name"):
        project_names.add(poetry["name"])
    deps.extend(_poetry_deps(poetry.get("dependencies", {}), "required"))
    deps.extend(_poetry_deps(poetry.get("dev-dependencies", {}), "dev"))
    return deps, project_names, []


def _pyproject_group_values(groups: dict, group: str, scope: str, seen: set[str]) -> list[dict]:
    deps = []
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
    deps = []
    for key, scope in [
        ("dependencies", "required"),
        ("peerDependencies", "required"),
        ("optionalDependencies", "optional"),
        ("devDependencies", "dev"),
    ]:
        for name, spec in (data.get(key) or {}).items():
            if _is_internal_spec(str(spec)):
                continue
            deps.append({"name": name, "scope": scope, "version_spec": str(spec)})
    return deps, {data.get("name", "")}, []


def _parse_cargo_toml(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]
    deps = []
    for key, scope in [
        ("dependencies", "required"),
        ("dev-dependencies", "test"),
        ("build-dependencies", "dev"),
    ]:
        deps.extend(_toml_table_deps(data.get(key, {}), scope))
    return deps, {data.get("package", {}).get("name", "")}, []


def _parse_go_mod(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps = []
    project_names = set()
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
            raw = stripped.replace("require ", "", 1).split("//")[0].strip()
            parts = raw.split()
            if len(parts) >= 2:
                deps.append({
                    "name": parts[0],
                    "scope": "optional" if "// indirect" in line else "required",
                    "version_spec": parts[1],
                })
    return deps, project_names, []


def _parse_pom(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]
    ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    prefix = "m:" if ns else ""
    project_names = {
        (root.findtext(f"{prefix}artifactId", default="", namespaces=ns) or "").strip()
    }
    for node in root.findall(f".//{prefix}dependency", ns):
        group = (node.findtext(f"{prefix}groupId", default="", namespaces=ns) or "").strip()
        artifact = (node.findtext(f"{prefix}artifactId", default="", namespaces=ns) or "").strip()
        scope_value = (node.findtext(f"{prefix}scope", default="", namespaces=ns) or "").strip().lower()
        version = (node.findtext(f"{prefix}version", default="", namespaces=ns) or "").strip()
        if not artifact:
            continue
        scope = "test" if scope_value == "test" else "dev" if scope_value in {"provided", "system"} else "required"
        deps.append({"name": f"{group}:{artifact}" if group else artifact, "scope": scope, "version_spec": version})
    return deps, project_names, []


def _parse_gradle(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps = []
    for conf, group, artifact, version in _GRADLE_DEP_RE.findall(content):
        scope = "test" if conf.lower().startswith("test") else "dev" if conf == "compileOnly" else "required"
        deps.append({"name": f"{group}:{artifact}", "scope": scope, "version_spec": version})
    return deps, set(), ["HEURISTIC_MANIFEST_PARSE"] if deps else []


def _parse_requirements(path: str, content: str) -> tuple[list[dict], set[str], list[str]]:
    basename = Path(path).name.lower()
    scope = "dev" if any(word in basename for word in ("dev", "lint", "docs", "build")) else "test" if "test" in basename else "required"
    deps = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(("-", "--")):
            continue
        deps.append(_dep(stripped.split("#", 1)[0].strip(), scope))
    return deps, set(), []


def _parse_setup_py(content: str) -> tuple[list[dict], set[str], list[str]]:
    flags = []
    deps = []
    project_names = set()
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
                deps.extend(_dep_strings(_literal_list(kw.value, variables, flags), "required"))
            elif kw.arg == "extras_require":
                extras = _literal_dict(kw.value, variables, flags)
                for group, values in extras.items():
                    deps.extend(_dep_strings(values, _group_scope(group, default="optional")))
    return deps, project_names, sorted(set(flags))


def _parse_setup_cfg(content: str) -> tuple[list[dict], set[str], list[str]]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(content)
    except configparser.Error:
        return [], set(), ["MANIFEST_PARSE_FAILED"]
    deps = []
    project_names = {parser.get("metadata", "name", fallback="")}
    if parser.has_option("options", "install_requires"):
        deps.extend(_dep_strings(parser.get("options", "install_requires").splitlines(), "required"))
    if parser.has_section("options.extras_require"):
        for group, value in parser.items("options.extras_require"):
            deps.extend(_dep_strings(value.splitlines(), _group_scope(group, default="optional")))
    return deps, project_names, []


def _parse_gemfile(content: str) -> tuple[list[dict], set[str], list[str]]:
    deps = []
    for line in content.splitlines():
        match = re.match(r"\s*gem\s+['\"]([^'\"]+)['\"](?:,\s*['\"]([^'\"]+)['\"])?", line)
        if match:
            deps.append({"name": match.group(1), "scope": "required", "version_spec": match.group(2) or ""})
    return deps, set(), []


def _parse_composer_json(content: str) -> tuple[list[dict], set[str], list[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [], set(), ["MANIFEST_PARSE_FAILED"]
    deps = []
    for key, scope in [("require", "required"), ("require-dev", "dev")]:
        for name, spec in (data.get(key) or {}).items():
            if name.lower() == "php":
                continue
            deps.append({"name": name, "scope": scope, "version_spec": str(spec)})
    return deps, {data.get("name", "").split("/")[-1]}, []


def _static_assignments(tree: ast.AST, flags: list[str]) -> dict[str, object]:
    values = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
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


def _literal_list(node: ast.AST, variables: dict[str, object], flags: list[str]) -> list[str]:
    if isinstance(node, ast.Name):
        value = variables.get(node.id)
        return value if isinstance(value, list) else []
    try:
        value = ast.literal_eval(node)
    except Exception:
        flags.append("DYNAMIC_DEPS_UNPARSED")
        return []
    return value if isinstance(value, list) else []


def _literal_dict(node: ast.AST, variables: dict[str, object], flags: list[str]) -> dict:
    if isinstance(node, ast.Name):
        value = variables.get(node.id)
        return value if isinstance(value, dict) else {}
    try:
        value = ast.literal_eval(node)
    except Exception:
        flags.append("DYNAMIC_DEPS_UNPARSED")
        return {}
    return value if isinstance(value, dict) else {}


def _dep_strings(values, scope: str) -> list[dict]:
    if not isinstance(values, list):
        return []
    return [_dep(value, scope) for value in values if isinstance(value, str)]


def _poetry_deps(values, scope: str) -> list[dict]:
    deps = []
    if not isinstance(values, dict):
        return deps
    for name, spec in values.items():
        if name.lower() == "python":
            continue
        deps.append({"name": name, "scope": scope, "version_spec": str(spec)})
    return deps


def _toml_table_deps(values, scope: str) -> list[dict]:
    deps = []
    if not isinstance(values, dict):
        return deps
    for name, spec in values.items():
        if isinstance(spec, dict) and ("path" in spec or "git" in spec):
            continue
        deps.append({"name": name, "scope": scope, "version_spec": str(spec)})
    return deps


def _dep(value: str, scope: str) -> dict:
    cleaned = value.strip().strip(",")
    name = _normalize_name(cleaned)
    version = cleaned[len(name):].strip() if cleaned.lower().startswith(name.lower()) else cleaned
    return {"name": name, "raw_name": cleaned, "scope": scope, "version_spec": version}


def _group_scope(group: str, default: str) -> str:
    lower = group.lower()
    if any(token in lower for token in ("test", "tests")):
        return "test"
    if any(token in lower for token in ("dev", "doc", "docs", "lint", "build", "bench")):
        return "dev"
    return default


def _normalize_name(value: str) -> str:
    text = value.strip()
    text = text.split(";", 1)[0].strip()
    text = text.split("[", 1)[0].strip()
    text = _VERSION_SPLIT_RE.split(text, 1)[0].strip()
    match = _DEP_NAME_RE.match(text)
    if not match:
        return ""
    name = match.group(1).lower().replace("_", "-")
    if ":" in name:
        name = name.split(":")[-1]
    return name


# Token-boundary match — AND the match token must be the primary name token,
# not just a component of a compound package name like types-redis or jest-redis
def _canonicalize(normalized: str) -> tuple[str, str]:
    if normalized in _CANONICAL:
        return _CANONICAL[normalized]

    parts = normalized.split("-")

    # Only match if the canonical key equals the FIRST token of the package name
    # types-redis → first token is "types" not "redis" → no match
    # redis-py    → first token is "redis" → match
    # ray-serve   → first token is "ray" → match (intentional — Ray extension)
    # array       → first token is "array" → no match against "ray"
    for key, value in _CANONICAL.items():
        if key == parts[0]:
            return value

    return normalized, "library"


def _is_self_dependency(canonical_name: str, self_names: set[str]) -> bool:
    if not self_names:
        return False
    normalized = canonical_name.lower().replace("_", "-").replace(" ", "-")
    return normalized in self_names or any(
        name and (name in normalized or normalized in name)
        for name in self_names
    )


def _confidence(scope: str, origin: str) -> float:
    base = {
        "required": 0.95,
        "optional": 0.80,
        "test": 0.70,
        "dev": 0.60,
    }.get(scope, 0.60)
    return round(base if origin == "product" else base * 0.3, 3)


def _is_internal_spec(spec: str) -> bool:
    lower = str(spec).strip().lower()
    return lower.startswith(("workspace:", "file:", "link:"))


def _repo_names(repo_full_name: str) -> set[str]:
    names = set()
    for part in repo_full_name.split("/"):
        norm = _normalize_name(part)
        if norm:
            names.add(norm)
    return names
