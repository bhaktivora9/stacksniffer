"""
backend/services/dep_classifier.py

Phase 0 + Phase 2a of the detection pipeline.

Phase 0: apply_file_signals()
  Deterministic file-presence signals. Dockerfile → Docker, go.mod → Go.
  <1ms. Zero false positives. No Gemini.

Phase 2a: classify_dependencies()
  Single Gemini Flash call classifying the raw dep list from Phase 1.
  ENRICHMENT, not a gate — analyze.py builds a complete base from raw_deps
  first, so a Gemini failure degrades technology_role precision but never empties
  the stack.
"""
import asyncio
import json
import logging
import re
import time

from backend.services.technology_role_registry import is_builtin, valid_technology_roles

logger = logging.getLogger(__name__)

GEMINI_REQUEST_TIMEOUT_SECONDS = 40.0

# ── Phase 0: File signals ─────────────────────────────────────────────────────

FILE_SIGNALS: dict[str, tuple[str, str, float]] = {
    "Dockerfile":           ("Docker",         "infra",      1.0),
    "docker-compose.yml":   ("Docker",         "infra",      1.0),
    "docker-compose.yaml":  ("Docker",         "infra",      1.0),
    "Chart.yaml":           ("Kubernetes",     "infra",      1.0),
    "skaffold.yaml":        ("Skaffold",       "infra",      1.0),
    "go.mod":               ("Go",             "languages",  1.0),
    "Cargo.toml":           ("Rust",           "languages",  1.0),
    "Gemfile":              ("Ruby",           "languages",  0.99),
    "composer.json":        ("PHP",            "languages",  0.99),
    "pubspec.yaml":         ("Flutter",        "frameworks", 1.0),
    "mix.exs":              ("Elixir",         "languages",  1.0),
    "rebar.config":         ("Erlang",         "languages",  1.0),
    "manage.py":            ("Django",         "frameworks", 1.0),
    "angular.json":         ("Angular",        "frameworks", 1.0),
    "next.config.js":       ("Next.js",        "frameworks", 1.0),
    "next.config.ts":       ("Next.js",        "frameworks", 1.0),
    "nuxt.config.js":       ("Nuxt.js",        "frameworks", 1.0),
    "nuxt.config.ts":       ("Nuxt.js",        "frameworks", 1.0),
    "svelte.config.js":     ("SvelteKit",      "frameworks", 1.0),
    ".scalafmt.conf":       ("Scala",          "languages",  1.0),
    "build.sbt":            ("Scala",          "languages",  1.0),
    "mix.lock":             ("Elixir",         "languages",  0.95),
    ".github/workflows/":   ("GitHub Actions", "infra",      1.0),
}

EXTENSION_SIGNALS: dict[str, tuple[str, str, float]] = {
    ".py":    ("Python",     "languages", 0.99),
    ".js":    ("JavaScript", "languages", 0.99),
    ".jsx":   ("JavaScript", "languages", 0.99),
    ".ts":    ("TypeScript", "languages", 0.99),
    ".tsx":   ("TypeScript", "languages", 0.99),
    ".go":    ("Go",         "languages", 0.99),
    ".rs":    ("Rust",       "languages", 0.99),
    ".java":  ("Java",       "languages", 0.99),
    ".kt":    ("Kotlin",     "languages", 0.99),
    ".kts":   ("Kotlin",     "languages", 0.95),
    ".swift": ("Swift",      "languages", 0.99),
    ".rb":    ("Ruby",       "languages", 0.99),
    ".c":     ("C",          "languages", 0.95),
    ".cpp":   ("C++",        "languages", 0.95),
    ".cc":    ("C++",        "languages", 0.95),
    ".cs":    ("C#",         "languages", 0.99),
    ".scala": ("Scala",      "languages", 0.99),
    ".ex":    ("Elixir",     "languages", 0.99),
    ".exs":   ("Elixir",     "languages", 0.95),
    ".hs":    ("Haskell",    "languages", 0.99),
    ".lua":   ("Lua",        "languages", 0.99),
    ".ml":    ("OCaml",      "languages", 0.99),
    ".dart":  ("Dart",       "languages", 0.99),
    ".zig":   ("Zig",        "languages", 0.99),
}

_MIN_EXT_COUNT = 2


