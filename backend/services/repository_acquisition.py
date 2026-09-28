"""Materialize an immutable, verified snapshot of one repository commit.

The acquirer fetches exactly one commit (shallow, no tags, no submodules) into
an attempt-scoped temporary directory, verifies that the detached ``HEAD`` is
the resolved SHA, and writes the selected blobs byte-for-byte into a plain
source tree. Files are written from ``git cat-file --batch`` rather than a
checkout, so no filter, hook, LFS smudge or attribute conversion ever runs,
symlinks and submodules are never materialized, and only paths that passed the
exclusion rules and resource limits touch the disk.

The snapshot is ephemeral: it exists only inside ``acquire(...)`` and is
removed on success, failure, timeout and cancellation. Its temporary path must
never be persisted; ``RepositorySnapshot.provenance()`` is the durable record.
Each attempt directory carries an ownership marker so that directories
orphaned by a crashed process can be swept on startup
(``remove_stale_acquisitions``) without touching anything else in the
temporary directory.

Size enforcement is layered, because Git owns the network stream and no exact
download-byte cap is possible from outside it:

1. Pre-acquisition gate: the provider-reported repository size (GitHub's is an
   approximate figure that includes history) must be within
   ``max_workdir_bytes``.
2. Pre-write policy: the selected blobs' sizes, read from the fetched tree,
   must fit ``max_checkout_bytes`` and ``max_file_count`` before any file is
   written.
3. Post-checkout validation: the materialized tree is measured on disk and
   rejected if it exceeds ``max_checkout_bytes``, before extraction sees it.
4. Best-effort watchdog: while Git fetches, the attempt directory is measured
   periodically and Git is killed once it grows past ``max_workdir_bytes``.
   Growth between checks can overshoot the limit by up to one interval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

try:
    from services.repository_resolver import RepositoryResolutionError, canonicalize_repository_url
except ModuleNotFoundError:
    from backend.services.repository_resolver import RepositoryResolutionError, canonicalize_repository_url

logger = logging.getLogger(__name__)

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_STDERR_LIMIT = 2000
_BINARY_SNIFF_BYTES = 8000
_WORKDIR_ROOT_NAME = "stacksniffer-acquisitions"
ATTEMPT_DIR_PREFIX = "stacksniffer-acquisition-"
MARKER_NAME = ".stacksniffer-acquisition.json"


# --- errors ----------------------------------------------------------------------------------

_REDACTIONS = (
    (re.compile(r"(\w+://)[^/@\s]+@"), r"\1***@"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "***"),
    (re.compile(r"(?i)\b(authorization:\s*)(bearer|basic|token)\s+\S+"), r"\1\2 ***"),
    (re.compile(r"(?i)\b(access_token|token|password)=[^&\s]+"), r"\1=***"),
)


def redact(text: str) -> str:
    """Remove credentials and tokens from text that may reach logs or persisted failure details."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RepositoryAcquisitionError(RuntimeError):
    """Base class for acquisition failures; ``failure_code`` is persisted on the attempt."""

    failure_code = "ACQUISITION_FAILED"

    def __init__(self, message: str, **detail: Any):
        super().__init__(redact(message))
        self.detail = detail

    @property
    def failure_detail(self) -> dict[str, Any]:
        return {"message": str(self), **self.detail}


class InvalidAcquisitionRequest(RepositoryAcquisitionError):
    failure_code = "INVALID_ACQUISITION_REQUEST"


class GitUnavailable(RepositoryAcquisitionError):
    failure_code = "GIT_UNAVAILABLE"


class GitCommandFailed(RepositoryAcquisitionError):
    failure_code = "GIT_COMMAND_FAILED"


class CommitMismatch(RepositoryAcquisitionError):
    failure_code = "COMMIT_SHA_MISMATCH"


class AcquisitionTimeout(RepositoryAcquisitionError):
    failure_code = "ACQUISITION_TIMEOUT"


class AcquisitionCancelled(RepositoryAcquisitionError):
    failure_code = "ACQUISITION_CANCELLED"


