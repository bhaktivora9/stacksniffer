"""
backend/services/ai_pipeline.py

v2.0 — AI/insights layer fixes:
  1. Pass Gemini the flags array — model can reject implausible detections
  2. Run classify_domain BLIND (no pattern_matches) — avoids "restated" confirmation
  3. Reject why_this_stack without causal verb
  4. Exclude infra techs from insights unless domain == infra_tool
  5. Ban retired vocabulary (microservices) when domain != web_api
  6. DAG Scheduler + Pull-Based Scraping added to allowed pattern enum
  7. model_id emitted into response
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
from os import getenv
import json
import logging
from backend.models.schemas import AiInference

logger = logging.getLogger(__name__)

_KEY        = getenv("GEMINI_API_KEY", "")
_MODEL      = getenv("GEMINI_ANALYSIS_MODEL", "gemini-2.5-flash")
_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_ANALYSIS", 8192))

genai.configure(api_key=_KEY)

_json_model = genai.GenerativeModel(
    _MODEL,
    generation_config=genai.types.GenerationConfig(
        response_mime_type="application/json",
        max_output_tokens=_MAX_TOKENS,
        temperature=0.2,
    ),
)

_DOMAIN_SAFE_DEFAULTS = {
    "domain":             "unknown",
    "domain_confidence":  0.0,
    "domain_reasoning":   "AI classification failed",
    "architecture_style": "unknown",
    "missing_patterns":   [],
    "ai_inferred_techs":  [],
    "rag_influenced":     False,
    "similar_repos_used": 0,
    "rejected":           False,
}

_INSIGHTS_SAFE_DEFAULTS = {
    "why_this_stack":       "",
    "stack_pattern":        "Custom",
    "ecosystem_context":    "",
    "notable_combinations": [],
}

_FALLBACK_DOMAIN_OPTIONS = (
    "web_api | web_app | cli_tool | library | database | "
    "data_pipeline | ml_platform | infra_tool | "
    "mobile_app | desktop_app | language | unknown"
)

# Causal verbs required in why_this_stack
_CAUSAL_VERBS = [
    "enables", "decouples", "avoids", "prevents", "allows",
    "reduces", "eliminates", "provides", "enforces", "ensures",
    "separates", "abstracts", "simplifies", "offloads", "replaces"
]

# Pattern enum — all values Gemini is allowed to emit
# DAG Scheduler and Pull-Based Scraping added from ground truth
_DOMAIN_PATTERN_MAP = {
    "library": ["Plugin Architecture", "Chain of Responsibility", "Fluent Interface", "Hexagonal", "MVC", "Custom"],
    "ml_platform": ["Plugin Architecture", "Chain of Responsibility", "Fluent Interface", "Hexagonal", "Custom"],
    "data_pipeline": ["Event-Driven", "DAG Scheduler", "Lambda Architecture", "Event Sourcing", "Custom"],
    "infra_tool": ["Plugin Architecture", "Pull-Based Scraping", "Event-Driven", "Hexagonal", "Custom"],
    "web_api": ["MVC", "Hexagonal", "CQRS", "Event-Driven", "Microservices", "Custom"],
    "web_app": ["MVC", "JAMstack", "Hexagonal", "Custom"],
    "database": ["Plugin Architecture", "Hexagonal", "Event-Driven", "Custom"],
    "cli_tool": ["Plugin Architecture", "Hexagonal", "Custom"],
}
_ALL_PATTERNS = sorted({pattern for patterns in _DOMAIN_PATTERN_MAP.values() for pattern in patterns})

_CLASSIFICATION_RULES = """
Classify by answering: "Who uses the final artifact and how do they interact with it?"

RULES (strict priority order):

"language":    Repo IS a programming language, compiler, or runtime.
               Lexer/parser/AST, bytecode, runtime GC. Examples: cpython, rust-lang/rust

"library":     Developers import it. Package manifest but NO Dockerfile, NO app server.
               CRITICAL: Framework repos are ALWAYS library.
               fastapi/fastapi, nestjs/nest, gin-gonic/gin, vercel/next.js,
               spring-projects/spring-boot → ALL library.

"database":    Repo IS a storage engine. No web framework, storage internals present.
               Examples: elasticsearch, redis, chroma, qdrant

"data_pipeline": Data flows through it. Stream processors, batch ETL, orchestrators.
               Examples: kafka, airflow, flink, dagster

"ml_platform": AI/ML IS the primary product. Removing AI destroys core value.
               LLM frameworks (LangChain, LlamaIndex) → ml_platform.
               Vector databases → database (not ml_platform).

"infra_tool":  Operators deploy/monitor/manage other systems.
               IaC, Kubernetes operators, monitoring (Prometheus, Grafana).
               Grafana has web UI but operators use it → infra_tool not web_app.

