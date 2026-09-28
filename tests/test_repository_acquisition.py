"""Repository acquisition against real local Git repositories served over file://."""

import asyncio
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from backend.services.repository_acquisition import (
    AcquisitionCancelled,
    AcquisitionLimits,
    AcquisitionRequest,
    AcquisitionTimeout,
    CommitMismatch,
    GitCommandFailed,
    GitRepositoryAcquirer,
    InvalidAcquisitionRequest,
    RepositoryTooLarge,
    TooManyFiles,
    exclusion_reason,
    github_remote_url,
    redact,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

GIT_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "core.autocrlf=false")


def git(cwd, *args, input=None, env=None):
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=cwd, input=input, capture_output=True, check=True, env=env
    )
    return result.stdout.decode().strip()


class SourceRepo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        git(path, "init", "--quiet")

    def write(self, files: dict[str, bytes]) -> None:
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def add_special(self, mode: str, path: str, object_id: str) -> None:
        """Stage a symlink (120000) or gitlink (160000) without needing one on disk."""
        git(self.path, "update-index", "--add", "--cacheinfo", f"{mode},{object_id},{path}")

    def blob(self, content: bytes) -> str:
        return git(self.path, "hash-object", "-w", "--stdin", input=content)

    def commit(self, files: dict[str, bytes] | None = None) -> str:
        files = files or {}
        self.write(files)
        # Not --all: that would drop staged symlink/gitlink entries that have no file on disk.
        for name in [*files, ".gitmodules"]:
            if (self.path / name).exists():
                git(self.path, "add", "--force", "--", name)
        git(self.path, "commit", "--quiet", "--allow-empty", "-m", "commit")
        return git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    return SourceRepo(tmp_path / "source-repo")


@pytest.fixture
def workdir(tmp_path):
    return tmp_path / "acquisitions"


def acquirer_for(repo, workdir, **limits):
    return GitRepositoryAcquirer(
        AcquisitionLimits(**limits),
        workdir_root=workdir,
        remote_url_for=lambda key: repo.path.as_uri(),
        allowed_protocols=("file",),
    )


def request(sha):
    return AcquisitionRequest("github:test-owner/test-repo", sha, uuid4(), uuid4())


def acquire(acquirer, sha, inspect=lambda snapshot: None, cancel_event=None):
    """Acquire, run ``inspect`` while the snapshot exists, and return the snapshot."""

    async def run():
        async with acquirer.acquire(request(sha), cancel_event=cancel_event) as snapshot:
            assert snapshot.root.is_dir()
            inspect(snapshot)
            return snapshot

    return asyncio.run(run())


def assert_cleaned(workdir):
    assert not workdir.exists() or list(workdir.iterdir()) == []


# --- successful acquisition ------------------------------------------------------------------


def test_acquires_the_exact_commit_byte_for_byte(repo, workdir):
    first = repo.commit({
        "src/app.py": b"print('first')\n",
        "README.md": b"line one\nline two\n",
        # A normal checkout would write CRLF here; the snapshot must match the blob byte-for-byte.
        ".gitattributes": b"* text eol=crlf\n",
    })
    repo.commit({"src/app.py": b"print('second')\n", "later.txt": b"added later\n"})
    seen = {}

    def inspect(snapshot):
        seen["files"] = {path: (snapshot.root / path).read_bytes() for path in snapshot.files}
        seen["entries"] = sorted(p.relative_to(snapshot.root).as_posix() for p in snapshot.root.rglob("*") if p.is_file())

    snapshot = acquire(acquirer_for(repo, workdir), first, inspect)

    assert snapshot.commit_sha == first
    assert snapshot.files == (".gitattributes", "README.md", "src/app.py")
    assert seen["entries"] == list(snapshot.files)  # no .git or anything else on disk
    assert seen["files"]["src/app.py"] == b"print('first')\n"
    assert seen["files"]["README.md"] == b"line one\nline two\n"
    assert snapshot.file_count == 3
    assert snapshot.checkout_bytes == sum(len(content) for content in seen["files"].values())
    assert snapshot.duration_seconds > 0
    assert_cleaned(workdir)


def test_provenance_is_durable_and_never_contains_the_temporary_path(repo, workdir):
    sha = repo.commit({"a.py": b"x = 1\n"})
    snapshot = acquire(acquirer_for(repo, workdir), sha)

    provenance = snapshot.provenance()
    assert provenance["verified_commit_sha"] == sha
    assert provenance["file_count"] == 1
    assert str(snapshot.root) not in json.dumps(provenance)
    assert str(workdir) not in json.dumps(provenance)


