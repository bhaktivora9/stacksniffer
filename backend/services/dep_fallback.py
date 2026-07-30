"""
backend/services/dep_fallback.py

Deterministic dependency classification. No AI, no network, no I/O.

WHY THIS EXISTS
---------------
Phase 2a (Gemini dep classification) was a GATE: raw deps flowed through it and
if the call failed they were discarded. Two consecutive runs proved the cost â€”
fastapi and vercel/next.js both returned DEP_CLASSIFICATION_FAILED, and both
produced a stack with every category empty except languages. next.js declares
100+ dependencies in its root package.json. Phase 1 extracted all of them.
All of them were dropped because one Gemini call died.

The fix is not "a better table". It is an invariant:

    Phase 1 extracted N deps  ->  the output contains N deps.

Classification decides HOW a dep is labelled. It never decides WHETHER it
exists. build_base_detections() is an identity transform over raw_deps: it
always returns one record per dep, whatever happens upstream. Gemini then
enriches that base â€” it can relabel and raise confidence, but it cannot delete.

CONFIDENCE TIERS
----------------
    0.85  table       â€” curated ecosystem map, high precision
    0.55  heuristic   â€” name pattern match, plausible but unverified
    0.40  passthrough â€” declared in a manifest, category unknown

Passthrough matters most. An unrecognised dep is still DECLARED IN A MANIFEST,
which is stronger evidence than anything the AI layer infers from a file tree.
Emitting it at 0.40 in `library` is honest. Dropping it is not.

STEP (a) KNOWLEDGE-STORE INDIRECTION
------------------------------------
The five ecosystem tables below are now the SEED DATA the KnowledgeStore loads.
They are still defined here as in-code dicts (step a). Step (b) moves them to a
seed file; step (c) to Mongo. The functions in this module read the assembled
tables via _ecosystem_tables(), which sources them from the store â€” so the
later data-source swaps touch load() only, not this module's logic.

Nothing about classification behavior changes in step (a): same entries, same
_seed_table shape, same tiers, same confidences.
"""

from __future__ import annotations

import re

__all__ = [
    "build_base_detections",
    "enrich_with_classifications",
    "classify_dep",
    "assert_deps_survived",
]

# â”€â”€ Ecosystem seed tables â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Not exhaustive by design â€” the heuristic and passthrough tiers catch the
# tail. Only add entries you are confident about; a wrong table entry is
# worse than a passthrough, because it looks authoritative.
#
# Dependency knowledge lives in config/dependency_knowledge.json.

_ECOSYSTEM_ALIASES = {
    "npm": "npm", "package.json": "npm",
    "pypi": "pypi", "pyproject.toml": "pypi", "requirements.txt": "pypi",
    "setup.py": "pypi", "setup.cfg": "pypi", "Pipfile": "pypi",
    "maven": "maven", "pom.xml": "maven",
    "gradle": "maven", "build.gradle": "maven",
    "cargo": "cargo", "Cargo.toml": "cargo",
    "go": "go", "go.mod": "go",
}

from backend.services.knowledge_store import store as _KNOWLEDGE_STORE

_CANONICAL_TABLES = _KNOWLEDGE_STORE.dep_tables()
_NPM = _CANONICAL_TABLES["npm"]
_PYPI = _CANONICAL_TABLES["pypi"]
_MAVEN_GROUP = _CANONICAL_TABLES["maven"]
_CARGO = _CANONICAL_TABLES["cargo"]
_GO = _CANONICAL_TABLES["go"]

# Lazily-built alias-keyed lookup. Built on first use from the store's tables,
# NOT at import time â€” that would create a dep_fallback <-> knowledge_store
# import cycle. Cached after first build.
_ECOSYSTEM_TABLES_CACHE: dict[str, dict] | None = None


def _ecosystem_tables() -> dict[str, dict]:
    """Alias-keyed ecosystem->table lookup, sourced from the KnowledgeStore.

    Identical in content to the old module-level _ECOSYSTEM_TABLES literal;
    only the source (store) and the build timing (lazy) changed.
    """
    global _ECOSYSTEM_TABLES_CACHE
    if _ECOSYSTEM_TABLES_CACHE is None:
        from backend.services.knowledge_store import store
        canonical = store.dep_tables()  # {"npm": {...}, "pypi": {...}, ...}
        _ECOSYSTEM_TABLES_CACHE = {
            alias: canonical[canon]
            for alias, canon in _ECOSYSTEM_ALIASES.items()
        }
    return _ECOSYSTEM_TABLES_CACHE


# Canonical-table references for the cross-ecosystem fallback and identity
# checks used below. Sourced from the store, same shape as before.
def _canonical_tables() -> dict[str, dict]:
    from backend.services.knowledge_store import store
    return store.dep_tables()


# â”€â”€ Heuristics (tier 2) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Ordered: first match wins. Deliberately conservative â€” a wrong heuristic is
# worse than a passthrough because it carries higher confidence.

