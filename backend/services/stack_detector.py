import json
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

_COMPLEXITY_MAP = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9}


def _basename(path: str) -> str:
    return Path(path).name


def _is_self_reference(tech_name: str, repo_full_name: str) -> bool:
    """
    Return True if tech_name refers to the repo itself.
    Runs on the ORIGINAL pattern name before any normalization.
    fastapi/fastapi + 'FastAPI' → True
    huggingface/transformers + 'HuggingFace' → True
    langchain-ai/langchain + 'LangChain' → True

    Does NOT strip suffixes — fastapi-cli stays distinct from FastAPI
    (handled by manifest_parser._repo_names self-exclude on package level).
    """
    if not repo_full_name:
        return False
    repo_parts = repo_full_name.lower().replace("-", " ").replace("_", " ").split("/")
    tech_lower = tech_name.lower().replace("-", " ").replace("_", " ")
    for part in repo_parts:
        if part in tech_lower or tech_lower in part:
            return True
        if set(part.split()) & set(tech_lower.split()):
            return True
    return False



def _file_tree_set(file_tree: list[str]) -> set[str]:
    return {_basename(p) for p in file_tree} | set(file_tree)


def _match_file_patterns(
    pattern: dict,
    category: str,
    file_tree_set: set[str],
    file_tree: list[str],
) -> list[PatternMatch]:
    matches = []
    for detection_file in pattern.get("detection_files", []):
        if detection_file.endswith("/"):
            prefix = detection_file.rstrip("/")
            for path in file_tree:
                if prefix in path:
                    matches.append(PatternMatch(
                        tech=pattern["name"],
                        category=category,
                        matched_file=path,
                        matched_keyword="",
                        confidence=pattern["confidence"],
                    ))
                    break
        elif detection_file.startswith("*."):
            ext = detection_file[1:]
            for path in file_tree:
                if path.endswith(ext):
                    matches.append(PatternMatch(
                        tech=pattern["name"],
                        category=category,
                        matched_file=path,
                        matched_keyword="",
                        confidence=pattern["confidence"],
                    ))
                    break
        elif detection_file in file_tree_set:
            matches.append(PatternMatch(
                tech=pattern["name"],
                category=category,
                matched_file=detection_file,
                matched_keyword="",
                confidence=pattern["confidence"],
            ))
    return matches


def _match_keyword_patterns(
    pattern: dict,
    category: str,
    file_contents: dict[str, str],
    detection_files: list[str],
) -> list[PatternMatch]:
    matches = []
    keywords = pattern.get("keywords", [])
    if not keywords:
        return matches

    search_contents: dict[str, str] = {}
    if detection_files:
        for fname in detection_files:
            if fname in file_contents:
                search_contents[fname] = file_contents[fname]
            else:
                for path, content in file_contents.items():
                    if _basename(path) == fname:
                        search_contents[path] = content
    else:
        search_contents = file_contents

    for keyword in keywords:
        for filepath, content in search_contents.items():
            if keyword in content:
                matches.append(PatternMatch(
                    tech=pattern["name"],
                    category=category,
                    matched_file=filepath,
                    matched_keyword=keyword,
                    confidence=pattern["confidence"],
                ))
                break
        else:
            continue
        break

    return matches


def _match_extensions(
    pattern: dict,
    category: str,
    file_tree: list[str],
) -> list[PatternMatch]:
    matches = []
    for ext in pattern.get("extensions", []):
        for path in file_tree:
            if path.endswith(ext):
                matches.append(PatternMatch(
                    tech=pattern["name"],
                    category=category,
                    matched_file=path,
                    matched_keyword=ext,
                    confidence=pattern["confidence"],
                ))
                break
    return matches