def apply_file_signals(file_tree: list[str]) -> list[dict]:
    """Phase 0: deterministic file-presence signals. Zero false positives."""
    detected: dict[str, dict] = {}
    basenames = {p.split("/")[-1] for p in file_tree}
    all_paths = set(file_tree)

    for filename, (tech, technology_role, conf) in FILE_SIGNALS.items():
        if filename.endswith("/"):
            if any(p.startswith(filename) for p in all_paths) and tech not in detected:
                detected[tech] = {
                    "name": tech, "technology_role": technology_role, "confidence": conf,
                    "scope": "required", "detection_source": "file_signal",
                    "matched_file": filename,
                }
        elif filename in basenames:
            real_path = next(
                (p for p in all_paths if p.split("/")[-1] == filename), filename
            )
            if tech not in detected:
                detected[tech] = {
                    "name": tech, "technology_role": technology_role, "confidence": conf,
                    "scope": "required", "detection_source": "file_signal",
                    "matched_file": real_path,
                }

    ext_counts: dict[str, int] = {}
    ext_example: dict[str, str] = {}
    for path in file_tree:
        low = path.lower()
        if any(seg in low for seg in ("/vendor/", "/node_modules/", "/dist/",
                                      "/build/", "/.git/", "/generated/")):
            continue
        if "." in path:
            suffix = "." + path.rsplit(".", 1)[-1].lower()
            ext_counts[suffix] = ext_counts.get(suffix, 0) + 1
            ext_example.setdefault(suffix, path)

    for ext, (tech, technology_role, conf) in EXTENSION_SIGNALS.items():
        count = ext_counts.get(ext, 0)
        if count < _MIN_EXT_COUNT:
            continue
        if tech in detected:
            detected[tech]["file_count"] = detected[tech].get("file_count", 0) + count
            continue
        detected[tech] = {
            "name": tech, "technology_role": technology_role, "confidence": conf,
            "scope": "required", "detection_source": "file_signal",
            "matched_file": ext_example.get(ext, ext), "file_count": count,
        }

    return list(detected.values())


# ── Phase 2a: Gemini dep classification ──────────────────────────────────────

# NOTE ON THE PROMPT: this template contains a literal JSON example with braces.
# It is NOT a .format() string. We build the final prompt with explicit
# placeholder replacement (_render_prompt below), because .format() scans the
# WHOLE string for {…} and a single stray brace anywhere in ~60 lines of rules
# raises KeyError/ValueError — which fired BEFORE the try/except and surfaced as
# a meaningless truncated DEP_CLASSIFICATION_FAILED, identically on every repo.
# Placeholders here are «double-angle» wrapped so they cannot collide with JSON.
_CLASSIFY_PROMPT = """\
You are classifying software package dependencies for the repository: «REPO_FULL_NAME»

File tree sample (for context only — do not classify file tree entries):
«FILE_TREE_SAMPLE»

Raw dependencies extracted from manifests (Phase 1 structural extraction):
«RAW_DEPS_JSON»

TASK: Classify each dependency into exactly ONE technology_role and return a
deduplicated list of CANONICAL TECH NAMES. Also assign its architectural layer,
or null when the dependency's functional tier is genuinely unclear.

TECHNOLOGY_ROLES:
  languages    — programming language runtime, SDK, or toolchain
  frameworks   — application framework developers build on top of
  databases    — storage engines, ORMs, query builders, database clients
  messaging    — message queues, event buses, pub/sub, streaming
  ai_ml        — ML libraries, LLM clients, model training/serving frameworks
  infra        — ASGI/WSGI servers, containers, process managers, deployment
  testing      — test frameworks, assertion libs, mocking, coverage, E2E testing
  library      — utility library (HTTP clients, serialization, validation, logging)
  dev_tool     — linters, formatters, type checkers, build tools — EXCLUDE FROM OUTPUT

ARCHITECTURAL LAYERS (use exactly one value or null):
  frontend | backend | messaging | cache | data | observability | infra |
  testing | ai_ml | language_runtime

TechnologyRole and layer answer different questions. Return both independently.
Do not force a layer merely to match the technology_role. If evidence is insufficient,
return null for architectural_layer.

CLASSIFICATION RULES:
  Java group IDs (format group:artifact or org.x.y):
    org.springframework.boot -> Spring Boot (frameworks)
    org.apache.kafka         -> Apache Kafka (messaging)
    io.ray                   -> Ray (ai_ml)
    org.postgresql           -> PostgreSQL (databases)
    org.junit.jupiter        -> JUnit (testing)
    org.mockito              -> Mockito (testing)

  Go module paths:
    github.com/gin-gonic/gin  -> Gin (frameworks)
    github.com/redis/go-redis -> Redis (databases)
    go.etcd.io/etcd           -> etcd (databases)
    github.com/gorilla/mux    -> Gorilla Mux (frameworks)

  Scoped npm (at-scope/pkg):
    @nestjs/core     -> NestJS (frameworks)
    @playwright/test -> Playwright (testing)
    @prisma/client   -> Prisma (databases)

  Specific rules:
    - types-* packages (TypeScript stubs) -> dev_tool -> EXCLUDE
    - uvicorn, gunicorn, hypercorn -> infra (servers, not frameworks)
    - celery, kombu -> messaging (task queue)
    - sqlalchemy, alembic, prisma, typeorm -> databases (ORM)
    - pytest-asyncio, pytest-cov, pytest-xdist -> collapse to single pytest (testing)
    - pydantic-settings, pydantic-extra-types -> collapse to single Pydantic (library)
    - langchain-openai, langchain-anthropic -> collapse to LangChain (ai_ml)
    - django-rest-framework, djangorestframework -> collapse to Django (frameworks)
    - spring-boot-*, spring-web, spring-data-* -> collapse to Spring Boot (frameworks)
    - black, ruff, mypy, eslint, prettier, flake8, pylint -> dev_tool -> EXCLUDE
    - If a package clearly IS this repo -> EXCLUDE (self-reference)

  Scope mapping:
    scope=required -> confidence 0.85-0.99 (product dependency)
    scope=optional -> confidence 0.70-0.84 (optional feature)
    scope=dev      -> confidence 0.55-0.69 (development tooling)
    scope=test     -> confidence 0.55-0.69 (test infrastructure)
    Dev/test scope frameworks (Flask in a test group) -> confidence 0.55, note in reasoning

OUTPUT FORMAT: a JSON array ONLY. No markdown, no prose, no other text.
One entry per unique CANONICAL TECH NAME (deduplicated). Example of the shape:

[
  {
    "name":       "FastAPI",
    "technology_role":   "frameworks",
    "architectural_layer": "backend",
    "layer_confidence": 0.92,
    "confidence": 0.95,
    "scope":      "required",
    "packages":   ["fastapi", "fastapi-cli"],
    "reasoning":  "Python ASGI web framework"
  }
]

Exclude dev_tool entries entirely. Return [] if no classifiable dependencies.
"""

