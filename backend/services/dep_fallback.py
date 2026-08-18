"""
backend/services/dep_fallback.py

Deterministic dependency classification. No AI, no network, no I/O.

WHY THIS EXISTS
---------------
Phase 2a (Gemini dep classification) was a GATE: raw deps flowed through it and
if the call failed they were discarded. Two consecutive runs proved the cost â€”
fastapi and vercel/next.js both returned DEP_CLASSIFICATION_FAILED, and both
produced a stack with every technology_role empty except languages. next.js declares
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
    0.40  passthrough â€” declared in a manifest, technology_role unknown

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
    "collect_unresolved_tail",
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

from services.knowledge_store import store as _KNOWLEDGE_STORE

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
        from services.knowledge_store import store
        canonical = store.dep_tables()  # {"npm": {...}, "pypi": {...}, ...}
        _ECOSYSTEM_TABLES_CACHE = {
            alias: canonical[canon]
            for alias, canon in _ECOSYSTEM_ALIASES.items()
        }
    return _ECOSYSTEM_TABLES_CACHE


# Canonical-table references for the cross-ecosystem fallback and identity
# checks used below. Sourced from the store, same shape as before.
def _canonical_tables() -> dict[str, dict]:
    from services.knowledge_store import store
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

def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _dedupe_identity_key(name: str | None) -> str:
    """Aggressive key scoped only to deduplication within one analysis.

    Separator-only package-name collisions are possible across a global corpus,
    so this must not become a storage or cross-repository identity.
    """
    normalized = (name or "").strip().casefold()
    return re.sub(r"[-_.\s]+", "", normalized)


def _maven_group(name: str) -> str:
    """org.apache.kafka:kafka-clients:3.6.0 -> org.apache.kafka

    The GROUP, never the artifact. This is the _normalize_name bug from the
    Java path: keying on `kafka-clients` misses org.apache.kafka entirely.
    """
    return name.split(":")[0] if ":" in name else name


def _maven_lookup_key(name: str, table: dict[str, dict]) -> str:
    """Resolve a Maven group by the longest curated group-prefix match.

    org.springframework.security:spring-security-core first tries the complete
    group, then org.springframework.security, then org.springframework. This
    preserves specific mappings while allowing curated parent groups to cover
    their subgroups.
    """
    coordinate = _norm(name)
    if coordinate in table:
        return coordinate
    group = _norm(_maven_group(name))
    matches = [
        key
        for key in table
        if group == key or group.startswith(f"{key}.")
    ]
    return max(matches, key=len) if matches else group


def _go_module_root(name: str) -> str:
    """github.com/gin-gonic/gin/v2 -> github.com/gin-gonic/gin"""
    parts = name.split("/")
    if len(parts) >= 3:
        base = "/".join(parts[:3])
        return re.sub(r"/v\d+$", "", base)
    return name


def _usage_scope(scope: str | None) -> str:
    normalized = _norm(scope)
    if normalized in {"test", "testing"}:
        return "test"
    if normalized in {"dev", "development"}:
        return "dev"
    if normalized == "build":
        return "build"
    return "runtime"


def _layer_assignment(seed: dict | None, confidence: float) -> dict | None:
    if not seed or not seed.get("layer"):
        return None
    multi_role = bool(seed.get("multi_role"))
    return {
        "primary": seed["layer"],
        "secondary": [],
        "assignment_method": "provisional" if multi_role else "deterministic",
        "confidence": min(confidence, 0.65) if multi_role else confidence,
        "disambiguation_pending": multi_role,
    }


def classify_dep(
    name: str,
    ecosystem: str | None = None,
    scope: str | None = None,
) -> tuple[str, float, str]:
    """
    -> (technology_role, confidence, tier)

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
        lookup = _maven_lookup_key(raw, maven_table)
    elif table is go_table or raw.startswith("github.com/"):
        lookup = _norm(_go_module_root(raw))

    # Dev tooling â€” declared, but not part of the product stack.
    if lookup in _DEV_TOOLING or raw in _DEV_TOOLING:
        return "library", 0.30, "dev_tool"

    def table_result(entry: dict, confidence: float) -> tuple[str, float, str]:
        return (
            entry["technology_role"],
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
    for pattern, technology_role in _HEURISTICS:
        if pattern.search(lookup):
            return technology_role, 0.55, "heuristic"

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
        identity_key = _dedupe_identity_key(name)
        if identity_key in seen:
            continue
        seen.add(identity_key)

        key = _norm(name)

        ecosystem = dep.get("ecosystem") or dep.get("matched_file")
        scope = dep.get("scope")
        technology_role, confidence, tier = classify_dep(name, ecosystem, scope)
        table = ecosystem_tables.get(ecosystem or "", {})
        lookup = key
        if table is maven_table or ":" in key:
            lookup = _maven_lookup_key(key, maven_table)
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

        # Dependency groups describe how a package is used, not what it is.
        # Test/dev membership belongs exclusively in usage_scope. A curated
        # package keeps its intrinsic role/layer; an unknown package remains
        # unresolved for the consolidated classifier instead of being guessed
        # as testing.
        layer_assignment = _layer_assignment(seed, confidence)

        out.append({
            "name": name,
            "technology_role": technology_role,
            "confidence": confidence,
            "detection_source": f"manifest_{tier}",
            "scope": scope or "required",
            "origin": dep.get("origin"),
            "matched_file": dep.get("matched_file"),
            "version_spec": dep.get("version_spec"),
            "fallback_tier": tier,
            "multi_role": multi_role,
            "secondary_roles": list(seed.get("secondary_roles", [])) if seed else [],
            "architectural_layer": layer_assignment,
            "usage_scope": _usage_scope(scope),
        })
    return out


def collect_unresolved_tail(
    raw_deps: list[dict],
    base_detections: list[dict],
) -> list[dict]:
    """Return only deps still unresolved after the complete deterministic pass."""
    unresolved_names = {
        _dedupe_identity_key(dep.get("name"))
        for dep in base_detections
        if dep.get("fallback_tier") in {"heuristic", "passthrough"}
        and dep.get("architectural_layer") is None
    }
    return [
        dep
        for dep in raw_deps
        if _dedupe_identity_key(dep.get("name")) in unresolved_names
    ]


def enrich_with_classifications(
    base: list[dict],
    classifications: list[dict],
) -> list[dict]:
    """
    Overlay Gemini's classifications onto the base.

    Enrichment ONLY. It may relabel a technology_role and raise confidence. It may add
    a tech the base missed. It may NOT remove anything â€” that is the whole
    point. If `classifications` is empty, the base passes through untouched and
    the stack survives a total Gemini outage with degraded technology_role precision.
    """
    if not classifications:
        return base

    by_name = {_dedupe_identity_key(d["name"]): d for d in base}

    for cls in classifications:
        name = (cls.get("name") or "").strip()
        if not name:
            continue
        key = _dedupe_identity_key(name)
        existing = by_name.get(key)
        inferred_layer = cls.get("architectural_layer")
        layer_assignment = (
            {
                "primary": inferred_layer,
                "secondary": [],
                "assignment_method": "ai_inferred",
                "confidence": cls.get(
                    "layer_confidence",
                    cls.get("confidence", 0.75),
                ),
                "disambiguation_pending": False,
            }
            if inferred_layer
            else None
        )

        if existing is None:
            # Gemini collapsed several packages into one tech (e.g. the 12
            # @babel/* packages -> "Babel"), or renamed one. Keep it.
            by_name[key] = {
                "name": name,
                "technology_role": cls.get("technology_role", "library"),
                "confidence": cls.get("confidence", 0.80),
                "detection_source": "ai_inferred",
                "scope": cls.get("scope", "required"),
                "origin": None,
                "matched_file": None,
                "version_spec": None,
                "fallback_tier": "ai_classified",
                "multi_role": False,
                "secondary_roles": [],
                "architectural_layer": layer_assignment,
                "layer_inference_status": cls.get("layer_resolution"),
                "usage_scope": _usage_scope(cls.get("scope")),
            }
            continue

        # A differently-spelled AI twin is a hallucinated alias, not new
        # evidence. Preserve the manifest record byte-for-byte. Curated table
        # and provisional detections likewise outrank AI even when Gemini
        # echoes the exact package spelling.
        if name != existing["name"] or existing["fallback_tier"] in {
            "table",
            "provisional",
            "learned",
        }:
            continue

        # Relabel: Gemini beats a heuristic or a passthrough, but not a table
        # hit we are confident about.
        if existing["fallback_tier"] in ("heuristic", "passthrough", "dev_tool"):
            existing["technology_role"] = cls.get("technology_role", existing["technology_role"])
        existing["confidence"] = max(
            existing["confidence"], cls.get("confidence", 0.0)
        )
        # The dependency was discovered in a manifest, but its role/layer was
        # supplied by AI. Preserve both facts instead of collapsing provenance
        # to the ambiguous generic value `manifest`.
        existing["detection_source"] = "manifest_ai_inferred"
        if existing["fallback_tier"] not in ("table", "provisional"):
            existing["fallback_tier"] = "ai_classified"
            # Tail entries have no deterministic layer. Trust the layer returned
            # by this same consolidated call, independently of technology_role.
            existing["architectural_layer"] = layer_assignment
            existing["layer_inference_status"] = cls.get("layer_resolution")

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