"web_app":     End users interact via browser. Frontend framework present.
               Has a backend too? Still web_app.

"web_api":     Other services call its HTTP endpoints. Backend framework, no frontend.

"cli_tool":    End users interact via terminal. argparse/click/cobra, no HTTP server.

"mobile_app":  AndroidManifest.xml, Info.plist, pubspec.yaml, React Native.

"desktop_app": Electron, Tauri, Qt, WPF.

"unknown":     Genuinely ambiguous. Do not use as default.

DISAMBIGUATION:
Q1: Repo name matches well-known framework? → library
Q2: Primary artifact is a storage engine? → database
Q3: Data moves through it between systems? → data_pipeline
Q4: AI/ML IS the product (removing it = nothing left)? → ml_platform
Q5: Operators use it to manage infrastructure? → infra_tool
Q6: End users see browser UI? → web_app
Q7: Other services call HTTP endpoints? → web_api
Q8: Terminal binary? → cli_tool
"""


def _serialize(raw: dict) -> dict:
    result = {}
    for cat, techs in raw.items():
        result[cat] = [
            t.model_dump() if hasattr(t, "model_dump") else t
            for t in techs
        ]
    return result


def _safe_json(text: str, fallback: dict) -> dict:
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            cleaned = "\n".join(inner).strip()
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("[ai_pipeline] JSON parse failed: %s — raw: %s", e, text[:400])
        return fallback


def _log_error(fn: str, err: str) -> None:
    if "429" in err or "quota" in err.lower():
        msg = "QUOTA EXCEEDED"
    elif "403" in err or "PERMISSION_DENIED" in err:
        msg = "AUTH FAILED — check GEMINI_API_KEY"
    elif "404" in err or "not found" in err.lower():
        msg = f"MODEL NOT FOUND — check GEMINI_ANALYSIS_MODEL={_MODEL}"
    else:
        msg = "FAILED"
    print(f"[ai_pipeline] {fn} {msg}: {err[:300]}")


async def _get_domain_options() -> str:
    try:
        import backend.services.storage_service as storage_service
        domains = await storage_service.get_all_domains()
        if domains:
            active = [
                d["domain_id"] for d in domains
                if d.get("status", "active") == "active"
            ]
            if "unknown" not in active:
                active.append("unknown")
            return " | ".join(active)
    except Exception:
        pass
    return _FALLBACK_DOMAIN_OPTIONS


def _format_rag_context(similar_repos: list[dict]) -> str:
    if not similar_repos:
        return ""
    lines = ["SIMILAR REPOS FROM CORPUS (few-shot examples):"]
    for r in similar_repos:
        name    = r.get("repo", {}).get("full_name", "unknown")
        domain  = r.get("stack", {}).get("domain", "unknown")
        pattern = r.get("stack", {}).get("stack_pattern", "")
        lang    = r.get("stack", {}).get("primary_language", "")
        why     = r.get("stack", {}).get("why_this_stack", "")
        score   = r.get("score", 0.0)
        lines.append(f"  • {name} [{lang}] → {domain} | {pattern} | sim={score:.3f}")
        if why:
            lines.append(f"    {why}")
    return "\n".join(lines)


def _filter_insights_techs(detections: dict, domain: str) -> dict:
    """
    Exclude infra techs from insights input unless domain == infra_tool.
    GitHub Actions, Docker, Helm are deployment details, not architectural signals.
    """
    if domain == "infra_tool":
        return detections

    INFRA_NOISE = {"GitHub Actions", "Docker", "Helm", "Kubernetes",
                   "Skaffold", "Terraform", "Ansible", "Pulumi"}
    result = {}
    for cat, techs in detections.items():
        insight_techs = [
            t for t in techs
            if not (
                _tech_get(t, "detection_source") == "manifest"
                and (_tech_get(t, "scope") != "required" or _tech_get(t, "origin") != "product")
            )
        ]
        if cat == "infra":
            # Keep only non-noise infra
            result[cat] = [
                t for t in insight_techs
                if _tech_get(t, "name") not in INFRA_NOISE
            ]
        else:
            result[cat] = insight_techs
    return result


def _tech_get(tech, key: str, default=""):
    if isinstance(tech, dict):
        return tech.get(key, default)
    return getattr(tech, key, default)


def _has_causal_verb(text: str) -> bool:
    text_lower = text.lower()
    return any(verb in text_lower for verb in _CAUSAL_VERBS)


async def classify_domain(
    raw_detections: dict,
    file_tree: list[str],
    flags: list[dict] = None,
    similar_repos: list[dict] = None,
) -> dict:
    """
    Domain classification — BLIND to pattern_matches.
    Receives only tech category summaries, NOT raw pattern_matches.
    Avoids "detection restated as confirmation" problem.

    Passes flags array so model can output rejected:true for implausible inputs.
    """
    serializable   = _serialize(raw_detections)
    domain_options = await _get_domain_options()
    rag_section    = _format_rag_context(similar_repos or [])

    # Format flags for Gemini — it can output rejected:true if detections are implausible
    flags_str = ""
    if flags:
        error_flags = [f for f in flags if f.get("severity") == "error"]
        if error_flags:
            flags_str = f"""