_GEMINI_SCOPES = {"required", "optional"}
_SCOPE_PRIORITY = {"required": 0, "optional": 1, "dev": 2, "test": 3}
_ARCHITECTURAL_LAYERS = {
    "frontend", "backend", "messaging", "cache", "data",
    "observability", "infra", "testing", "ai_ml", "language_runtime",
}
_MAX_AI_LAYER_CONFIDENCE = 0.80


def _render_prompt(repo_full_name: str, file_tree_sample: str, raw_deps_json: str) -> str:
    """
    Fill the prompt WITHOUT str.format(). Explicit replace means braces in the
    JSON example are inert — no escaping, no KeyError, no stray-brace landmine.
    """
    return (
        _CLASSIFY_PROMPT
        .replace("«REPO_FULL_NAME»", repo_full_name)
        .replace("«FILE_TREE_SAMPLE»", file_tree_sample)
        .replace("«RAW_DEPS_JSON»", raw_deps_json)
    )


def _extract_json_array(text: str) -> str:
    """Pull a JSON array out of a Gemini response regardless of fences or prose."""
    text = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return text


async def _build_technology_role_feedback_context() -> str:
    """Human technology_role decisions, injected into the prompt to steer Gemini."""
    try:
        import backend.services.storage_service as storage_service
        decisions = await storage_service.get_technology_role_feedback_decisions()
    except Exception:
        return ""

    discarded = decisions.get("discarded", [])
    merged = decisions.get("merged", {})
    promoted = decisions.get("promoted", [])
    if not discarded and not merged and not promoted:
        return ""

    lines = ["\nTECHNOLOGY_ROLE FEEDBACK (human decisions from previous analyses):"]
    if discarded:
        lines.append(
            f"  DISCARDED — do NOT emit these technology_roles: {discarded}. "
            f"Map techs from them to 'library' or 'dev_tool' instead."
        )
    for cat, target in merged.items():
        lines.append(f"  MERGED — classify '{cat}' as '{target}' instead.")
    if promoted:
        lines.append(f"  PROMOTED — emit these first-class technology_roles normally: {promoted}")
    return "\n".join(lines)


async def _store_emergent_technology_roles(clean: list[dict], repo_full_name: str) -> None:
    """Persist any non-standard technology_role Gemini invented, for human review."""
    try:
        import backend.services.storage_service as storage_service
        for entry in clean:
            technology_role = entry["technology_role"]
            if not is_builtin(technology_role):
                await storage_service.record_emergent_technology_role(
                    name=technology_role,
                    example_tech=entry["name"],
                    example_repo=repo_full_name,
                )
    except Exception as e:
        logger.warning("[dep_classifier] Could not store emergent technology_roles: %s", e)