_HEURISTICS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|[-_./])(test|tests|spec|mock|junit|pytest|jest|assert)([-_./]|$)"), "testing"),
    (re.compile(r"(^|[-_./])(postgres|postgresql|pgsql|mysql|mariadb|sqlite|mongo|mongodb|"
                r"redis|cassandra|dynamodb|elasticsearch|opensearch|clickhouse|"
                r"neo4j|couchdb|orm|sqlalchemy|prisma)([-_./]|$)"), "databases"),
    (re.compile(r"(^|[-_./])(kafka|rabbit|rabbitmq|amqp|pulsar|nats|zeromq|mqtt|"
                r"celery|sqs|pubsub|eventbus|broker)([-_./]|$)"), "messaging"),
    (re.compile(r"(^|[-_./])(torch|tensorflow|keras|sklearn|scikit|llm|openai|anthropic|"
                r"gemini|gpt|embedding|embeddings|transformers|huggingface|"
                r"onnx|cuda|nlp|vector)([-_./]|$)"), "ai_ml"),
    (re.compile(r"(^|[-_./])(docker|kubernetes|k8s|helm|terraform|ansible|aws|azure|gcp|"
                r"cloud|nginx|envoy|prometheus|grafana|opentelemetry|otel|"
                r"webpack|vite|rollup|esbuild|bundler|compiler)([-_./]|$)"), "infra"),
    (re.compile(r"(^|[-_./])(react|vue|angular|svelte|django|flask|rails|spring|express|"
                r"framework)([-_./]|$)"), "frameworks"),
]

# Build/dev tooling that should never be reported as part of the product stack.
_DEV_TOOLING = {
    "eslint", "prettier", "black", "ruff", "flake8", "isort", "mypy",
    "pylint", "husky", "lint-staged", "commitlint", "semantic-release",
    "typescript", "ts-node", "rimraf", "cross-env", "npm-run-all",
    "@types/node", "setuptools", "wheel", "pip", "twine", "build",
}

_TEST_SCOPES = {"test", "dev", "development", "testing"}


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _maven_group(name: str) -> str:
    """org.apache.kafka:kafka-clients:3.6.0 -> org.apache.kafka

    The GROUP, never the artifact. This is the _normalize_name bug from the
    Java path: keying on `kafka-clients` misses org.apache.kafka entirely.
    """
    return name.split(":")[0] if ":" in name else name


def _go_module_root(name: str) -> str:
    """github.com/gin-gonic/gin/v2 -> github.com/gin-gonic/gin"""
    parts = name.split("/")
    if len(parts) >= 3:
        base = "/".join(parts[:3])
        return re.sub(r"/v\d+$", "", base)
    return name


def classify_dep(
    name: str,
    ecosystem: str | None = None,
    scope: str | None = None,
) -> tuple[str, float, str]:
    """
    -> (category, confidence, tier)

    tier is one of: "table" | "heuristic" | "passthrough" | "dev_tool"
    Never raises. Never returns None. Every dep gets a home.
    """
    raw = _norm(name)
    if not raw:
        return "library", 0.40, "passthrough"

    ecosystem_tables = _ecosystem_tables()
    canonical_tables = _canonical_tables()
    maven_table = canonical_tables["maven"]
    go_table = canonical_tables["go"]

    table = ecosystem_tables.get(ecosystem or "", {})

    # Ecosystem-specific key shaping
    lookup = raw
    if table is maven_table or ":" in raw:
        lookup = _norm(_maven_group(raw))
    elif table is go_table or raw.startswith("github.com/"):
        lookup = _norm(_go_module_root(raw))

    # Dev tooling â€” declared, but not part of the product stack.
    if lookup in _DEV_TOOLING or raw in _DEV_TOOLING:
        return "library", 0.30, "dev_tool"

    def table_result(entry: dict, confidence: float) -> tuple[str, float, str]:
        return (
            entry["category"],
            confidence if not entry["multi_role"] else min(confidence, 0.65),
            "provisional" if entry["multi_role"] else "table",
        )

    # Tier 1: exact table hit
    if lookup in table:
        return table_result(table[lookup], 0.85)

    # Tier 1b: try every table when the ecosystem is unknown. Lower confidence:
    # a cross-ecosystem hit is a weaker signal than one we expected to find.
    if not table:
        for candidate in canonical_tables.values():
            if lookup in candidate:
                return table_result(candidate[lookup], 0.70)

    # Scoped npm: @scope/pkg -> try the bare package name
    if raw.startswith("@") and "/" in raw:
        bare = raw.split("/", 1)[1]
        if bare in table:
            return table_result(table[bare], 0.70)

    # Tier 2: name heuristics
    for pattern, category in _HEURISTICS:
        if pattern.search(lookup):
            return category, 0.55, "heuristic"

    # Tier 3: passthrough. Declared in a manifest, so it exists. We just don't
    # know what it is â€” which is a labelling gap, not grounds for deletion.
    return "library", 0.40, "passthrough"


