"""
backend/services/dep_fallback.py

Deterministic dependency classification. No AI, no network, no I/O.

WHY THIS EXISTS
---------------
Phase 2a (Gemini dep classification) was a GATE: raw deps flowed through it and
if the call failed they were discarded. Two consecutive runs proved the cost —
fastapi and vercel/next.js both returned DEP_CLASSIFICATION_FAILED, and both
produced a stack with every category empty except languages. next.js declares
100+ dependencies in its root package.json. Phase 1 extracted all of them.
All of them were dropped because one Gemini call died.

The fix is not "a better table". It is an invariant:

    Phase 1 extracted N deps  ->  the output contains N deps.

Classification decides HOW a dep is labelled. It never decides WHETHER it
exists. build_base_detections() is an identity transform over raw_deps: it
always returns one record per dep, whatever happens upstream. Gemini then
enriches that base — it can relabel and raise confidence, but it cannot delete.

CONFIDENCE TIERS
----------------
    0.85  table       — curated ecosystem map, high precision
    0.55  heuristic   — name pattern match, plausible but unverified
    0.40  passthrough — declared in a manifest, category unknown

Passthrough matters most. An unrecognised dep is still DECLARED IN A MANIFEST,
which is stronger evidence than anything the AI layer infers from a file tree.
Emitting it at 0.40 in `library` is honest. Dropping it is not.
"""

from __future__ import annotations

import re

__all__ = [
    "build_base_detections",
    "enrich_with_classifications",
    "classify_dep",
    "assert_deps_survived",
]

# ── Ecosystem tables ─────────────────────────────────────────────────────
# Not exhaustive by design — the heuristic and passthrough tiers catch the
# tail. Only add entries you are confident about; a wrong table entry is
# worse than a passthrough, because it looks authoritative.

_NPM = {
    # frameworks
    "react": "frameworks", "react-dom": "frameworks", "next": "frameworks",
    "vue": "frameworks", "svelte": "frameworks", "@angular/core": "frameworks",
    "express": "frameworks", "fastify": "frameworks", "koa": "frameworks",
    "@nestjs/core": "frameworks", "remix": "frameworks", "astro": "frameworks",
    "solid-js": "frameworks", "preact": "frameworks",
    # testing
    "jest": "testing", "vitest": "testing", "mocha": "testing",
    "chai": "testing", "jasmine": "testing", "ava": "testing",
    "playwright": "testing", "@playwright/test": "testing",
    "cypress": "testing", "puppeteer": "testing", "supertest": "testing",
    "@testing-library/react": "testing", "@testing-library/jest-dom": "testing",
    # infra / build
    "webpack": "infra", "vite": "infra", "rollup": "infra",
    "esbuild": "infra", "parcel": "infra", "turbo": "infra",
    "@swc/core": "infra", "swc": "infra", "babel": "infra",
    "@babel/core": "infra", "nodemon": "infra", "pm2": "infra",
    # databases
    "pg": "databases", "mysql": "databases", "mysql2": "databases",
    "sqlite3": "databases", "better-sqlite3": "databases",
    "mongodb": "databases", "mongoose": "databases",
    "redis": "databases", "ioredis": "databases",
    "prisma": "databases", "@prisma/client": "databases",
    "drizzle-orm": "databases", "typeorm": "databases", "sequelize": "databases",
    "knex": "databases", "@supabase/supabase-js": "databases",
    "elasticsearch": "databases", "@elastic/elasticsearch": "databases",
    # messaging
    "kafkajs": "messaging", "amqplib": "messaging", "bullmq": "messaging",
    "bull": "messaging", "nats": "messaging", "mqtt": "messaging",
    "socket.io": "messaging", "ws": "messaging",
    # ai_ml
    "openai": "ai_ml", "@anthropic-ai/sdk": "ai_ml", "langchain": "ai_ml",
    "@google/generative-ai": "ai_ml", "ai": "ai_ml",
    "@tensorflow/tfjs": "ai_ml", "onnxruntime-node": "ai_ml",
    # library
    "lodash": "library", "axios": "library", "zod": "library",
    "date-fns": "library", "rxjs": "library", "immer": "library",
    "zustand": "library", "redux": "library", "@reduxjs/toolkit": "library",
    "tailwindcss": "library", "styled-components": "library",
}

