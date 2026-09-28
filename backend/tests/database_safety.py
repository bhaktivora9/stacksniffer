"""Decide whether a database URL is safe for tests to write to.

Tests may only use a disposable database: a local one, or a remote host explicitly
allow-listed in TEST_DATABASE_ALLOWED_HOSTS (e.g. a Neon branch created for testing).
A URL that points at the same database as any configured production URL is refused
even when its host is allow-listed.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Files whose DATABASE_URL counts as production for the comparison (read, never loaded).
PRODUCTION_ENV_FILES = ("backend/.env", ".env", ".env.local")


def database_identity(url: str) -> tuple[str, int, str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # Neon pooled and direct endpoints are the same database: ep-x-pooler.* vs ep-x.*
    host = host.replace("-pooler.", ".")
    return host, parsed.port or 5432, unquote(parsed.path.lstrip("/")), unquote(parsed.username or "")


def production_database_urls(root: Path, environ: dict[str, str]) -> list[str]:
    from dotenv import dotenv_values

    urls = [environ["DATABASE_URL"]] if environ.get("DATABASE_URL") else []
    for name in PRODUCTION_ENV_FILES:
        path = root / name
        if path.is_file():
            value = dotenv_values(path).get("DATABASE_URL")
            if value:
                urls.append(value)
    return urls


def unsafe_reason(url: str, production_urls: Iterable[str], allowed_hosts: Iterable[str] = ()) -> str | None:
    """Why ``url`` must not be used by tests, or None when it is a disposable database."""
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
        return "TEST_DATABASE_URL must be a postgresql:// URL with a host"
    identity = database_identity(url)
    if any(identity == database_identity(other) for other in production_urls):
        return f"TEST_DATABASE_URL points at the same database as a production DATABASE_URL ({identity[0]})"
    host = identity[0]
    allowed = {h.strip().lower() for h in allowed_hosts if h.strip()}
    if host not in LOCAL_HOSTS and host not in allowed:
        return (f"TEST_DATABASE_URL host {host} is not local; to use a disposable remote database, add it to "
                "TEST_DATABASE_ALLOWED_HOSTS")
    return None