def test_excluded_paths_never_reach_the_snapshot(repo, workdir):
    sha = repo.commit({
        "main.py": b"print('kept')\n",
        ".env": b"SECRET=1\n",
        ".env.production": b"SECRET=1\n",
        ".env.example": b"SECRET=\n",
        "certs/server.pem": b"-----BEGIN PRIVATE KEY-----\n",
        "node_modules/lib/index.js": b"module.exports = 1\n",
        "static/app.min.js": b"var a=1\n",
        "logo.png": b"\x89PNG\r\n",
        "data.txt": b"text with a \x00 byte\n",
        ".hg/store": b"vcs\n",
        "big.txt": b"x" * 2048,
        "package-lock.json": b"{}\n",
    })

    snapshot = acquire(acquirer_for(repo, workdir, max_file_bytes=1024), sha)

    assert snapshot.files == (".env.example", "main.py", "package-lock.json")
    assert snapshot.excluded == {"binary": 2, "generated": 2, "secret": 3, "too_large": 1, "vcs": 1}


def test_exclusion_rules():
    assert exclusion_reason("config/.env.local") == "secret"
    assert exclusion_reason("deploy/id_rsa") == "secret"
    assert exclusion_reason("frontend/node_modules/react/index.js") == "generated"
    assert exclusion_reason("src/node_modules.py") is None
    assert exclusion_reason("docs/diagram.svg") is None
    assert exclusion_reason("pkg/.svn/entries") == "vcs"


# --- submodules, symlinks, execution ---------------------------------------------------------


def test_submodules_remain_uninitialized(repo, workdir, tmp_path):
    other = SourceRepo(tmp_path / "other-repo")
    other_sha = other.commit({"inside-submodule.py": b"print('never fetched')\n"})
    repo.write({".gitmodules": f'[submodule "vendor/sub"]\n\tpath = vendor/sub\n\turl = {other.path.as_uri()}\n'.encode()})
    repo.add_special("160000", "vendor/sub", other_sha)
    sha = repo.commit({"main.py": b"x = 1\n"})

    snapshot = acquire(acquirer_for(repo, workdir), sha, lambda s: assert_not_exists(s.root / "vendor"))

    assert snapshot.excluded == {"submodule": 1}
    assert "vendor/sub" not in snapshot.files


def assert_not_exists(path):
    assert not path.exists()


def test_symlinks_are_never_materialized_or_followed(repo, workdir, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not read")
    repo.add_special("120000", "escape.txt", repo.blob(str(outside).encode()))
    repo.add_special("120000", "escape-dir", repo.blob(b"../../.."))
    sha = repo.commit({"main.py": b"x = 1\n"})

    def inspect(snapshot):
        assert not (snapshot.root / "escape.txt").exists()
        assert not (snapshot.root / "escape-dir").exists()
        assert not any(path.is_symlink() for path in snapshot.root.rglob("*"))

    snapshot = acquire(acquirer_for(repo, workdir), sha, inspect)

    assert snapshot.files == ("main.py",)
    assert snapshot.excluded == {"symlink": 2}
    assert outside.read_text() == "do not read"


def test_repository_contents_and_ambient_git_config_are_never_executed(repo, workdir, tmp_path, monkeypatch):
    sentinel = tmp_path / "executed.txt"
    touch = f"touch '{sentinel.as_posix()}'"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("post-checkout", "reference-transaction", "post-index-change"):
        (hooks / hook).write_text(f"#!/bin/sh\n{touch}\n", newline="\n")
        (hooks / hook).chmod(0o755)
    # A hostile ambient config: filters, LFS smudge, hooks and credential helpers that all run code.
    evil_config = tmp_path / "evil-gitconfig"
    evil_config.write_text(
        f'[filter "evil"]\n\tsmudge = {touch}\n\trequired = true\n'
        f'[filter "lfs"]\n\tsmudge = {touch}\n\tprocess = {touch}\n\trequired = true\n'
        f"[core]\n\thooksPath = {hooks.as_posix()}\n"
        f"[credential]\n\thelper = !{touch}\n"
    )
    sha = repo.commit({
        ".gitattributes": b"*.py filter=evil\n*.bin filter=lfs diff=lfs merge=lfs -text\n",
        "setup.py": f"open({str(sentinel)!r}, 'w').write('setup ran')\n".encode(),
        "package.json": json.dumps({"scripts": {"postinstall": touch}}).encode(),
        "Makefile": f"all:\n\t{touch}\n".encode(),
    })
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil_config))

    # Control: a plain clone under the same environment does run the hostile configuration.
    subprocess.run(["git", "clone", "--quiet", repo.path.as_uri(), str(tmp_path / "control")], capture_output=True)
    assert sentinel.exists()
    sentinel.unlink()

    snapshot = acquire(acquirer_for(repo, workdir), sha)

    assert not sentinel.exists()
    assert "setup.py" in snapshot.files
    assert_cleaned(workdir)


