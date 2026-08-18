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
import time
from os import getenv
import google.generativeai as genai
from backend.services.cross_cutting_layer_fix import (
    LAYER_INFERENCE_NULL_PREFERENCE,
    guard_cross_cutting_layer,
)
from backend.services.safe_json import get_repair_counters, safe_parse_gemini_json
from backend.services.technology_role_registry import valid_technology_roles
from backend.services.gemini_interactions import InteractionsModel
from backend.services.ai_pipeline import (
    _build_generation_config,
    _dep_json_model,
    _DEP_CLASSIFICATION_MODEL,
    _MODEL as _SOFTWARE_TYPE_MODEL,
)
from backend.config.signals import FILE_SIGNALS, EXTENSION_SIGNALS

logger = logging.getLogger(__name__)

def _env_float(name: str, default: float) -> float:
    try:
        return float(getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning(
            "[dep_classifier] invalid float for %s, using default %.1fs",
            name,
            default,
        )
        return default


GEMINI_REQUEST_TIMEOUT_SECONDS = _env_float("GEMINI_REQUEST_TIMEOUT_SECONDS", 40.0)
_DEP_CLASSIFICATION_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_DEP_CLASSIFICATION", 32768))
_DEP_CLASSIFICATION_CHUNK_SIZE = 30
_DEP_CLASSIFICATION_MAX_RETRIES = 2

# ── Phase 0: File signals ─────────────────────────────────────────────────────

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


def _log_truncation_stage(raw_response, *, chunk_no: int, total_chunks: int) -> None:
    try:
        candidates = getattr(raw_response, "candidates", None)
        if candidates:
            finish_reason = str(getattr(candidates[0], "finish_reason", "")).lower()
            if "max" in finish_reason and "token" in finish_reason:
                logger.warning(
                    "[dep_classifier] generation chunk=%s/%s truncated by model: %s",
                    chunk_no,
                    total_chunks,
                    finish_reason,
                )
                return
    except Exception:
        pass

    try:
        usage = getattr(raw_response, "usage_metadata", None)
        output_tokens = getattr(usage, "candidates_token_count", None)
        if (
            output_tokens is not None
            and output_tokens >= _DEP_CLASSIFICATION_MAX_TOKENS
        ):
            logger.warning(
                "[dep_classifier] generation chunk=%s/%s hit max_output_tokens=%s (observed=%s)",
                chunk_no,
                total_chunks,
                _DEP_CLASSIFICATION_MAX_TOKENS,
                output_tokens,
            )
    except Exception:
        pass


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


def _is_deadline_or_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    if "deadline" in text or "deadline_exceeded" in text:
        return True
    if "504" in text:
        return True
    return type(exc).__name__ in {"DeadlineExceeded", "ReadTimeout", "TimeoutError"}


async def _store_emergent_technology_roles(clean: list[dict], repo_full_name: str) -> None:
    """Persist any non-standard technology_role Gemini invented, for human review."""
    try:
        import backend.services.storage_service as storage_service
        valid_roles = await valid_technology_roles()
        for entry in clean:
            technology_role = entry["technology_role"]
            if technology_role not in valid_roles:
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
    _json_model=None,
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
    )

    deps_for_prompt = [
        {
            "name": d["name"], "raw_name": d.get("raw_name", d["name"]),
            "scope": d.get("scope", "required"), "matched_file": d.get("matched_file", ""),
        }
        for d in unique_deps
    ]

    if not deps_for_prompt:
        return []

    valid = await valid_technology_roles()
    clean: list[dict] = []
    observed: list[dict] = []

    # Everything that can throw now lives INSIDE the try, so a prompt-build or
    # feedback-fetch error is logged with a full traceback instead of escaping
    # as an opaque DEP_CLASSIFICATION_FAILED.
    try:
        feedback_context = await _build_technology_role_feedback_context()
        base_prompt = "\n\n" + LAYER_INFERENCE_NULL_PREFERENCE + feedback_context
        file_tree_sample = json.dumps(file_tree[:40])

        for chunk_index in range(0, len(deps_for_prompt), _DEP_CLASSIFICATION_CHUNK_SIZE):
            chunk = deps_for_prompt[chunk_index : chunk_index + _DEP_CLASSIFICATION_CHUNK_SIZE]
            prompt = _render_prompt(
                repo_full_name=repo_full_name,
                file_tree_sample=file_tree_sample,
                raw_deps_json=json.dumps(chunk, indent=2),
            ) + base_prompt

            chunk_no = (chunk_index // _DEP_CLASSIFICATION_CHUNK_SIZE) + 1
            total_chunks = (len(deps_for_prompt) + _DEP_CLASSIFICATION_CHUNK_SIZE - 1) // _DEP_CLASSIFICATION_CHUNK_SIZE
            logger.info(
                "[dep_classifier] Gemini start (chunk=%s/%s, product_tail=%s, unresolved_tail=%s)",
                chunk_no,
                total_chunks,
                len(chunk),
                len(seen),
            )
            call_started = time.monotonic()
            model = _json_model or _dep_json_model
            response = None
            if _json_model is not None:
                candidate_model_ids = [None]
            else:
                candidate_model_ids = [
                    _DEP_CLASSIFICATION_MODEL,
                    "gemini-3.5-flash-lite",
                    _SOFTWARE_TYPE_MODEL,
                    "gemini-3.5-flash",
                ]
            seen_models: list[str] = []
            last_error: Exception | None = None

            for candidate in candidate_model_ids:
                if candidate is not None:
                    if candidate in seen_models:
                        continue
                    seen_models.append(candidate)
                    candidate_model = InteractionsModel(
                        candidate,
                        generation_config=_build_generation_config(
                            _DEP_CLASSIFICATION_MAX_TOKENS,
                            model_id=candidate,
                        ),
                        legacy_model_factory=lambda c=candidate: genai.GenerativeModel(
                            c,
                            generation_config=_build_generation_config(
                                _DEP_CLASSIFICATION_MAX_TOKENS,
                                model_id=c,
                            ),
                        ),
                    )
                else:
                    candidate_model = model

                last_candidate_error: Exception | None = None
                for retry_no in range(0, _DEP_CLASSIFICATION_MAX_RETRIES + 1):
                    try:
                        response = await asyncio.to_thread(
                            candidate_model.generate_content,
                            prompt,
                            request_options={"timeout": GEMINI_REQUEST_TIMEOUT_SECONDS},
                            generation_config=_build_generation_config(
                                _DEP_CLASSIFICATION_MAX_TOKENS,
                                model_id=(candidate or getattr(model, "_model", _DEP_CLASSIFICATION_MODEL)),
                            ),
                        )
                        break
                    except TypeError:
                        response = await asyncio.to_thread(
                            candidate_model.generate_content,
                            prompt,
                            request_options={"timeout": GEMINI_REQUEST_TIMEOUT_SECONDS},
                        )
                        break
                    except Exception as e:
                        last_candidate_error = e
                        if candidate is not None and "404" in str(e) and "no longer available" in str(e).lower():
                            break
                        if _is_deadline_or_timeout(e) and retry_no < _DEP_CLASSIFICATION_MAX_RETRIES:
                            await asyncio.sleep(0.4 * (2 ** retry_no))
                            continue
                        break
                if response is not None:
                    break
                if candidate is not None and "404" in str(last_candidate_error or "") and "no longer available" in str(last_candidate_error or "").lower():
                    last_error = last_candidate_error
                    logger.warning(
                        "[dep_classifier] model %s unavailable, trying fallback",
                        candidate,
                    )
                    continue
                if last_candidate_error:
                    if candidate is None:
                        raise last_candidate_error
                    last_error = last_candidate_error
                    raise last_candidate_error

            if response is None:
                if last_error is None:
                    raise RuntimeError("dep_classifier: no candidate model succeeded")
                raise last_error
            _log_truncation_stage(
                response,
                chunk_no=chunk_no,
                total_chunks=total_chunks,
            )
            logger.info(
                "[dep_classifier] Gemini call took %.1fs (chunk=%s/%s)",
                time.monotonic() - call_started,
                chunk_no,
                total_chunks,
            )

            parsed = safe_parse_gemini_json(
                response.text or "",
                expect="array",
                site="dep_classifier",
                on_repair=lambda stage: logger.warning(
                    "[dep_classifier] parse_repair stage=%s chunk=%s/%s count=%s",
                    stage, chunk_no, total_chunks,
                    get_repair_counters().get("dep_classifier", {}).get(stage, 0),
                ),
            )
            if not isinstance(parsed, list):
                logger.warning("[dep_classifier] Gemini returned %s for chunk %s, not a list", type(parsed).__name__, chunk_no)
                continue

            for entry in parsed:
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
                guarded_layer = guard_cross_cutting_layer(
                    name, layer, packages=entry.get("packages", []),
                )
                if guarded_layer is None and layer is not None:
                    logger.info(
                        "[dep_classifier] Overrode cross-cutting layer %s for %s to null",
                        layer, name,
                    )
                    layer_resolution = "cross_cutting_null"
                layer = guarded_layer
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

        logger.info(
            "[dep_classifier] classified %s techs from %s packages",
            len(clean),
            len(unique_deps),
        )
        await _store_emergent_technology_roles(observed, repo_full_name)
        return clean

    except json.JSONDecodeError as e:
        response_text = locals().get("response") and locals()["response"].text
        raw = (response_text or "")[:800] if response_text is not None else "<generate_content raised>"
        logger.error("[dep_classifier] JSON parse failed: %s", e)
        logger.error("[dep_classifier] raw Gemini output: %r", raw)
        return []

    except Exception as e:
        # Full traceback. If the prompt template ever regains a stray brace, or
        # generate_content raises, or the model config is wrong — it shows HERE,
        # in full, instead of as a truncated symptom.
        import traceback
        logger.error("[dep_classifier] failed:\n%s", traceback.format_exc())
        logger.error(
            "[dep_classifier] failed with %s: %s",
            type(e).__name__,
            e,
        )
        return []