def detect_stack(
    file_contents: dict[str, str],
    file_tree: list[str],
    repo_full_name: str = "",
    topics: list[str] = None,
) -> dict:
    all_paths  = set(file_tree) | set(file_contents.keys())
    tree_set   = all_paths | {_basename(p) for p in all_paths}

    # Structural manifest parsing (authoritative for declared deps)
    manifest_result = parse_manifest_dependencies(
        file_contents=file_contents,
        file_tree=list(all_paths),
        repo_full_name=repo_full_name,
    )

    all_pattern_matches: list[PatternMatch] = []
    detections: dict[str, list[DetectedTech]] = {cat: [] for cat in _CATEGORIES}
    patterns_checked = 0

    for category in _CATEGORIES:
        seen_techs: dict[str, float] = {}
        for pattern in _PATTERNS.get(category, []):
            patterns_checked += 1

            # Self-reference exclusion on ORIGINAL tech name
            if _is_self_reference(pattern.get("name", ""), repo_full_name):
                continue

            file_hits = _match_file_patterns(pattern, category, tree_set, file_tree)
            keyword_hits = _match_keyword_patterns(
                pattern, category, file_contents, pattern.get("detection_files", [])
            )
            ext_hits = _match_extensions(pattern, category, file_tree)
            fired = file_hits + keyword_hits + ext_hits

            if not fired:
                continue

            for pm in fired:
                all_pattern_matches.append(pm)

            best = max(pm.confidence for pm in fired)
            if pattern["name"] not in seen_techs or best > seen_techs[pattern["name"]]:
                seen_techs[pattern["name"]] = best

        for tech_name, confidence in seen_techs.items():
            detections[category].append(DetectedTech(
                name=tech_name,
                confidence=confidence,
                detection_source="pattern_match",
                category=category,
            ))

    # Merge manifest detections (authoritative — override pattern matches)
    for category, techs in manifest_result["detections"].items():
        if category not in detections:
            detections[category] = []
        for tech in techs:
            existing = next(
                (t for t in detections[category] if t.name.lower() == tech.name.lower()),
                None,
            )
            if existing:
                existing.detection_source = "both"

                if getattr(tech, "scope", None):
                    existing.scope = tech.scope
                if getattr(tech, "origin", None):
                    existing.origin = tech.origin
                if getattr(tech, "matched_file", None):
                    existing.matched_file = tech.matched_file
                if getattr(tech, "version_spec", None):
                    existing.version_spec = tech.version_spec
                if getattr(tech, "manifest_frequency", None):
                    existing.manifest_frequency = tech.manifest_frequency

                manifest_scope = getattr(tech, "scope", "required")
                if manifest_scope in ("required", "optional"):
                    existing.confidence = max(existing.confidence, tech.confidence)
                elif manifest_scope in ("dev", "test"):
                    existing.confidence = min(existing.confidence, tech.confidence)
            else:
                detections[category].append(tech)

    all_pattern_matches.extend(manifest_result["pattern_matches"])

    primary_language = ""
    if detections["languages"]:
        primary_language = max(detections["languages"], key=lambda t: t.confidence).name

    high_conf = sum(
        1 for cat in _CATEGORIES
        if any(getattr(t, "confidence", 0) >= 0.80 for t in detections.get(cat, []))
    )
    scale = {0: 1, 1: 2, 2: 4, 3: 5, 4: 7, 5: 8, 6: 9}
    complexity_score = scale.get(high_conf, 10)

    confidence_breakdown: dict[str, float] = {}
    for cat in _CATEGORIES:
        techs = detections[cat]
        if techs:
            confidence_breakdown[cat] = round(sum(t.confidence for t in techs) / len(techs), 3)

    return {
        "detections":           detections,
        "pattern_matches":      all_pattern_matches,
        "files_analyzed":       len(file_contents),
        "patterns_checked":     patterns_checked,
        "primary_language":     primary_language,
        "complexity_score":     complexity_score,
        "confidence_breakdown": confidence_breakdown,
        "manifests_selected":   manifest_result["manifests_selected"],
        "flags":                manifest_result["flags"],
    }