# --- failures --------------------------------------------------------------------------------


def expect_failure(acquirer, sha, error, cancel_event=None):
    with pytest.raises(error) as caught:
        acquire(acquirer, sha, cancel_event=cancel_event)
    return caught.value


def test_unknown_commit_is_a_failed_git_command(repo, workdir):
    repo.commit({"a.py": b"x\n"})
    error = expect_failure(acquirer_for(repo, workdir), "0" * 40, GitCommandFailed)
    assert error.failure_code == "GIT_COMMAND_FAILED"
    assert error.failure_detail["git_command"] == "fetch"
    assert_cleaned(workdir)


def test_unreachable_remote_is_a_failed_git_command(tmp_path, workdir):
    acquirer = GitRepositoryAcquirer(
        workdir_root=workdir,
        remote_url_for=lambda key: (tmp_path / "missing").as_uri(),
        allowed_protocols=("file",),
    )
    expect_failure(acquirer, "a" * 40, GitCommandFailed)
    assert_cleaned(workdir)


def test_disallowed_protocol_is_refused(repo, workdir):
    sha = repo.commit({"a.py": b"x\n"})
    acquirer = GitRepositoryAcquirer(workdir_root=workdir, remote_url_for=lambda key: repo.path.as_uri())
    expect_failure(acquirer, sha, GitCommandFailed)  # file:// is not in the default https-only allow list
    assert_cleaned(workdir)


def test_head_that_does_not_match_the_resolved_sha_is_rejected(repo, workdir):
    sha = repo.commit({"a.py": b"x\n"})

    class LyingGit(GitRepositoryAcquirer):
        def _git(self, attempt, *args, **kwargs):
            if args[:1] == ("rev-parse",):
                return b"f" * 40 + b"\n"
            return super()._git(attempt, *args, **kwargs)

    acquirer = LyingGit(workdir_root=workdir, remote_url_for=lambda key: repo.path.as_uri(), allowed_protocols=("file",))
    error = expect_failure(acquirer, sha, CommitMismatch)
    assert error.failure_code == "COMMIT_SHA_MISMATCH"
    assert error.failure_detail["expected_sha"] == sha
    assert_cleaned(workdir)


def test_oversized_repository_fails_before_writing_files(repo, workdir):
    sha = repo.commit({"a.txt": b"a" * 600, "b.txt": b"b" * 600})
    error = expect_failure(acquirer_for(repo, workdir, max_checkout_bytes=1000), sha, RepositoryTooLarge)
    assert error.failure_code == "REPOSITORY_TOO_LARGE"
    assert error.failure_detail["max_checkout_bytes"] == 1000
    assert_cleaned(workdir)


def test_excessive_file_count_fails(repo, workdir):
    sha = repo.commit({f"f{i}.py": b"x\n" for i in range(3)})
    error = expect_failure(acquirer_for(repo, workdir, max_file_count=2), sha, TooManyFiles)
    assert error.failure_code == "REPOSITORY_TOO_MANY_FILES"
    assert_cleaned(workdir)


@pytest.fixture
def hanging_git(tmp_path):
    """A stand-in for git that never finishes, to exercise the watchdog."""
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n")
    return (sys.executable, str(script))


def test_timeout_kills_git_and_cleans_up(workdir, hanging_git):
    acquirer = GitRepositoryAcquirer(
        AcquisitionLimits(timeout_seconds=0.5), workdir_root=workdir, git_command=hanging_git,
        remote_url_for=lambda key: "https://example.invalid/repo.git",
    )
    started = time.monotonic()
    error = expect_failure(acquirer, "a" * 40, AcquisitionTimeout)
    assert time.monotonic() - started < 15
    assert error.failure_code == "ACQUISITION_TIMEOUT"
    assert_cleaned(workdir)


def test_cancellation_event_stops_acquisition_and_cleans_up(workdir, hanging_git):
    acquirer = GitRepositoryAcquirer(
        workdir_root=workdir, git_command=hanging_git, remote_url_for=lambda key: "https://example.invalid/repo.git",
    )
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    expect_failure(acquirer, "a" * 40, AcquisitionCancelled, cancel_event=cancel)
    assert time.monotonic() - started < 15
    assert_cleaned(workdir)


