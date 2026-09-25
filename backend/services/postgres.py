"""Small PostgreSQL adapter used by request and worker services."""

from __future__ import annotations

import os
from typing import Any


class PostgresUnavailable(RuntimeError):
    """Raised when PostgreSQL support is requested but not installed/configured."""


class PostgresConnection:
    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, query: str, *args: Any) -> int:
        """Run a statement and return its row count (read before the cursor closes)."""
        with self._connection.cursor() as cursor:
            cursor.execute(query, args)
            return cursor.rowcount

    def fetch_one(self, query: str, *args: Any) -> Any:
        with self._connection.cursor() as cursor:
            cursor.execute(query, args)
            return cursor.fetchone()

    def fetch_all(self, query: str, *args: Any) -> list[Any]:
        with self._connection.cursor() as cursor:
            cursor.execute(query, args)
            return list(cursor.fetchall())

    def fetch_scalar(self, query: str, *args: Any) -> Any:
        row = self.fetch_one(query, *args)
        return row[0] if row is not None else None

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def make_connection_factory(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise PostgresUnavailable("DATABASE_URL is not configured")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise PostgresUnavailable(
            "psycopg is required for PostgreSQL persistence; install backend requirements"
        ) from exc

    def connect() -> PostgresConnection:
        return PostgresConnection(psycopg.connect(url))

    return connect
