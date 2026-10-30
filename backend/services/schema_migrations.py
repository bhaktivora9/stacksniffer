"""Schema migrations: numbered SQL files applied once each, on top of the baseline schema.

The baseline is StackSniffer_R1_PostgreSQL_pgvector_Schema/stacksniffer-r1-schema/001-006. Every
later change is a file in its migrations/ directory named NNNN_description.sql, applied in order,
each in its own transaction together with its row in core.schema_migration. A fresh database and
an existing one reach the same schema the same way: baseline, then every migration.

The API needs the newest migration in this checkout (``schema_problem``); ``python -m
backend.migrate`` applies whatever is pending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "StackSniffer_R1_PostgreSQL_pgvector_Schema" / "stacksniffer-r1-schema"
MIGRATIONS_DIR = SCHEMA_DIR / "migrations"
BASELINE_FILES = ("001_extensions_and_namespaces.sql", "002_core_repository_ir.sql", "003_semantic_pgvector.sql",
                  "004_graphrag_runtime.sql", "005_evaluation_and_ml.sql", "006_optional_ann_indexes.sql")
MIGRATE_COMMAND = "python -m backend.migrate"

_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
# One runner at a time per database, whatever the number of replicas or release jobs.
_LOCK_KEY = 0x5354_4b53  # "STKS"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def available_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """The migration files in version order; versions must run 1, 2, 3, ... without gaps."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration file {path.name} is not named NNNN_description.sql")
        found.append(Migration(int(match.group(1)), match.group(2), path))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise ValueError(f"migration versions must be 1..{len(found)} without gaps, found {versions}")
    return found


def required_version(directory: Path = MIGRATIONS_DIR) -> int:
    """The schema version this checkout's code needs: its newest migration."""
    migrations = available_migrations(directory)
    return migrations[-1].version if migrations else 0


def applied_version(conn: Any) -> int | None:
    """The highest migration applied, 0 for a baseline schema with none, None without the baseline."""
    if conn.fetch_scalar("SELECT to_regclass('core.analysis')") is None:
        return None
    if conn.fetch_scalar("SELECT to_regclass('core.schema_migration')") is None:
        return 0
    return conn.fetch_scalar("SELECT coalesce(max(version), 0) FROM core.schema_migration")


def schema_problem(conn: Any, directory: Path = MIGRATIONS_DIR) -> dict[str, Any] | None:
    """None when the database has the schema this code needs; otherwise what is wrong and the fix."""
    required = required_version(directory)
    found = applied_version(conn)
    if found is None:
        return {"required": required, "found": None,
                "detail": "The database has no StackSniffer schema. Apply the baseline schema "
                          f"(run_migrations.sh), then run migrations: {MIGRATE_COMMAND}"}
    if found < required:
        return {"required": required, "found": found,
                "detail": f"Database schema version {required} is required, but the database is at version "
                          f"{found}. Run migrations: {MIGRATE_COMMAND}"}
    if found > required:
        return {"required": required, "found": found,
                "detail": f"The database is at schema version {found}, newer than this code knows "
                          f"(version {required}). Deploy the matching code."}
    return None


def apply_pending(connection_factory: Callable[[], Any], directory: Path = MIGRATIONS_DIR, *,
                  log: Callable[[str], None] = print) -> list[Migration]:
    """Apply every migration newer than the database's version; returns the ones applied."""
    migrations = available_migrations(directory)
    applied: list[Migration] = []
    conn = connection_factory()
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", _LOCK_KEY)
        try:
            current = applied_version(conn)
            if current is None:
                raise RuntimeError("the database has no StackSniffer schema; apply the baseline first "
                                   "(run_migrations.sh)")
            conn.execute("CREATE TABLE IF NOT EXISTS core.schema_migration ("
                         "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                         "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.commit()
            for migration in migrations:
                if migration.version <= current:
                    continue
                log(f"Applying migration {migration.version:04d} {migration.name}")
                try:
                    conn.execute_script(migration.sql())
                    conn.execute("INSERT INTO core.schema_migration (version, name) VALUES (%s, %s)",
                                 migration.version, migration.name)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                applied.append(migration)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", _LOCK_KEY)
            conn.commit()
    finally:
        conn.close()
    return applied