def test_task_cancellation_stops_git_and_cleans_up(workdir, hanging_git):
    acquirer = GitRepositoryAcquirer(
        workdir_root=workdir, git_command=hanging_git, remote_url_for=lambda key: "https://example.invalid/repo.git",
    )

    async def run():
        async def use():
            async with acquirer.acquire(request("a" * 40)):
                pass

        task = asyncio.create_task(use())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(run())
    assert_cleaned(workdir)


def test_cleanup_after_downstream_failure(repo, workdir):
    sha = repo.commit({"a.py": b"x\n"})

    def explode(snapshot):
        raise RuntimeError("extraction blew up")

    with pytest.raises(RuntimeError, match="extraction blew up"):
        acquire(acquirer_for(repo, workdir), sha, explode)
    assert_cleaned(workdir)


def test_invalid_requests_are_rejected_before_touching_disk(workdir):
    acquirer = GitRepositoryAcquirer(workdir_root=workdir)
    expect_failure(acquirer, "HEAD", InvalidAcquisitionRequest)
    with pytest.raises(InvalidAcquisitionRequest):
        github_remote_url("gitlab:someone/repo")
    assert not workdir.exists()


def test_remote_url_is_rebuilt_from_the_canonical_key():
    assert github_remote_url("github:Owner/Repo") == "https://github.com/owner/repo.git"


def test_credentials_and_tokens_are_redacted():
    text = redact(
        "fatal: https://user:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r.git "
        "Authorization: Bearer secret-value github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ_123 ?token=abc"
    )
    assert "ghp_" not in text and "github_pat_" not in text
    assert "secret-value" not in text and "token=abc" not in text
    assert "user:" not in text


# --- size policy: each layer tested on its own -----------------------------------------------


def test_reported_size_gate_rejects_before_anything_is_fetched(workdir, tmp_path):
    async def huge(key):
        return 10 * 1024 * 1024

    acquirer = GitRepositoryAcquirer(
        AcquisitionLimits(max_workdir_bytes=1024 * 1024), workdir_root=workdir, size_probe=huge,
        git_command=("git-must-not-run",),
    )
    error = expect_failure(acquirer, "a" * 40, RepositoryTooLarge)
    assert error.failure_detail["source"] == "provider_metadata"
    assert error.failure_detail["reported_bytes"] == 10 * 1024 * 1024
    assert not workdir.exists()


def test_unavailable_size_probe_falls_back_to_the_local_limits(repo, workdir):
    sha = repo.commit({"a.py": b"x\n"})

    async def broken(key):
        raise ConnectionError("GitHub is down")

    acquirer = acquirer_for(repo, workdir)
    acquirer.size_probe = broken
    assert acquire(acquirer, sha).files == ("a.py",)


def test_post_checkout_validation_rejects_an_oversized_materialized_tree(tmp_path):
    from collections import Counter

    from backend.services.repository_acquisition import _Attempt

    workdir = tmp_path / "attempt"
    (workdir / "source").mkdir(parents=True)
    (workdir / "source" / "big.txt").write_bytes(b"x" * 2000)
    acquirer = GitRepositoryAcquirer(AcquisitionLimits(max_checkout_bytes=1000))
    attempt = _Attempt(request("a" * 40), workdir, time.monotonic() + 60, threading.Event(), acquirer.limits)

    with pytest.raises(RepositoryTooLarge) as caught:
        acquirer._verify_tree(attempt, {"big.txt"}, Counter())
    assert caught.value.failure_detail["checkout_bytes"] == 2000


def test_post_checkout_validation_rejects_files_nobody_asked_for(tmp_path):
    from collections import Counter

    from backend.services.repository_acquisition import UnsafeCheckout, _Attempt

    workdir = tmp_path / "attempt"
    (workdir / "source").mkdir(parents=True)
    (workdir / "source" / "planted.py").write_bytes(b"x")
    acquirer = GitRepositoryAcquirer()
    attempt = _Attempt(request("a" * 40), workdir, time.monotonic() + 60, threading.Event(), acquirer.limits)

    with pytest.raises(UnsafeCheckout):
        acquirer._verify_tree(attempt, set(), Counter())


@pytest.fixture
def growing_git(tmp_path):
    """Fake git: succeeds instantly except for fetch, which keeps writing into its working directory."""
    script = tmp_path / "grow.py"
    script.write_text(
        "import sys, time\n"
        "if 'fetch' not in sys.argv:\n"
        "    sys.exit(0)\n"
        "with open('runaway.pack', 'wb') as out:\n"
        "    for _ in range(600):\n"
        "        out.write(b'x' * 256 * 1024); out.flush(); time.sleep(0.05)\n"
    )
    return (sys.executable, str(script))