QUALITY FLAGS (errors detected in Pass 1):
{json.dumps([{"code": f["code"], "message": f["message"]} for f in error_flags], indent=2)}

If these flags indicate unreliable detections, set "rejected": true and
explain in domain_reasoning. Do not rationalize false positives.
"""

    prompt = f"""{_CLASSIFICATION_RULES}

{rag_section}

DETECTED TECHNOLOGIES (Pass 1 results — pattern matching only):
{json.dumps(serializable, indent=2)}

File tree sample (60 files): {json.dumps(file_tree[:60])}

{flags_str}

Return ONLY valid JSON:
{{
  "domain": "one of: {domain_options}",
  "domain_confidence": 0.0,
  "domain_reasoning": "under 120 chars — cite specific file/keyword evidence",
  "architecture_style": "one of: monolith | microservices | serverless | event_driven | unknown",
  "missing_patterns": [
    "tech NAMES that are very likely used but appear in NEITHER the detected "
    "technologies list above NOR in the AI inferred techs you are about to emit. "
    "Do not list techs you are already classifying in ai_inferred_techs."
   ],
  "ai_inferred_techs": [
    {{"name": "str", "category": "str", "confidence": 0.0, "reasoning": "under 80 chars"}}
  ],
  "rejected": false,
  "rejection_reason": "only set if rejected=true — why detections are implausible",
  "rag_influenced": {json.dumps(bool(similar_repos))}
}}"""

    try:
        response = await asyncio.to_thread(_json_model.generate_content, prompt)
        result = _safe_json(response.text, _DOMAIN_SAFE_DEFAULTS.copy())
        result["rag_influenced"]     = bool(similar_repos)
        result["similar_repos_used"] = len(similar_repos) if similar_repos else 0
        result.setdefault("rejected", False)
        return result
    except Exception as e:
        _log_error("classify_domain", str(e))
        return _DOMAIN_SAFE_DEFAULTS.copy()


async def generate_stack_insights(
    domain_result: dict,
    detections: dict,
    repo_name: str,
    repo_description: str,
    similar_repos: list[dict] = None,
) -> dict:
    """
    Generate architectural insights.

    Changes from v1:
    - Infra techs filtered out unless domain==infra_tool
    - why_this_stack validated for causal verb after generation
    - Banned vocabulary enforced: microservices for non-web_api repos
    - Pattern enum expanded: DAG Scheduler, Pull-Based Scraping
    """
    domain = domain_result.get("domain", "unknown")

    # Filter infra noise from insights input
    filtered_detections = _filter_insights_techs(detections, domain)
    serializable = _serialize(filtered_detections)
    rag_section  = _format_rag_context(similar_repos or [])

    # Domain-specific pattern guidance
    additional_context = ""
    if domain in ("library", "ml_platform"):
        additional_context = """
IMPORTANT: This repo is a LIBRARY or FRAMEWORK (or ml_platform framework).
stack_pattern MUST describe its design architecture, NOT deployment topology.
Use: Plugin Architecture, Chain of Responsibility, Fluent Interface, Hexagonal
Do NOT use: Microservices, Serverless, Event-Driven (unless this is a service, not a lib)
"""
    elif domain == "data_pipeline":
        additional_context = """
For data pipeline repos, valid stack_patterns include:
DAG Scheduler (airflow/dagster — workflow DAGs), Event-Driven (kafka/flink — streaming),
Lambda Architecture (batch + stream), Event Sourcing.
"""
    elif domain == "infra_tool":
        additional_context = """
For infra tools, valid patterns: Plugin Architecture (Grafana, Terraform),
Pull-Based Scraping (Prometheus), Event-Driven (Kubernetes controllers), Hexagonal.
"""

    # Banned vocabulary
    banned = ""
    if domain != "web_api":
        banned = '\nDo NOT use the word "microservices" in any field.'
    allowed_patterns = _DOMAIN_PATTERN_MAP.get(domain, _ALL_PATTERNS)
    pattern_enum = " | ".join(allowed_patterns)

    prompt = f"""You are a principal engineer providing architectural analysis.
Base ALL insights on detected technologies only. Do not invent features.
why_this_stack MUST contain a causal verb: enables, decouples, avoids, prevents,
allows, reduces, eliminates, provides, enforces, ensures, separates, abstracts.
Enumeration without causality will be rejected.

Repository: {repo_name}
Description: {repo_description or "no description"}
Domain: {domain} — {domain_result.get("domain_reasoning", "")}
Allowed stack_pattern values for this domain: {pattern_enum}
Stack (infra noise excluded): {json.dumps(serializable, indent=2)}

