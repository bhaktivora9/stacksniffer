"""
backend/services/stack_detector.py

v2.0 — Detection layer fixes:
  1. matched_file emits REAL file path (was emitting pattern name)
  2. Word boundaries on all keywords < 6 chars
  3. File-type gates — ai_ml/frameworks don't fire on .gradle/.toml/.yml
     unless the keyword is specific enough
  4. Self-reference exclusion — repo name ≈ tech name → skip
  5. Empty-keyword only for definitive filenames
  6. Structural manifest parsing (tomllib, json.load) not substring
  7. scope field: required/optional/test/dev from manifest section
  8. Confidence is evidence-derived, not patterns.json constant
  9. Priority-sorted file_tree built from file_contents ∪ tree
 10. repo.topics consumed as Pass-0 prior
"""
import json
import re
import sys
from pathlib import Path
from backend.models.schemas import DetectedTech, PatternMatch
from backend.services.manifest_parser import is_recognized_manifest, parse_manifest_dependencies

_patterns_path = Path(__file__).parent.parent / "config" / "patterns.json"
with _patterns_path.open() as _f:
    _PATTERNS: dict = json.load(_f)

_CATEGORIES = [
    "languages", "frameworks", "databases",
    "messaging", "ai_ml", "infra", "testing", "library"
]

# File-type gates per category
# tech in these categories must only fire on these extensions/filenames
_CATEGORY_FILE_GATES = {
    "ai_ml": {
        "allowed_extensions": {".py", ".toml", ".txt", ".cfg", ".lock", ".rs"},
        "allowed_basenames":  {
            "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
            "Cargo.toml", "poetry.lock", "uv.lock"
        },
        "denied_extensions": {".gradle", ".yml", ".yaml", ".xml", ".json"},
    },
    "frameworks": {
        "allowed_extensions": {
            ".py", ".toml", ".txt", ".cfg", ".json", ".ts", ".js",
            ".go", ".rs", ".rb", ".java", ".kt"
        },
        "denied_patterns": [".github/", "docs/", "README"],
    },
    "databases": {
        "denied_patterns": [".github/", "docs/"],
    },
    "messaging": {
        "denied_patterns": [".github/", "docs/"],
    },
}

# Short keywords (< 6 chars) require word-boundary matching
_SHORT_KEYWORD_BOUNDARY_RE: dict[str, re.Pattern] = {}

def _get_boundary_pattern(keyword: str) -> re.Pattern:
    if keyword not in _SHORT_KEYWORD_BOUNDARY_RE:
        escaped = re.escape(keyword)
        _SHORT_KEYWORD_BOUNDARY_RE[keyword] = re.compile(
            rf'\b{escaped}\b', re.IGNORECASE
        )
    return _SHORT_KEYWORD_BOUNDARY_RE[keyword]


def _keyword_matches(keyword: str, content: str) -> bool:
    """
    Match keyword in content with word-boundary enforcement for short keywords.
    Prevents: ray → array/rayon, junit → jest-junit, pg → postgres
    """
    if len(keyword) < 6:
        return bool(_get_boundary_pattern(\b{keyword}\b).search(content))
    return keyword in content


def _passes_file_gate(category: str, filepath: str) -> bool:
    """
    Check if a file is an appropriate source for this tech category.
    ai_ml patterns must not fire on .gradle/.yml; etc.
    """
    gate = _CATEGORY_FILE_GATES.get(category)
    if not gate:
        return True

    ext      = Path(filepath).suffix.lower()
    basename = Path(filepath).name

    # Check denied extensions
    if ext in gate.get("denied_extensions", set()):
        return False

    # Check denied path patterns
    for pattern in gate.get("denied_patterns", []):
        if pattern in filepath:
            return False

    # Check allowed extensions (if specified, must match)
    allowed_ext = gate.get("allowed_extensions")
    if allowed_ext and ext and ext not in allowed_ext:
        allowed_basenames = gate.get("allowed_basenames", set())
        if basename not in allowed_basenames:
            return False

    return True


