"""Definitive per-technology architectural-layer resolution for v2.

Resolution is seed-first, followed by the six roles whose layer is
context-independent. Languages remain null; unresolved ambiguous roles are
handed to AI by the caller.
"""

from __future__ import annotations


UNAMBIGUOUS_ROLE_LAYER: dict[str, str] = {
    "messaging": "messaging",
    "ai_ml": "ai_ml",
    "testing": "testing",
    "infra": "infra",
    "observability": "observability",
    "build": "infra",
}

AMBIGUOUS_ROLES = {"frameworks", "library", "databases"}


# Per-technology knowledge. Keys are normalized lowercase names with separators
# stripped. None is an intentional cross-cutting/null layer, not a missing seed.
TECH_LAYER_SEED: dict[str, str | None] = {
    # Frontend frameworks / UI
    "react": "frontend", "reactdom": "frontend", "vue": "frontend",
    "angular": "frontend", "svelte": "frontend", "sveltekit": "frontend",
    "solid": "frontend", "preact": "frontend", "nextjs": "frontend",
    "next": "frontend", "nuxt": "frontend", "remix": "frontend",
    "gatsby": "frontend", "lit": "frontend", "emberjs": "frontend",
    # Backend frameworks
    "django": "backend", "flask": "backend", "fastapi": "backend",
    "starlette": "backend", "express": "backend", "koa": "backend",
    "nestjs": "backend", "nest": "backend", "fastify": "backend",
    "spring": "backend", "springboot": "backend", "rails": "backend",
    "gin": "backend", "echo": "backend", "fiber": "backend",
    "actixweb": "backend", "axum": "backend", "rocket": "backend",
    "laravel": "backend", "symfony": "backend", "phoenix": "backend",
    # Libraries with a definite layer
    "muimaterial": "frontend", "materialui": "frontend",
    "styledcomponents": "frontend", "tailwindcss": "frontend",
    "reactquery": "frontend", "tanstackreactquery": "frontend",
    "swr": "frontend", "apolloclient": "frontend", "redux": "frontend",
    "axios": "frontend",
    "sqlalchemy": "data", "psycopg2": "data", "psycopg": "data",
    "asyncpg": "data", "gormiogorm": "data", "prisma": "data",
    "mongoose": "data", "sequelize": "data", "alembic": "data",
    # Genuinely cross-cutting libraries
    "lodash": None, "underscore": None, "ramda": None, "datefns": None,
    "moment": None, "guava": None, "hppc": None, "commonsmath3": None,
    "commonslang3": None, "icu4j": None, "pyyaml": None, "tenacity": None,
    # Databases: data vs cache
    "postgresql": "data", "postgres": "data", "mysql": "data",
    "mongodb": "data", "sqlite": "data", "cassandra": "data",
    "elasticsearch": "data", "clickhouse": "data",
    "redis": "cache", "memcached": "cache", "keydb": "cache",
    "hazelcast": "cache", "dragonfly": "cache",
}


def _norm(name: str) -> str:
    tail = name.rpartition(":")[2] if ":" in name else name
    return "".join(character for character in tail.lower() if character.isalnum())


def layer_for(technology_name: str, technology_role: str) -> tuple[str | None, str]:
    """Return ``(architectural_layer, method)`` for one v2 technology.

    Method is one of ``seed_deterministic``, ``role_deterministic``,
    ``languages_null``, or ``needs_ai``.
    """

    role = (technology_role or "").lower()
    key = _norm(technology_name)

    if key in TECH_LAYER_SEED:
        return TECH_LAYER_SEED[key], "seed_deterministic"

    if role in UNAMBIGUOUS_ROLE_LAYER:
        return UNAMBIGUOUS_ROLE_LAYER[role], "role_deterministic"

    if role in ("languages", "language_runtime"):
        return None, "languages_null"

    if role in AMBIGUOUS_ROLES:
        return None, "needs_ai"

    return None, "needs_ai"