{rag_section}
{additional_context}
{banned}

Return ONLY valid JSON:
{{
  "why_this_stack": "under 130 chars — causal explanation (must contain enables/decouples/etc)",
  "stack_pattern": "one of: {pattern_enum}",
  "ecosystem_context": "under 160 chars — specific industry context",
  "notable_combinations": ["specific non-obvious tech relationship in this repo"]
}}"""

    try:
        response = await asyncio.to_thread(_json_model.generate_content, prompt)
        result   = _safe_json(response.text, _INSIGHTS_SAFE_DEFAULTS.copy())

        # Validate causal verb in why_this_stack
        why = result.get("why_this_stack", "")
        if why and not _has_causal_verb(why):
            result["_why_no_causal_verb"] = True
            logger.warning(
                "[ai_pipeline] why_this_stack missing causal verb: %s", why[:100]
            )

        return result
    except Exception as e:
        _log_error("generate_stack_insights", str(e))
        return _INSIGHTS_SAFE_DEFAULTS.copy()


async def run_full_ai_pipeline(
    raw_detections: dict,
    file_tree: list[str],
    repo_name: str,
    repo_description: str,
    flags: list[dict] = None,
) -> dict:
    """
    Full RAG + classification pipeline.
    flags param: quality flags from Pass 1, passed to classify_domain.
    """
    print(f"[ai_pipeline] ═══ PIPELINE START — repo={repo_name} model={_MODEL} ═══")

    similar_repos = []
    embedding     = []

    try:
        from backend.services.embedding_service import embed_stack
        import backend.services.storage_service as storage_service

        prelim_stack = {
            **{
                cat: [t.model_dump() if hasattr(t, "model_dump") else t for t in techs]
                for cat, techs in raw_detections.items()
            },
            "domain": "unknown", "stack_pattern": "", "why_this_stack": "",
            "ecosystem_context": "", "architecture_style": "unknown",
        }

        embedding = await embed_stack(prelim_stack)
        non_zero  = sum(1 for v in embedding if v != 0.0)
        print(f"[ai_pipeline] Embedding: dim={len(embedding)} non_zero={non_zero}")

        if non_zero > 0:
            corpus_size = await storage_service.count_embedded_analyses()
            if corpus_size >= 5:
                similar_repos = await storage_service.find_similar(embedding, limit=3)
                print(f"[ai_pipeline] RAG: {len(similar_repos)} repos (corpus={corpus_size})")
            else:
                print(f"[ai_pipeline] RAG skipped — corpus too small ({corpus_size}/5)")

    except Exception as e:
        print(f"[ai_pipeline] Embedding/RAG failed: {e}")
        logger.warning("[ai_pipeline] Embedding/RAG: %s", str(e)[:200])

    # classify_domain — blind (no pattern_matches), but sees flags
    result1 = await classify_domain(
        raw_detections, file_tree,
        flags=flags or [],
        similar_repos=similar_repos
    )

    # If model rejected — skip insights, return early with rejection
    if result1.get("rejected"):
        print(f"[ai_pipeline] REJECTED: {result1.get('rejection_reason', '')}")
        return {
            **result1,
            **_INSIGHTS_SAFE_DEFAULTS,
            "ai_inferences":          [],
            "ai_classification_used": False,
            "ai_calls_made":          1,
            "rag_repos_retrieved":    len(similar_repos),
            "embedding":              embedding,
            "model_id":               _MODEL,
        }

    result2 = await generate_stack_insights(
        result1, raw_detections, repo_name, repo_description, similar_repos
    )

    ai_inferences = []
    for t in result1.get("ai_inferred_techs", []):
        try:
            ai_inferences.append(AiInference(
                tech=t.get("name", ""),
                category=t.get("category", "infra"),
                reasoning=t.get("reasoning", ""),
                confidence=float(t.get("confidence", 0.0)),
            ))
        except Exception:
            pass

    ai_used = (
        result1.get("domain", "unknown") != "unknown"
        and result1.get("domain_confidence", 0.0) > 0.0
        and not result1.get("rejected", False)
    )

    print(
        f"[ai_pipeline] ═══ COMPLETE ═══ "
        f"domain={result1.get('domain')} "
        f"conf={result1.get('domain_confidence', 0):.2f} "
        f"pattern={result2.get('stack_pattern')} "
        f"rejected={result1.get('rejected', False)}"
    )

    return {
        **result1,
        **result2,
        "ai_inferences":          [i.model_dump() for i in ai_inferences],
        "ai_classification_used": ai_used,
        "ai_calls_made":          2,
        "rag_repos_retrieved":    len(similar_repos),
        "embedding":              embedding,
        "model_id":               _MODEL,   # emitted into response body
    }