_PYPI = {
    # frameworks
    "fastapi": "frameworks", "django": "frameworks", "flask": "frameworks",
    "starlette": "frameworks", "sanic": "frameworks", "tornado": "frameworks",
    "aiohttp": "frameworks", "litestar": "frameworks", "quart": "frameworks",
    "bottle": "frameworks", "pyramid": "frameworks",
    # testing
    "pytest": "testing", "pytest-asyncio": "testing", "pytest-cov": "testing",
    "unittest2": "testing", "hypothesis": "testing", "tox": "testing",
    "nose": "testing", "coverage": "testing", "mock": "testing",
    # databases
    "sqlalchemy": "databases", "psycopg2": "databases",
    "psycopg2-binary": "databases", "psycopg": "databases",
    "asyncpg": "databases", "pymongo": "databases", "motor": "databases",
    "redis": "databases", "aioredis": "databases", "alembic": "databases",
    "peewee": "databases", "elasticsearch": "databases",
    "chromadb": "databases", "pinecone-client": "databases", "qdrant-client": "databases",
    # messaging
    "celery": "messaging", "kombu": "messaging", "kafka-python": "messaging",
    "confluent-kafka": "messaging", "pika": "messaging", "aiokafka": "messaging",
    # ai_ml
    "torch": "ai_ml", "tensorflow": "ai_ml", "transformers": "ai_ml",
    "scikit-learn": "ai_ml", "numpy": "ai_ml", "pandas": "ai_ml",
    "scipy": "ai_ml", "langchain": "ai_ml", "openai": "ai_ml",
    "anthropic": "ai_ml", "google-generativeai": "ai_ml",
    "tokenizers": "ai_ml", "safetensors": "ai_ml", "huggingface-hub": "ai_ml",
    "datasets": "ai_ml", "accelerate": "ai_ml", "ray": "ai_ml",
    # infra
    "uvicorn": "infra", "gunicorn": "infra", "hypercorn": "infra",
    "docker": "infra", "kubernetes": "infra", "boto3": "infra",
    # library
    "pydantic": "library", "attrs": "library", "click": "library",
    "typer": "library", "requests": "library", "httpx": "library",
    "rich": "library", "python-dotenv": "library", "jinja2": "library",
}

# Maven: keyed on GROUP ID, not artifact. org.apache.kafka:kafka-clients is
# Kafka; the artifact alone ("kafka-clients") is ambiguous across ecosystems.
_MAVEN_GROUP = {
    "org.springframework": "frameworks",
    "org.springframework.boot": "frameworks",
    "io.quarkus": "frameworks", "io.micronaut": "frameworks",
    "io.vertx": "frameworks", "com.google.inject": "frameworks",
    "org.junit.jupiter": "testing", "org.junit": "testing",
    "junit": "testing", "org.mockito": "testing",
    "org.testcontainers": "testing", "org.assertj": "testing",
    "org.apache.kafka": "messaging", "org.apache.pulsar": "messaging",
    "com.rabbitmq": "messaging", "org.apache.activemq": "messaging",
    "org.postgresql": "databases", "mysql": "databases",
    "org.hibernate": "databases", "org.mongodb": "databases",
    "redis.clients": "databases", "co.elastic.clients": "databases",
    "org.elasticsearch": "databases",
    "org.apache.spark": "ai_ml", "org.deeplearning4j": "ai_ml",
    "com.google.cloud": "infra", "software.amazon.awssdk": "infra",
    "org.slf4j": "library", "com.fasterxml.jackson.core": "library",
    "com.google.guava": "library", "org.projectlombok": "library",
}

_CARGO = {
    "actix-web": "frameworks", "axum": "frameworks", "rocket": "frameworks",
    "warp": "frameworks", "tide": "frameworks", "poem": "frameworks",
    "tokio": "infra", "async-std": "infra", "rayon": "infra",
    "sqlx": "databases", "diesel": "databases", "sea-orm": "databases",
    "redis": "databases", "mongodb": "databases",
    "rdkafka": "messaging", "lapin": "messaging",
    "candle-core": "ai_ml", "tch": "ai_ml", "ort": "ai_ml",
    "serde": "library", "serde_json": "library", "clap": "library",
    "anyhow": "library", "thiserror": "library", "napi": "library",
    "swc_core": "infra", "criterion": "testing", "proptest": "testing",
}