def test_watchdog_kills_git_when_the_attempt_directory_grows_past_budget(workdir, growing_git):
    acquirer = GitRepositoryAcquirer(
        AcquisitionLimits(max_workdir_bytes=1024 * 1024, growth_check_seconds=0.1),
        workdir_root=workdir, git_command=growing_git,
        remote_url_for=lambda key: "https://example.invalid/repo.git",
    )
    started = time.monotonic()
    error = expect_failure(acquirer, "a" * 40, RepositoryTooLarge)
    assert time.monotonic() - started < 15  # killed long before the fake finishes (~30s)
    assert error.failure_detail["source"] == "workdir_growth"
    assert error.failure_detail["workdir_bytes"] > 1024 * 1024
    assert_cleaned(workdir)


# --- attempt directories and orphan cleanup --------------------------------------------------


def test_attempt_directory_is_named_and_marked_for_its_attempt(repo, workdir):
    from backend.services.repository_acquisition import MARKER_NAME

    sha = repo.commit({"a.py": b"x\n"})
    seen = {}

    def inspect(snapshot):
        attempt_dir = snapshot.root.parent
        seen["name"] = attempt_dir.name
        seen["marker"] = json.loads((attempt_dir / MARKER_NAME).read_text())
        seen["attempt_id"] = str(snapshot.attempt_id)
        seen["files"] = snapshot.files
        seen["in_root"] = (snapshot.root / MARKER_NAME).exists()

    acquire(acquirer_for(repo, workdir), sha, inspect)

    assert seen["name"] == f"stacksniffer-acquisition-{seen['attempt_id']}"
    assert seen["marker"]["attempt_id"] == seen["attempt_id"]
    assert MARKER_NAME not in seen["files"] and not seen["in_root"]  # never part of the snapshot
    assert_cleaned(workdir)


def make_attempt_dir(root, *, created_at, attempt_id=None, marker=True, name=None):
    from backend.services.repository_acquisition import MARKER_NAME

    attempt_id = attempt_id or str(uuid4())
    path = root / (name or f"stacksniffer-acquisition-{attempt_id}")
    (path / "git").mkdir(parents=True)
    (path / "git" / "pack").write_bytes(b"x")
    if marker:
        (path / MARKER_NAME).write_text(json.dumps(
            {"analysis_id": str(uuid4()), "attempt_id": attempt_id, "created_at": created_at}
        ))
    return path


def test_startup_sweep_removes_only_stale_stacksniffer_owned_directories(workdir):
    now = time.time()
    stale = make_attempt_dir(workdir, created_at=now - 10_000)
    fresh = make_attempt_dir(workdir, created_at=now - 60)
    unmarked = make_attempt_dir(workdir, created_at=now - 10_000, marker=False)
    mismatched = make_attempt_dir(workdir, created_at=now - 10_000, name=f"stacksniffer-acquisition-{uuid4()}")
    foreign = make_attempt_dir(workdir, created_at=now - 10_000, name="someone-elses-tmp")
    stray_file = workdir / f"stacksniffer-acquisition-{uuid4()}"
    stray_file.write_text("not a directory")

    removed = GitRepositoryAcquirer(workdir_root=workdir).remove_stale_acquisitions(3600, now=now)

    assert removed == [stale]
    assert not stale.exists()
    for kept in (fresh, unmarked, mismatched, foreign, stray_file):
        assert kept.exists()


def test_startup_sweep_tolerates_missing_or_already_removed_directories(tmp_path):
    from backend.services.repository_acquisition import _remove_tree

    assert GitRepositoryAcquirer(workdir_root=tmp_path / "never-created").remove_stale_acquisitions(0) == []
    assert _remove_tree(tmp_path / "already-gone") is True


def test_a_second_acquisition_never_adopts_an_existing_attempt_directory(repo, workdir):
    from backend.services.repository_acquisition import RepositoryAcquisitionError

    sha = repo.commit({"a.py": b"x\n"})
    same = request(sha)
    existing = make_attempt_dir(workdir, created_at=time.time(), attempt_id=str(same.attempt_id))

    async def run():
        async with acquirer_for(repo, workdir).acquire(same):
            pass

    with pytest.raises(RepositoryAcquisitionError):
        asyncio.run(run())
    assert existing.exists()  # left for its owner
