"""Apply pending schema migrations: ``python -m backend.migrate [--database-url URL] [--check]``.

The database comes from --database-url, else DATABASE_DIRECT_URL, else DATABASE_URL. backend/.env
is never read, so this cannot reach a database nobody named. --check only reports the version and
exits non-zero when migrations are pending. See backend/services/schema_migrations.py.
"""

from __future__ import annotations

import argparse
import os
import sys

from backend.services.postgres import make_connection_factory
from backend.services.schema_migrations import apply_pending, applied_version, required_version, schema_problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.getenv("DATABASE_DIRECT_URL") or os.getenv("DATABASE_URL"))
    parser.add_argument("--check", action="store_true", help="report the schema version; apply nothing")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("name the database with --database-url, DATABASE_DIRECT_URL or DATABASE_URL")

    factory = make_connection_factory(args.database_url)
    if args.check:
        conn = factory()
        try:
            problem = schema_problem(conn)
            print(f"schema version {applied_version(conn)}; this code needs {required_version()}")
        finally:
            conn.close()
        if problem:
            print(problem["detail"], file=sys.stderr)
            return 1
        return 0

    applied = apply_pending(factory)
    print(f"Applied {len(applied)} migration(s); schema is at version {required_version()}"
          if applied else f"Schema is up to date at version {required_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