class RepositoryTooLarge(RepositoryAcquisitionError):
    failure_code = "REPOSITORY_TOO_LARGE"


class TooManyFiles(RepositoryAcquisitionError):
    failure_code = "REPOSITORY_TOO_MANY_FILES"


class UnsafeCheckout(RepositoryAcquisitionError):
    failure_code = "UNSAFE_CHECKOUT"


# --- contract --------------------------------------------------------------------------------


@dataclass(frozen=True)
class AcquisitionRequest:
    canonical_repository_key: str
    commit_sha: str
    analysis_id: UUID
    attempt_id: UUID


@dataclass(frozen=True)
class RepositorySnapshot:
    """A verified, read-only-by-convention source tree for one commit.

    ``root`` contains only regular files listed in ``files`` (POSIX relative
    paths); it has no ``.git`` directory, symlinks or submodule contents.
    """

    analysis_id: UUID
    attempt_id: UUID
    canonical_repository_key: str
    commit_sha: str
    root: Path
    files: tuple[str, ...]
    checkout_bytes: int
    duration_seconds: float
    excluded: Mapping[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def provenance(self) -> dict[str, Any]:
        """Durable acquisition evidence. Deliberately excludes the temporary path."""
        return {
            "canonical_repository_key": self.canonical_repository_key,
            "verified_commit_sha": self.commit_sha,
            "file_count": self.file_count,
            "checkout_bytes": self.checkout_bytes,
            "duration_ms": round(self.duration_seconds * 1000),
            "excluded": dict(self.excluded),
        }


class RepositoryAcquirer(Protocol):
    def acquire(
        self, request: AcquisitionRequest, *, cancel_event: threading.Event | None = None
    ) -> Any:
        """Return an async context manager yielding a ``RepositorySnapshot``.

        The snapshot directory is removed when the context exits, whatever the
        outcome. Setting ``cancel_event`` aborts an in-flight acquisition.
        """


# --- limits and exclusions -------------------------------------------------------------------


@dataclass(frozen=True)
class AcquisitionLimits:
    timeout_seconds: float = 120.0
    max_checkout_bytes: int = 256 * 1024 * 1024
    max_file_count: int = 50_000
    # Larger files are left out of the snapshot rather than failing the analysis.
    max_file_bytes: int = 2 * 1024 * 1024
    # Local growth budget for the whole attempt directory (pack + checkout), and the
    # ceiling for the provider-reported size. Looser than the checkout budget
    # because packs include excluded files.
    max_workdir_bytes: int = 512 * 1024 * 1024
    growth_check_seconds: float = 0.5


# Returns the provider-reported repository size in bytes, or None when unknown.
SizeProbe = Callable[[str], Awaitable[int | None]]


_VCS_DIRS = frozenset({".git", ".hg", ".svn", ".bzr"})
_GENERATED_DIRS = frozenset({
    "node_modules", "bower_components", "__pycache__", ".venv", "venv", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".gradle", ".terraform",
})
_SECRET_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk")
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map")
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".psd",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".ear",
    ".whl", ".egg", ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".o", ".obj", ".class",
    ".pyc", ".pyo", ".wasm", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3",
    ".mp4", ".wav", ".ogg", ".mov", ".avi", ".webm", ".flac", ".sqlite", ".db", ".parquet",
    ".npy", ".pkl", ".pt", ".onnx", ".h5",
})
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)
_WINDOWS_INVALID_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}


def exclusion_reason(path: str) -> str | None:
    """Return why a repository path is left out of the snapshot, or None to keep it."""
    parts = path.split("/")
    directories, name = parts[:-1], parts[-1]
    lowered = name.lower()
    if any(part in _VCS_DIRS for part in parts):
        return "vcs"
    if any(part in _GENERATED_DIRS for part in directories):
        return "generated"
    if (
        lowered in _SECRET_NAMES
        or lowered.endswith(_SECRET_SUFFIXES)
        or (lowered.startswith(".env.") and not lowered.endswith(_ENV_TEMPLATE_SUFFIXES))
    ):
        return "secret"
    if lowered.endswith(_GENERATED_SUFFIXES):
        return "generated"
    if os.path.splitext(lowered)[1] in _BINARY_SUFFIXES:
        return "binary"
    return None