def build_base_detections(raw_deps: list[dict]) -> list[dict]:
    """
    Identity transform over Phase 1 output. THE invariant lives here:
    len(output) == len(deduped input), always, with no AI involved.

    raw_deps items: {name, scope, origin, matched_file, version_spec, ecosystem}
    """
    ecosystem_tables = _ecosystem_tables()
    canonical_tables = _canonical_tables()
    maven_table = canonical_tables["maven"]
    go_table = canonical_tables["go"]

    out: list[dict] = []
    seen: set[str] = set()

    for dep in raw_deps or []:
        name = (dep.get("name") or "").strip()
        if not name:
            continue
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)

        ecosystem = dep.get("ecosystem") or dep.get("matched_file")
        scope = dep.get("scope")
        category, confidence, tier = classify_dep(name, ecosystem, scope)
        table = ecosystem_tables.get(ecosystem or "", {})
        lookup = key
        if table is maven_table or ":" in key:
            lookup = _norm(_maven_group(key))
        elif table is go_table or key.startswith("github.com/"):
            lookup = _norm(_go_module_root(key))
        seed = table.get(lookup)
        if seed is None and not table:
            seed = next(
                (candidate[lookup] for candidate in canonical_tables.values()
                 if lookup in candidate),
                None,
            )
        if seed is None and key.startswith("@") and "/" in key:
            seed = table.get(key.split("/", 1)[1])
        multi_role = bool(seed and seed.get("multi_role"))

        # Test-scoped deps are testing regardless of what the name suggests.
        # Scope comes from the manifest and outranks the name every time.
        if _norm(scope) in _TEST_SCOPES and category not in ("testing",):
            category = "testing"
            confidence = min(confidence, 0.70)

        out.append({
            "name": name,
            "category": category,
            "confidence": confidence,
            "detection_source": f"manifest_{tier}",
            "scope": scope or "required",
            "origin": dep.get("origin"),
            "matched_file": dep.get("matched_file"),
            "version_spec": dep.get("version_spec"),
            "fallback_tier": tier,
            "assignment_method": (
                "provisional" if multi_role else
                "deterministic" if tier == "table" else tier
            ),
            "multi_role": multi_role,
            "secondary_roles": list(seed.get("secondary_roles", [])) if seed else [],
        })
    return out


def enrich_with_classifications(
    base: list[dict],
    classifications: list[dict],
) -> list[dict]:
    """
    Overlay Gemini's classifications onto the base.

    Enrichment ONLY. It may relabel a category and raise confidence. It may add
    a tech the base missed. It may NOT remove anything â€” that is the whole
    point. If `classifications` is empty, the base passes through untouched and
    the stack survives a total Gemini outage with degraded category precision.
    """
    if not classifications:
        return base

    by_name = {_norm(d["name"]): d for d in base}

    for cls in classifications:
        name = (cls.get("name") or "").strip()
        if not name:
            continue
        key = _norm(name)
        existing = by_name.get(key)

        if existing is None:
            # Gemini collapsed several packages into one tech (e.g. the 12
            # @babel/* packages -> "Babel"), or renamed one. Keep it.
            by_name[key] = {
                "name": name,
                "category": cls.get("category", "library"),
                "confidence": cls.get("confidence", 0.80),
                "detection_source": "manifest",
                "scope": cls.get("scope", "required"),
                "origin": None,
                "matched_file": None,
                "version_spec": None,
                "fallback_tier": "ai_classified",
                "assignment_method": "inference",
                "multi_role": False,
                "secondary_roles": [],
            }
            continue

        # Relabel: Gemini beats a heuristic or a passthrough, but not a table
        # hit we are confident about.
        if existing["fallback_tier"] in ("heuristic", "passthrough", "dev_tool"):
            existing["category"] = cls.get("category", existing["category"])
        existing["confidence"] = max(
            existing["confidence"], cls.get("confidence", 0.0)
        )
        existing["detection_source"] = "manifest"
        if existing["fallback_tier"] not in ("table", "provisional"):
            existing["fallback_tier"] = "ai_classified"
            existing["assignment_method"] = "inference"

    return list(by_name.values())


def assert_deps_survived(raw_deps: list[dict], detections: list[dict]) -> dict | None:
    """
    The invariant, as a flag rather than an exception.

    Phase 1 found deps but the output has none -> the pipeline dropped data.
    This is the check that would have caught the empty next.js stack at
    analysis time instead of a human noticing it in the UI a day later.
    """
    if raw_deps and not detections:
        return {
            "code": "PIPELINE_DROPPED_DEPS",
            "severity": "error",
            "message": (
                f"Phase 1 extracted {len(raw_deps)} deps but 0 reached the "
                f"output. Classification cannot delete deps â€” this is a bug."
            ),
            "field": "detections",
            "demote": True,
        }
    return None