_GO = {
    "github.com/gin-gonic/gin": "frameworks",
    "github.com/labstack/echo": "frameworks",
    "github.com/gofiber/fiber": "frameworks",
    "github.com/go-chi/chi": "frameworks",
    "gorm.io/gorm": "databases",
    "github.com/jackc/pgx": "databases",
    "go.mongodb.org/mongo-driver": "databases",
    "github.com/redis/go-redis": "databases",
    "github.com/IBM/sarama": "messaging",
    "github.com/Shopify/sarama": "messaging",
    "github.com/nats-io/nats.go": "messaging",
    "github.com/stretchr/testify": "testing",
    "k8s.io/client-go": "infra", "github.com/docker/docker": "infra",
    "github.com/spf13/cobra": "library",
}

_ECOSYSTEM_TABLES = {
    "npm": _NPM, "package.json": _NPM,
    "pypi": _PYPI, "pyproject.toml": _PYPI, "requirements.txt": _PYPI,
    "setup.py": _PYPI, "setup.cfg": _PYPI, "Pipfile": _PYPI,
    "maven": _MAVEN_GROUP, "pom.xml": _MAVEN_GROUP,
    "gradle": _MAVEN_GROUP, "build.gradle": _MAVEN_GROUP,
    "cargo": _CARGO, "Cargo.toml": _CARGO,
    "go": _GO, "go.mod": _GO,
}

# ── Heuristics (tier 2) ──────────────────────────────────────────────────
# Ordered: first match wins. Deliberately conservative — a wrong heuristic is
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

    table = _ECOSYSTEM_TABLES.get(ecosystem or "", {})

    # Ecosystem-specific key shaping
    lookup = raw
    if table is _MAVEN_GROUP or ":" in raw:
        lookup = _norm(_maven_group(raw))
    elif table is _GO or raw.startswith("github.com/"):
        lookup = _norm(_go_module_root(raw))

    # Dev tooling — declared, but not part of the product stack.
    if lookup in _DEV_TOOLING or raw in _DEV_TOOLING:
        return "library", 0.30, "dev_tool"

    # Tier 1: exact table hit
    if lookup in table:
        return table[lookup], 0.85, "table"

    # Tier 1b: try every table when the ecosystem is unknown. Lower confidence:
    # a cross-ecosystem hit is a weaker signal than one we expected to find.
    if not table:
        for candidate in (_NPM, _PYPI, _MAVEN_GROUP, _CARGO, _GO):
            if lookup in candidate:
                return candidate[lookup], 0.70, "table"

    # Scoped npm: @scope/pkg -> try the bare package name
    if raw.startswith("@") and "/" in raw:
        bare = raw.split("/", 1)[1]
        if bare in table:
            return table[bare], 0.70, "table"

    # Tier 2: name heuristics
    for pattern, category in _HEURISTICS:
        if pattern.search(lookup):
            return category, 0.55, "heuristic"

    # Tier 3: passthrough. Declared in a manifest, so it exists. We just don't
    # know what it is — which is a labelling gap, not grounds for deletion.
    return "library", 0.40, "passthrough"


def build_base_detections(raw_deps: list[dict]) -> list[dict]:
    """
    Identity transform over Phase 1 output. THE invariant lives here:
    len(output) == len(deduped input), always, with no AI involved.

    raw_deps items: {name, scope, origin, matched_file, version_spec, ecosystem}
    """
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
        })
    return out


def enrich_with_classifications(
    base: list[dict],
    classifications: list[dict],
) -> list[dict]:
    """
    Overlay Gemini's classifications onto the base.

    Enrichment ONLY. It may relabel a category and raise confidence. It may add
    a tech the base missed. It may NOT remove anything — that is the whole
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
        existing["fallback_tier"] = "ai_classified"

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
                f"output. Classification cannot delete deps — this is a bug."
            ),
            "field": "detections",
            "demote": True,
        }
    return None