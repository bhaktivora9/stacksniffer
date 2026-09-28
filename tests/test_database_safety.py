"""The test-session guard that keeps tests away from production databases."""

import os
import subprocess
import sys
from pathlib import Path

from backend.tests.database_safety import database_identity, production_database_urls, unsafe_reason

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = "postgresql://owner:secret@ep-snowy-poetry-a1-pooler.us-west-2.aws.neon.tech/neondb?sslmode=require"


def test_local_databases_are_allowed():
    assert unsafe_reason("postgresql://postgres:test@localhost:55432/stacksniffer", [PRODUCTION]) is None
    assert unsafe_reason("postgresql://u@127.0.0.1/db", [PRODUCTION]) is None


def test_the_production_database_is_refused_even_when_allow_listed():
    host = database_identity(PRODUCTION)[0]
    assert "same database" in unsafe_reason(PRODUCTION, [PRODUCTION], [host])
    direct = PRODUCTION.replace("-pooler.", ".")  # the direct endpoint of the same database
    assert "same database" in unsafe_reason(direct, [PRODUCTION], [host])


def test_remote_hosts_need_an_explicit_allow_list():
    branch = "postgresql://owner:secret@ep-test-branch-b2.us-west-2.aws.neon.tech/neondb"
    assert "not local" in unsafe_reason(branch, [PRODUCTION])
    assert unsafe_reason(branch, [PRODUCTION], ["ep-test-branch-b2.us-west-2.aws.neon.tech"]) is None


def test_malformed_urls_are_refused():
    assert unsafe_reason("sqlite:///x.db", []) is not None
    assert unsafe_reason("postgresql:///nohost", []) is not None


def test_production_urls_are_read_from_env_files_without_loading_them(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / ".env").write_text(f"DATABASE_URL={PRODUCTION}\n")
    urls = production_database_urls(tmp_path, {})
    assert urls == [PRODUCTION]
    assert os.environ.get("DATABASE_URL") != PRODUCTION


def test_the_session_never_exposes_database_url():
    import backend.main  # noqa: F401  -- importing the app must not load backend/.env

    assert os.environ.get("STACKSNIFFER_LOAD_DOTENV") == "0"
    assert "DATABASE_URL" not in os.environ


def test_the_session_refuses_a_production_test_database(tmp_path):
    env = {**os.environ, "TEST_DATABASE_URL": PRODUCTION, "DATABASE_URL": PRODUCTION}
    env.pop("TEST_DATABASE_ALLOWED_HOSTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_database_safety.py",
         "-k", "local_databases_are_allowed"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    assert "Refusing to run tests" in result.stdout + result.stderr
