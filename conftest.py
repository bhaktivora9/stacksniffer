"""Test-session safety net, loaded by pytest before any test module.

1. backend/.env is never loaded: STACKSNIFFER_LOAD_DOTENV=0 is set before anything
   can import backend.main, whose app factory honours it.
2. DATABASE_URL is removed from the environment, so no code path in a test can reach
   a configured (production) database by default.
3. TEST_DATABASE_URL, the only database tests may write to, must be local or an
   explicitly allow-listed disposable host, and must not match any production URL.
   Otherwise the whole session refuses to start.
"""

import os
from pathlib import Path

import pytest

from backend.tests.database_safety import production_database_urls, unsafe_reason

ROOT = Path(__file__).resolve().parent

os.environ["STACKSNIFFER_LOAD_DOTENV"] = "0"
_PRODUCTION_URLS = production_database_urls(ROOT, dict(os.environ))
os.environ.pop("DATABASE_URL", None)


def pytest_configure(config):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return
    allowed = os.environ.get("TEST_DATABASE_ALLOWED_HOSTS", "").split(",")
    problem = unsafe_reason(url, _PRODUCTION_URLS, allowed)
    if problem:
        raise pytest.UsageError(f"Refusing to run tests: {problem}")