def _unsafe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\x00" in path:
        return True
    for part in path.split("/"):
        if part in ("", ".", ".."):
            return True
        if os.name == "nt" and (
            _WINDOWS_RESERVED.match(part)
            or part.endswith((" ", "."))
            or any(ch in _WINDOWS_INVALID_CHARS for ch in part)
        ):
            return True
    return False


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_type: str
    object_id: str
    size: int | None
    path: str


def _parse_ls_tree(output: bytes) -> list[_TreeEntry]:
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, object_type, object_id, size = meta.decode("ascii").split()
        entries.append(_TreeEntry(
            mode=mode,
            object_type=object_type,
            object_id=object_id,
            size=None if size == "-" else int(size),
            path=raw_path.decode("utf-8", errors="surrogateescape"),
        ))
    return entries


# --- git execution ---------------------------------------------------------------------------


def github_remote_url(canonical_repository_key: str) -> str:
    """Rebuild the fetch URL from the canonical key instead of trusting stored clone URLs."""
    try:
        key = canonicalize_repository_url(canonical_repository_key)
    except RepositoryResolutionError as exc:
        raise InvalidAcquisitionRequest(f"invalid canonical repository key: {exc}") from exc
    return f"https://github.com/{key.removeprefix('github:')}.git"


def _terminate(process: subprocess.Popen) -> None:
    """Kill git and the helpers it spawned (remote-https, index-pack)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(Exception):
        process.kill()
    process.wait()


def _directory_bytes(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            with suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


class _Attempt:
    """Per-attempt state: directories, deadline and cancellation."""

    def __init__(self, request: AcquisitionRequest, workdir: Path, deadline: float,
                 cancel_event: threading.Event, limits: AcquisitionLimits):
        self.request = request
        self.workdir = workdir
        self.git_dir = workdir / "git"
        self.source = workdir / "source"
        self.home = workdir / "home"
        self.deadline = deadline
        self.cancel_event = cancel_event
        self.limits = limits

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise AcquisitionCancelled("acquisition was cancelled")
        if time.monotonic() >= self.deadline:
            raise AcquisitionTimeout(
                f"acquisition exceeded {self.limits.timeout_seconds:g}s",
                timeout_seconds=self.limits.timeout_seconds,
            )


class GitRepositoryAcquirer:
    """Git-backed ``RepositoryAcquirer``. Never executes repository content."""

    def __init__(
        self,
        limits: AcquisitionLimits | None = None,
        *,
        workdir_root: str | os.PathLike | None = None,
        git_command: Sequence[str] = ("git",),
        remote_url_for: Callable[[str], str] = github_remote_url,
        allowed_protocols: Sequence[str] = ("https",),
        size_probe: SizeProbe | None = None,
    ):
        self.limits = limits or AcquisitionLimits()
        self.workdir_root = Path(workdir_root) if workdir_root else Path(tempfile.gettempdir()) / _WORKDIR_ROOT_NAME
        self.git_command = tuple(git_command)
        self.remote_url_for = remote_url_for
        self.allowed_protocols = tuple(allowed_protocols)
        self.size_probe = size_probe

    @asynccontextmanager
    async def acquire(
        self, request: AcquisitionRequest, *, cancel_event: threading.Event | None = None
    ) -> AsyncIterator[RepositorySnapshot]:
        if not _SHA_PATTERN.fullmatch(request.commit_sha or ""):
            raise InvalidAcquisitionRequest("commit SHA must be 40 lowercase hex characters")
        remote_url = self.remote_url_for(request.canonical_repository_key)
        cancel_event = cancel_event or threading.Event()
        await self._check_reported_size(request)

        workdir = self._create_attempt_dir(request)
        attempt = _Attempt(request, workdir, time.monotonic() + self.limits.timeout_seconds,
                           cancel_event, self.limits)
        started = time.monotonic()
        log_context = (request.analysis_id, request.attempt_id, request.canonical_repository_key)
        logger.info("Acquiring analysis %s attempt %s: %s@%s", *log_context, request.commit_sha)
        try:
            # The thread keeps running after a task cancellation, so signal it and wait
            # for it to stop before the directory it writes into is removed.
            work = asyncio.ensure_future(asyncio.to_thread(self._materialize, attempt, remote_url, started))
            try:
                snapshot = await asyncio.shield(work)
            except asyncio.CancelledError:
                cancel_event.set()
                with suppress(BaseException):
                    await work
                raise
            logger.info(
                "Acquired analysis %s attempt %s: sha=%s files=%d bytes=%d duration_ms=%d excluded=%s",
                request.analysis_id, request.attempt_id, snapshot.commit_sha, snapshot.file_count,
                snapshot.checkout_bytes, round(snapshot.duration_seconds * 1000), dict(snapshot.excluded),
            )
            yield snapshot
        except RepositoryAcquisitionError as exc:
            logger.warning(
                "Acquisition failed for analysis %s attempt %s (%s): %s: %s",
                *log_context, exc.failure_code, exc,
            )
            raise
        finally:
            await asyncio.to_thread(_remove_tree, workdir)

    # -- attempt directories --------------------------------------------------------------

    async def _check_reported_size(self, request: AcquisitionRequest) -> None:
        if self.size_probe is None:
            return
        try:
            reported = await self.size_probe(request.canonical_repository_key)
        except Exception as exc:
            # The later layers still enforce limits, so an unavailable probe is not fatal.
            logger.warning(
                "Size check unavailable for analysis %s attempt %s: %s",
                request.analysis_id, request.attempt_id, redact(str(exc)),
            )
            return
        if reported is not None and reported > self.limits.max_workdir_bytes:
            raise RepositoryTooLarge(
                f"repository is reported at {reported} bytes; the limit is {self.limits.max_workdir_bytes}",
                source="provider_metadata", reported_bytes=reported,
                max_workdir_bytes=self.limits.max_workdir_bytes,
            )

    def _create_attempt_dir(self, request: AcquisitionRequest) -> Path:
        self.workdir_root.mkdir(parents=True, exist_ok=True)
        workdir = self.workdir_root / f"{ATTEMPT_DIR_PREFIX}{request.attempt_id}"
        try:
            workdir.mkdir()
        except FileExistsError as exc:
            # Never adopt (or later delete) a directory another acquisition may own.
            raise RepositoryAcquisitionError(
                f"attempt {request.attempt_id} already has an acquisition directory"
            ) from exc
        try:
            (workdir / MARKER_NAME).write_text(json.dumps({
                "analysis_id": str(request.analysis_id),
                "attempt_id": str(request.attempt_id),
                "created_at": time.time(),
                "pid": os.getpid(),
            }), encoding="utf-8")
        except BaseException:
            _remove_tree(workdir)
            raise
        return workdir

    def remove_stale_acquisitions(self, ttl_seconds: float, *, now: float | None = None) -> list[Path]:
        """Remove StackSniffer-owned attempt directories older than ``ttl_seconds``.

        Only directories named with ``ATTEMPT_DIR_PREFIX`` that hold a valid
        ownership marker are considered; everything else is left alone. The TTL
        must exceed the longest possible attempt, since another live worker on
        this host may share the directory. Returns the removed paths.
        """
        if not self.workdir_root.is_dir():
            return []
        now = time.time() if now is None else now
        removed: list[Path] = []
        for entry in os.scandir(self.workdir_root):
            if not entry.name.startswith(ATTEMPT_DIR_PREFIX):
                continue
            if not entry.is_dir(follow_symlinks=False) or entry.is_junction():
                continue
            path = Path(entry.path)
            marker = _read_marker(path)
            if marker is None:
                logger.warning("Leaving %s alone: no valid acquisition marker", entry.name)
                continue
            age = now - marker["created_at"]
            if age < ttl_seconds:
                continue
            ok = _remove_tree(path)
            if ok:
                removed.append(path)
            logger.info(
                "Stale acquisition directory for analysis %s attempt %s (age %ds): %s",
                marker["analysis_id"], marker["attempt_id"], age, "removed" if ok else "removal failed",
            )
        return removed

    # -- stages ---------------------------------------------------------------------------

    def _materialize(self, attempt: _Attempt, remote_url: str, started: float) -> RepositorySnapshot:
        sha = attempt.request.commit_sha
        attempt.home.mkdir()
        (attempt.home / "hooks").mkdir()
        (attempt.home / "gitconfig").write_bytes(b"")
        attempt.source.mkdir()

        self._git(attempt, "init", "--quiet", "--bare", "--template=", str(attempt.git_dir), with_git_dir=False)
        self._git(
            attempt, "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules",
            "--no-write-fetch-head", remote_url, sha, watch_growth=True,
        )
        if self._git(attempt, "cat-file", "-t", sha).strip() != b"commit":
            raise GitCommandFailed(f"fetched object {sha} is not a commit")

        # Detached HEAD pinned to the resolved commit, then verified independently.
        self._git(attempt, "update-ref", "--no-deref", "HEAD", sha)
        head = self._git(attempt, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        if head != sha:
            raise CommitMismatch(
                f"checked-out HEAD {head} does not match resolved commit {sha}",
                expected_sha=sha, actual_sha=head,
            )

        entries = _parse_ls_tree(self._git(attempt, "ls-tree", "-r", "-l", "-z", "--full-tree", sha))
        selected, excluded = self._select(entries)
        excluded.update(self._write_blobs(attempt, selected))
        files, checkout_bytes = self._verify_tree(attempt, {entry.path for entry in selected}, excluded)
        return RepositorySnapshot(
            analysis_id=attempt.request.analysis_id,
            attempt_id=attempt.request.attempt_id,
            canonical_repository_key=attempt.request.canonical_repository_key,
            commit_sha=head,
            root=attempt.source,
            files=files,
            checkout_bytes=checkout_bytes,
            duration_seconds=time.monotonic() - started,
            excluded=dict(sorted(excluded.items())),
        )

    def _select(self, entries: list[_TreeEntry]) -> tuple[list[_TreeEntry], Counter]:
        limits = self.limits
        excluded: Counter = Counter()
        selected: list[_TreeEntry] = []
        for entry in entries:
            if entry.mode == "160000":
                excluded["submodule"] += 1
            elif entry.mode == "120000":
                excluded["symlink"] += 1
            elif entry.object_type != "blob" or entry.size is None:
                excluded["unsupported"] += 1
            elif _unsafe_path(entry.path):
                excluded["unsafe_path"] += 1
            elif reason := exclusion_reason(entry.path):
                excluded[reason] += 1
            elif entry.size > limits.max_file_bytes:
                excluded["too_large"] += 1
            else:
                selected.append(entry)

        if len(selected) > limits.max_file_count:
            raise TooManyFiles(
                f"repository has {len(selected)} eligible files; the limit is {limits.max_file_count}",
                file_count=len(selected), max_file_count=limits.max_file_count,
            )
        total = sum(entry.size or 0 for entry in selected)
        if total > limits.max_checkout_bytes:
            raise RepositoryTooLarge(
                f"repository needs {total} bytes; the limit is {limits.max_checkout_bytes}",
                checkout_bytes=total, max_checkout_bytes=limits.max_checkout_bytes,
            )
        return selected, excluded

    def _write_blobs(self, attempt: _Attempt, selected: list[_TreeEntry]) -> Counter:
        """Stream blob contents from the object store straight into the source tree."""
        excluded: Counter = Counter()
        if not selected:
            return excluded
        paths_by_object: dict[str, list[str]] = {}
        for entry in selected:
            paths_by_object.setdefault(entry.object_id, []).append(entry.path)
        request_file = attempt.workdir / "objects.txt"
        request_file.write_text("".join(f"{oid}\n" for oid in paths_by_object), encoding="ascii")

        def consume(stream) -> None:
            for _ in range(len(paths_by_object)):
                header = stream.readline().split()
                if len(header) != 3 or header[1] != b"blob":
                    raise GitCommandFailed(f"unexpected object in blob stream: {header!r}")
                object_id, size = header[0].decode(), int(header[2])
                content = stream.read(size)
                stream.read(1)  # trailing newline
                for path in paths_by_object[object_id]:
                    attempt.check()
                    excluded.update(self._write_file(attempt.source, path, content))

        self._git(attempt, "cat-file", "--batch", stdin_path=request_file, consume_stdout=consume)
        return excluded

    @staticmethod
    def _write_file(root: Path, path: str, content: bytes) -> Counter:
        if b"\0" in content[:_BINARY_SNIFF_BYTES]:
            return Counter(binary=1)
        target = root.joinpath(*path.split("/"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive create: a case-insensitive collision never overwrites an earlier file.
            with open(target, "xb") as handle:
                handle.write(content)
        except FileExistsError:
            return Counter(path_collision=1)
        except OSError:
            return Counter(unwritable_path=1)
        return Counter()

    def _verify_tree(self, attempt: _Attempt, expected: set[str], excluded: Counter) -> tuple[tuple[str, ...], int]:
        """Walk the written tree without following links and prove it holds only expected files."""
        root = os.path.realpath(attempt.source)
        files: list[str] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(attempt.source, followlinks=False):
            for name in list(dirnames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or os.path.isjunction(full):
                    raise UnsafeCheckout(f"snapshot contains a link at {name!r}")
            for name in filenames:
                full = os.path.join(dirpath, name)
                info = os.lstat(full)
                relative = Path(os.path.relpath(full, attempt.source)).as_posix()
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafeCheckout(f"snapshot contains a non-regular file at {relative!r}")
                if os.path.commonpath([root, os.path.realpath(full)]) != root or relative not in expected:
                    raise UnsafeCheckout(f"snapshot contains an unexpected file at {relative!r}")
                files.append(relative)
                total += info.st_size
        if total > self.limits.max_checkout_bytes:
            raise RepositoryTooLarge(
                f"snapshot is {total} bytes; the limit is {self.limits.max_checkout_bytes}",
                checkout_bytes=total, max_checkout_bytes=self.limits.max_checkout_bytes,
            )
        return tuple(sorted(files)), total

    # -- subprocess -----------------------------------------------------------------------

    def _environment(self, attempt: _Attempt) -> dict[str, str]:
        # Start from an empty environment so ambient GIT_* settings, credential
        # helpers and user config cannot influence (or execute during) the fetch.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT") if key in os.environ}
        env.update({
            "HOME": str(attempt.home),
            "XDG_CONFIG_HOME": str(attempt.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(attempt.home / "gitconfig"),
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_ALLOW_PROTOCOL": ":".join(self.allowed_protocols),
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
        })
        return env

    def _hardening(self, attempt: _Attempt) -> list[str]:
        settings = (
            f"core.hooksPath={attempt.home / 'hooks'}",
            "core.fsmonitor=false",
            "core.symlinks=false",
            "core.protectNTFS=true",
            "core.protectHFS=true",
            "core.askPass=",
            "credential.helper=",
            "credential.interactive=never",
            "submodule.recurse=false",
            "fetch.recurseSubmodules=false",
            "filter.lfs.smudge=",
            "filter.lfs.process=",
            "filter.lfs.required=false",
            "gc.auto=0",
            "maintenance.auto=false",
        )
        return [arg for setting in settings for arg in ("-c", setting)]

    def _git(
        self,
        attempt: _Attempt,
        *args: str,
        with_git_dir: bool = True,
        watch_growth: bool = False,
        stdin_path: Path | None = None,
        consume_stdout: Callable[[Any], None] | None = None,
    ) -> bytes:
        attempt.check()
        command = [*self.git_command, *self._hardening(attempt)]
        if with_git_dir:
            command.append(f"--git-dir={attempt.git_dir}")
        command.extend(args)
        stderr_path = attempt.workdir / "git-stderr.txt"
        stdout_path = attempt.workdir / "git-stdout.bin"

        with open(stderr_path, "wb") as stderr, open(stdin_path or os.devnull, "rb") as stdin, \
                (open(os.devnull, "wb") if consume_stdout else open(stdout_path, "wb")) as stdout:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=stdin,
                    stdout=subprocess.PIPE if consume_stdout else stdout,
                    stderr=stderr,
                    cwd=attempt.workdir,
                    env=self._environment(attempt),
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                raise GitUnavailable(f"git could not be started: {exc}") from exc

            stop_reason: list[RepositoryAcquisitionError] = []
            watchdog = threading.Thread(
                target=self._watch, args=(attempt, process, watch_growth, stop_reason), daemon=True
            )
            watchdog.start()
            consume_error: BaseException | None = None
            try:
                if consume_stdout:
                    try:
                        consume_stdout(process.stdout)
                    except BaseException as exc:
                        consume_error = exc
                        _terminate(process)
                    finally:
                        process.stdout.close()
                returncode = process.wait()
            finally:
                _terminate(process)
                watchdog.join()

        if stop_reason:
            raise stop_reason[0]
        if consume_error is not None:
            raise consume_error
        if returncode != 0:
            message = stderr_path.read_bytes()[:_STDERR_LIMIT].decode("utf-8", errors="replace").strip()
            raise GitCommandFailed(
                f"git {args[0]} failed with exit code {returncode}: {message or 'no output'}",
                git_command=args[0], exit_code=returncode,
            )
        return b"" if consume_stdout else stdout_path.read_bytes()

    def _watch(self, attempt: _Attempt, process: subprocess.Popen, watch_growth: bool,
               stop_reason: list[RepositoryAcquisitionError]) -> None:
        """Kill git on cancellation, deadline or local growth past budget; exits when git exits."""
        while process.poll() is None:
            try:
                attempt.check()
                if watch_growth:
                    grown = _directory_bytes(attempt.workdir)
                    if grown > self.limits.max_workdir_bytes:
                        raise RepositoryTooLarge(
                            f"acquisition directory grew past {self.limits.max_workdir_bytes} bytes",
                            source="workdir_growth", workdir_bytes=grown,
                            max_workdir_bytes=self.limits.max_workdir_bytes,
                        )
            except RepositoryAcquisitionError as exc:
                stop_reason.append(exc)
                _terminate(process)
                return
            attempt.cancel_event.wait(self.limits.growth_check_seconds if watch_growth else 0.25)


def _read_marker(path: Path) -> dict[str, Any] | None:
    """Return the ownership marker if it is a regular file naming this directory's attempt."""
    marker = path / MARKER_NAME
    try:
        if not stat.S_ISREG(os.lstat(marker).st_mode):
            return None
        data = json.loads(marker.read_text(encoding="utf-8"))
        attempt_id = str(UUID(data["attempt_id"]))
        analysis_id = str(UUID(data["analysis_id"]))
        created_at = float(data["created_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if path.name != f"{ATTEMPT_DIR_PREFIX}{attempt_id}":
        return None
    return {"analysis_id": analysis_id, "attempt_id": attempt_id, "created_at": created_at}


def _remove_tree(path: Path) -> bool:
    """Remove a directory tree; an already-removed directory counts as success."""
    def make_writable(function, target, _exc):
        # Git writes pack files read-only; Windows refuses to delete them otherwise.
        os.chmod(target, stat.S_IWRITE)
        function(target)

    try:
        shutil.rmtree(path, onexc=make_writable)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Could not remove acquisition directory %s", path.name)
        return False
    return True