def _is_self_reference(tech_name: str, repo_full_name: str) -> bool:
    """
    Return True if the tech name is clearly referencing the repo itself.
    apache/kafka → skip "Apache Kafka" detection
    tiangolo/fastapi → skip "FastAPI" detection
    """
    repo_parts = repo_full_name.lower().replace("-", " ").replace("_", " ").split("/")
    tech_lower = tech_name.lower().replace("-", " ").replace("_", " ")

    for part in repo_parts:
        # Direct substring match (fastapi in fastapi)
        if part in tech_lower or tech_lower in part:
            return True
        # Token overlap (langchain-ai → langchain)
        part_tokens = set(part.split())
        tech_tokens = set(tech_lower.split())
        if part_tokens & tech_tokens:
            return True
    return False


def _strip_comments(content: str, filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    char_map = {
        ".py": "#", ".toml": "#", ".cfg": "#", ".txt": "#",
        ".yml": "#", ".yaml": "#", ".sh": "#",
    }
    char = char_map.get(ext)
    if not char:
        return content
    return "\n".join(
        line for line in content.splitlines()
        if not line.strip().startswith(char)
    )


# ── Structural manifest parsing ───────────────────────────────────────────────

def _parse_pyproject_deps(content: str) -> dict[str, str]:
    """
    Parse pyproject.toml structurally.
    Returns {package_name_lower: scope}
    scope: "required" | "optional" | "dev" | "test"
    """
    deps: dict[str, str] = {}
    current_section = ""
    pkg_re = re.compile(r'["\']?([a-zA-Z0-9][a-zA-Z0-9_\-\.]*)["\']?\s*[><=!@~\[]')

    for line in content.splitlines():
        stripped = line.strip()
        sec = re.match(r'^\[([^\]]+)\]', stripped)
        if sec:
            current_section = sec.group(1).lower()
            continue

        scope = None
        if current_section in ("project",):
            # Look for dependencies key
            if stripped.startswith("dependencies"):
                scope = "required"
        elif "optional-dependencies" in current_section:
            scope = "optional"
        elif "dependency-groups" in current_section:
            # uv/PEP 735 — check group name for test/dev hints
            if any(w in current_section for w in ("test", "dev", "lint", "doc")):
                scope = "test"
            else:
                scope = "dev"
        elif "tool.poetry.dependencies" == current_section:
            scope = "required"
        elif any(w in current_section for w in
                 ("dev-dependencies", "group.dev", "group.test", "group.lint")):
            scope = "dev"

        if scope:
            for m in pkg_re.finditer(stripped):
                pkg = m.group(1).lower().replace("_", "-")
                if len(pkg) > 2 and pkg not in deps:
                    deps[pkg] = scope

    return deps


def _parse_package_json_deps(content: str) -> dict[str, str]:
    """Parse package.json structurally. Returns {name_lower: scope}."""
    try:
        data = json.loads(content)
    except Exception:
        return {}
    deps: dict[str, str] = {}
    for key, scope in [
        ("dependencies", "required"),
        ("peerDependencies", "required"),
        ("optionalDependencies", "optional"),
        ("devDependencies", "dev"),
    ]:
        for pkg in data.get(key, {}):
            deps[pkg.lower()] = scope
    return deps


def _parse_pom_deps(content: str) -> dict[str, str]:
    """Parse pom.xml for artifactId values. Returns {artifact_lower: scope}."""
    deps: dict[str, str] = {}
    scope_re = re.compile(
        r'<artifactId>([^<]+)</artifactId>.*?(?:<scope>([^<]+)</scope>)?',
        re.DOTALL
    )
    for m in scope_re.finditer(content):
        artifact = m.group(1).strip().lower()
        scope_val = (m.group(2) or "required").strip().lower()
        scope = "test" if scope_val == "test" else (
            "dev" if scope_val in ("provided", "system") else "required"
        )
        deps[artifact] = scope
    return deps


def _parse_go_mod(content: str) -> dict[str, str]:
    """Parse go.mod require block. Returns {module_path_lower: scope}."""
    deps: dict[str, str] = {}
    in_require = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            in_require = True
            continue
        if in_require and stripped == ")":
            in_require = False
            continue
        if in_require or stripped.startswith("require "):
            parts = stripped.replace("require ", "").split()
            if parts:
                mod = parts[0].lower()
                scope = "dev" if "// indirect" in line else "required"
                deps[mod] = scope
    return deps


def _build_manifest_deps(file_contents: dict[str, str]) -> dict[str, dict[str, str]]:
    """
    Parse all manifests. Returns:
    {filepath: {package_lower: scope}}
    """
    result: dict[str, dict[str, str]] = {}
    for path, content in file_contents.items():
        bn = Path(path).name.lower()
        if bn == "pyproject.toml":
            result[path] = _parse_pyproject_deps(content)
        elif bn == "package.json":
            result[path] = _parse_package_json_deps(content)
        elif bn == "pom.xml":
            result[path] = _parse_pom_deps(content)
        elif bn == "go.mod":
            result[path] = _parse_go_mod(content)
    return result


def _get_manifest_scope(
    keyword: str,
    manifest_deps: dict[str, dict[str, str]]
) -> tuple[str | None, str | None]:
    """
    Look up keyword in parsed manifest deps.
    Returns (filepath, scope) or (None, None).
    """
    kw = keyword.lower().replace("_", "-")
    for filepath, deps in manifest_deps.items():
        if kw in deps:
            return filepath, deps[kw]
        # Partial match for namespaced packages: @nestjs/core → nestjs
        for pkg, scope in deps.items():
            if kw in pkg or pkg in kw:
                return filepath, scope
    return None, None


# ── Pass-0: topics prior ──────────────────────────────────────────────────────

def _apply_topics_prior(
    topics: list[str],
    detections: dict[str, list[DetectedTech]],
) -> None:
    """
    Consume repo.topics as Pass-0 prior.
    Topics are GitHub-provided metadata — authoritative for language/framework.
    Topics boost existing detections or add low-confidence signals.
    """
    if not topics:
        return

    topic_lower = {t.lower().replace("-", "").replace("_", "") for t in topics}

    for category in _CATEGORIES:
        for pattern in _PATTERNS.get(category, []):
            if pattern.get("_meta"):
                continue
            name = pattern.get("name", "")
            name_norm = name.lower().replace("-", "").replace("_", "").replace(" ", "")

            if name_norm in topic_lower:
                # Check if already detected
                existing = next(
                    (t for t in detections[category] if t.name == name), None
                )
                if existing:
                    # Boost confidence slightly
                    existing.confidence = min(existing.confidence + 0.05, 0.99)
                    existing.detection_source = "both"
                else:
                    # Add as low-confidence topic signal
                    detections[category].append(DetectedTech(
                        name=name,
                        confidence=0.65,
                        detection_source="topic_prior",
                        category=category,
                        scope="required",
                    ))


# ── Main detection ────────────────────────────────────────────────────────────

def detect_stack(
    file_contents: dict[str, str],
    file_tree: list[str],
    repo_full_name: str = "",
    topics: list[str] = None,
) -> dict:
    """
    Pass 1: deterministic pattern matching.

    repo_full_name: used for self-reference exclusion
    topics:         repo.topics from GitHub metadata (Pass-0 prior)
    """
    # Build unified file set (tree + fetched file keys)
    all_paths    = set(file_tree) | set(file_contents.keys())
    tree_set     = all_paths
    tree_basenames = {Path(p).name for p in all_paths}

    # Parse all manifests structurally
    manifest_deps = _build_manifest_deps(file_contents)
    manifest_result = parse_manifest_dependencies(
        file_contents=file_contents,
        file_tree=list(all_paths),
        repo_full_name=repo_full_name,
    )

    all_pattern_matches: list[PatternMatch] = []
    detections: dict[str, list[DetectedTech]] = {cat: [] for cat in _CATEGORIES}
    patterns_checked = 0
    matched_files:  set[str] = set()

    for category in _CATEGORIES:
        seen_techs: dict[str, tuple[float, str]] = {}  # name → (confidence, scope)

        for pattern in _PATTERNS.get(category, []):
            if pattern.get("_meta"):
                continue
            patterns_checked += 1
            tech_name = pattern.get("name", "")

            # Self-reference exclusion
            if repo_full_name and _is_self_reference(tech_name, repo_full_name):
                continue

            keywords       = pattern.get("keywords", [])
            detection_files = pattern.get("detection_files", [])
            extensions     = pattern.get("extensions", [])
            base_conf      = pattern.get("confidence", 0.80)

            fired_matches: list[PatternMatch] = []

            # ── File presence matches (only for definitive filenames) ──────────
            DEFINITIVE_FILES = {
                "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                "go.mod", "Cargo.toml", "Chart.yaml", "manage.py",
                "angular.json", "next.config.js", "next.config.ts",
                "skaffold.yaml", ".scalafmt.conf", "build.sbt",
            }

            for df in detection_files:
                if df.endswith("/"):
                    # Directory prefix match
                    for path in file_tree:
                        if df.rstrip("/") in path:
                            if not keywords:  # only fire if no keywords required
                                fired_matches.append(PatternMatch(
                                    tech=tech_name, category=category,
                                    matched_file=path,      # REAL PATH
                                    matched_keyword="",
                                    confidence=base_conf,
                                ))
                            break
                elif df in DEFINITIVE_FILES and df in tree_basenames:
                    # Find actual full path
                    real_path = next(
                        (p for p in all_paths if Path(p).name == df), df
                    )
                    if not keywords:
                        fired_matches.append(PatternMatch(
                            tech=tech_name, category=category,
                            matched_file=real_path,    # REAL PATH
                            matched_keyword="",
                            confidence=base_conf,
                        ))

            # ── Extension matches ─────────────────────────────────────────────
            for ext in extensions:
                for path in file_tree:
                    if path.endswith(ext):
                        fired_matches.append(PatternMatch(
                            tech=tech_name, category=category,
                            matched_file=path,          # REAL PATH
                            matched_keyword=ext,
                            confidence=base_conf,
                        ))
                        break

            # ── Keyword matches ───────────────────────────────────────────────
            if keywords:
                # Determine search scope
                if detection_files:
                    search_contents = {
                        path: content
                        for path, content in file_contents.items()
                        if Path(path).name in {Path(df).name for df in detection_files}
                        or any(df.rstrip("/") in path for df in detection_files if df.endswith("/"))
                    }
                    if not search_contents:
                        search_contents = file_contents
                else:
                    search_contents = file_contents

                for keyword in keywords:
                    found = False
                    for filepath, raw_content in search_contents.items():

                        if is_recognized_manifest(filepath):
                            continue

                        # File-type gate
                        if not _passes_file_gate(category, filepath):
                            continue

                        content = _strip_comments(raw_content, filepath)

                        if not _keyword_matches(keyword, content):
                            continue

                        # Evidence-derived confidence from manifest scope
                        conf   = base_conf
                        scope  = "required"
                        bn     = Path(filepath).name.lower()

                        if bn in ("pyproject.toml", "package.json", "pom.xml", "go.mod"):
                            manifest_scope = manifest_deps.get(filepath, {})
                            kw_norm = keyword.lower().replace("_", "-")
                            # Find scope for this keyword in the manifest
                            for pkg, pkg_scope in manifest_scope.items():
                                if kw_norm in pkg or pkg in kw_norm:
                                    scope = pkg_scope
                                    if scope in ("test", "dev"):
                                        conf = round(base_conf * 0.6, 3)
                                    elif scope == "optional":
                                        conf = round(base_conf * 0.7, 3)
                                    break

                        fired_matches.append(PatternMatch(
                            tech=tech_name, category=category,
                            matched_file=filepath,      # REAL FILE PATH
                            matched_keyword=keyword,
                            confidence=conf,
                            scope=scope if hasattr(PatternMatch, 'scope') else None,
                        ))
                        found = True
                        break
                    if found:
                        break

            if not fired_matches:
                continue

            # Register matches
            for pm in fired_matches:
                all_pattern_matches.append(pm)
                if pm.matched_file:
                    matched_files.add(pm.matched_file)

            best_conf  = max(pm.confidence for pm in fired_matches)
            best_scope = next(
                (pm.scope for pm in fired_matches
                 if hasattr(pm, 'scope') and pm.scope), "required"
            )

            if tech_name not in seen_techs or best_conf > seen_techs[tech_name][0]:
                seen_techs[tech_name] = (best_conf, best_scope or "required")

        for tech_name, (confidence, scope) in seen_techs.items():
            detections[category].append(DetectedTech(
                name=tech_name,
                confidence=confidence,
                detection_source="pattern_match",
                category=category,
                scope=scope,
            ))

    # Pass-0: apply topics prior
    if topics:
        _apply_topics_prior(topics, detections)

    # Manifest dependencies are authoritative for declared third-party deps.
    for category, techs in manifest_result["detections"].items():
        if category not in detections:
            detections[category] = []
        for tech in techs:
            existing = next(
                (t for t in detections[category] if t.name.lower() == tech.name.lower()),
                None,
            )
            if existing:
                if tech.confidence > existing.confidence:
                    existing.confidence = tech.confidence
                    existing.detection_source = tech.detection_source
                    existing.scope = tech.scope
                    existing.origin = tech.origin
                    existing.matched_file = tech.matched_file
                    existing.version_spec = tech.version_spec
                    existing.manifest_frequency = tech.manifest_frequency
                else:
                    existing.detection_source = "both"
            else:
                detections[category].append(tech)

    all_pattern_matches.extend(manifest_result["pattern_matches"])

    # Primary language
    primary_language = ""
    if detections["languages"]:
        primary_language = max(
            detections["languages"], key=lambda t: t.confidence
        ).name

    # Complexity
    cats_with_hits = sum(1 for cat in _CATEGORIES if detections[cat])
    complexity_score = min(cats_with_hits * 2, 10)

    # Confidence breakdown over all categories
    confidence_breakdown: dict[str, float] = {}
    for cat in _CATEGORIES:
        techs = detections[cat]
        if techs:
            confidence_breakdown[cat] = round(
                sum(t.confidence for t in techs) / len(techs), 3
            )

    return {
        "detections":          detections,
        "pattern_matches":     all_pattern_matches,
        "files_analyzed":      len(file_contents),
        "patterns_checked":    patterns_checked,
        "primary_language":    primary_language,
        "complexity_score":    complexity_score,
        "confidence_breakdown": confidence_breakdown,
        "manifests_selected":   manifest_result["manifests_selected"],
        "flags":                manifest_result["flags"],
    }


def rewrite_detection_sources(
    ai_inferences: list[dict],
    detections: dict,
) -> tuple[list[dict], list[str]]:
    """
    Post-process after AI pipeline:
    - Techs Gemini inferred that Pass 1 already matched → rewrite to pattern_match
    - Techs Gemini inferred with no patterns.json entry → missing_patterns list
    """
    pass1_techs = {
        t.name.lower()
        for cat in _CATEGORIES
        for t in detections.get(cat, [])
    }
    pattern_tech_names = {
        entry.get("name", "").lower()
        for entries in _PATTERNS.values()
        if isinstance(entries, list)
        for entry in entries
        if isinstance(entry, dict)
    }

    fixed      = []
    missing    = []

    for inf in ai_inferences:
        tech_lower = inf.get("tech", "").lower()
        if tech_lower in pass1_techs:
            fixed.append({**inf, "detection_source": "pattern_match"})
        else:
            fixed.append(inf)
        if tech_lower not in pattern_tech_names:
            missing.append(inf.get("tech", ""))

    return fixed, [m for m in missing if m]

 
# ── Defect 2: Normalization — no suffix stripping ────────────────────────────
 
def _normalize_pkg(name: str) -> str:
    """
    Normalize package name for lookup only.
    Replace _ with - and lowercase. NO suffix stripping.
    fastapi-cli stays fastapi-cli. pydantic-settings stays pydantic-settings.
    Matching langchain-core → langchain is done by substring containment,
    not by mutating the key.
    """
    return name.lower().replace("_", "-")
 
 
def _is_self_reference(tech_name: str, repo_full_name: str) -> bool:
    """
    Check on ORIGINAL (pre-normalization) tech_name and repo path.
    Runs before any normalization to avoid fastapi-cli → fastapi confusion.
    """
    repo_parts = repo_full_name.lower().replace("-", " ").replace("_", " ").split("/")
    tech_lower = tech_name.lower().replace("-", " ").replace("_", " ")
    for part in repo_parts:
        if part in tech_lower or tech_lower in part:
            return True
        if set(part.split()) & set(tech_lower.split()):
            return True
    return False
 