async def classify_dependencies(
    raw_deps: list[dict],
    file_tree: list[str],
    repo_full_name: str,
    _json_model,
) -> list[dict]:
    """
    Phase 2a: Gemini classifies the PRODUCT dependency list into tech records.

    ENRICHMENT, not a gate. analyze.py builds a complete base from raw_deps via
    build_base_detections BEFORE this runs, so [] on failure degrades precision
    but never empties the stack. Filters its input to required/optional scope;
    the dev/test tail is handled by the deterministic fallback tiers.
    """
    if not raw_deps:
        return []

    # Dedup by name, keeping highest-priority scope across manifests.
    seen: dict[str, dict] = {}
    for dep in raw_deps:
        key = dep.get("name", "").strip()
        if not key:
            continue
        existing = _SCOPE_PRIORITY.get(seen.get(key, {}).get("scope", "test"), 3)
        this = _SCOPE_PRIORITY.get(dep.get("scope", "test"), 3)
        if key not in seen or this < existing:
            seen[key] = dep

    # Scope filter — product stack only. GEMINI INPUT ONLY; the base sees all.
    classifiable = [d for d in seen.values() if d.get("scope", "required") in _GEMINI_SCOPES]
    if not classifiable:
        return []

    unique_deps = sorted(
        classifiable, key=lambda d: _SCOPE_PRIORITY.get(d.get("scope", "optional"), 1)
    )[:100]

    deps_for_prompt = [
        {
            "name": d["name"], "raw_name": d.get("raw_name", d["name"]),
            "scope": d.get("scope", "required"), "matched_file": d.get("matched_file", ""),
        }
        for d in unique_deps
    ]

    # Everything that can throw now lives INSIDE the try, so a prompt-build or
    # feedback-fetch error is logged with a full traceback instead of escaping
    # as an opaque DEP_CLASSIFICATION_FAILED.
    response = None
    try:
        feedback_context = await _build_technology_role_feedback_context()
        prompt = _render_prompt(
            repo_full_name=repo_full_name,
            file_tree_sample=json.dumps(file_tree[:40]),
            raw_deps_json=json.dumps(deps_for_prompt, indent=2),
        ) + feedback_context

        print(
            f"[dep_classifier] Gemini start "
            f"(product_tail={len(unique_deps)}, unresolved_tail={len(seen)})"
        )
        call_started = time.monotonic()
        response = await asyncio.to_thread(
            _json_model.generate_content,
            prompt,
            request_options={"timeout": GEMINI_REQUEST_TIMEOUT_SECONDS},
        )
        print(
            f"[dep_classifier] Gemini call took "
            f"{time.monotonic() - call_started:.1f}s "
            f"(product_tail={len(unique_deps)})"
        )
        result = json.loads(_extract_json_array(response.text or ""))

        if not isinstance(result, list):
            logger.warning("[dep_classifier] Gemini returned %s, not a list", type(result).__name__)
            return []

        valid = await valid_technology_roles()
        clean: list[dict] = []
        observed: list[dict] = []
        for entry in result:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            cat = entry.get("technology_role", "library")
            if not name:
                continue
            try:
                confidence = float(entry.get("confidence", 0.75))
            except (TypeError, ValueError):
                confidence = 0.75
            if "architectural_layer" not in entry:
                layer = None
                layer_resolution = "missing"
            else:
                layer = entry.get("architectural_layer")
                if layer is None:
                    layer_resolution = "llm_null"
                elif layer not in _ARCHITECTURAL_LAYERS:
                    logger.warning(
                        "[dep_classifier] Off-enum architectural layer %r for %s",
                        layer,
                        name,
                    )
                    layer = None
                    layer_resolution = "off_enum"
                else:
                    layer_resolution = "resolved"
            try:
                layer_confidence = float(entry.get("layer_confidence", confidence))
            except (TypeError, ValueError):
                layer_confidence = confidence
            normalized = {
                "name": name, "technology_role": cat,
                "confidence": max(0.0, min(1.0, confidence)),
                "architectural_layer": layer,
                "layer_confidence": max(
                    0.0,
                    min(_MAX_AI_LAYER_CONFIDENCE, layer_confidence),
                ),
                "layer_resolution": layer_resolution,
                "scope": entry.get("scope", "required"),
                "packages": entry.get("packages", []),
                "reasoning": entry.get("reasoning", ""),
            }
            observed.append(normalized)
            if cat in valid:
                clean.append(normalized)

        print(f"[dep_classifier] classified {len(clean)} techs from {len(unique_deps)} packages")
        await _store_emergent_technology_roles(observed, repo_full_name)
        return clean

    except json.JSONDecodeError as e:
        raw = response.text[:800] if response is not None else "<generate_content raised>"
        logger.error("[dep_classifier] JSON parse failed: %s", e)
        logger.error("[dep_classifier] raw Gemini output: %r", raw)
        return []

    except Exception as e:
        # Full traceback. If the prompt template ever regains a stray brace, or
        # generate_content raises, or the model config is wrong — it shows HERE,
        # in full, instead of as a truncated symptom.
        import traceback
        logger.error("[dep_classifier] failed:\n%s", traceback.format_exc())
        print(f"[dep_classifier] failed with {type(e).__name__}: {e}")
        return []
