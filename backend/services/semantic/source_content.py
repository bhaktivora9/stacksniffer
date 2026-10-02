"""Retain source bytes at extraction, so semantic indexing never needs the repository again.

While an attempt's snapshot exists (right after structural extraction), every extracted file is
classified once and recorded on ``core.source_file.content_status``:

- SECRET: never retained. A credential-like file name (``.env``, ``*.pem``, ``id_rsa``...) or a
  credential pattern in the content (private-key blocks, cloud and VCS access tokens).
- OVERSIZED: larger than ``MAX_RETAINED_BYTES``; not retained.
- BINARY: NUL bytes or not UTF-8; not retained.
- RETAINED: stored once in ``core.source_content`` keyed by the SHA-256 the extractor recorded.

Binary files the acquisition stage already excluded never reach ``core.source_file``; their
counts stay in the analysis's acquisition metadata.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

MAX_RETAINED_BYTES = 1_000_000
_RETENTION_BATCH_BYTES = 8_000_000

_SECRET_NAME = re.compile(
    r"(^|/)("
    r"\.env(\.[^/]*)?|[^/]*\.(pem|key|p12|pfx|jks|keystore|asc|gpg|ppk|tfstate|tfstate\.backup)"
    r"|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|\.netrc|\.npmrc|\.pypirc|\.htpasswd|\.git-credentials"
    r"|credentials(\.[a-z]+)?|secrets?\.(json|ya?ml|toml|env|properties)|service[-_]account[^/]*\.json"
    r")$",
    re.IGNORECASE,
)
# Templates that document variables without values are safe by name; their content is still scanned.
_SECRET_NAME_TEMPLATES = re.compile(r"\.env\.(example|sample|template|dist)$", re.IGNORECASE)
_SECRET_CONTENT = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
    rb"|\bAKIA[0-9A-Z]{16}\b"  # AWS access key id
    rb"|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"  # GitHub tokens
    rb"|\bxox[abposr]-[A-Za-z0-9-]{10,}\b"  # Slack tokens
    rb"|\bAIza[0-9A-Za-z_\-]{35}\b"  # Google API keys
    rb"|\bsk_live_[0-9A-Za-z]{20,}\b"  # Stripe live secret keys
)
CONTENT_STATUSES = ("RETAINED", "SECRET", "BINARY", "OVERSIZED")


def classify_source(path: str, data: bytes) -> str:
    """The file's content status: RETAINED, or the reason it is not retained."""
    if _SECRET_NAME.search(path) and not _SECRET_NAME_TEMPLATES.search(path):
        return "SECRET"
    if len(data) > MAX_RETAINED_BYTES:
        return "OVERSIZED"
    if b"\x00" in data:
        return "BINARY"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "BINARY"
    if _SECRET_CONTENT.search(data):
        return "SECRET"
    return "RETAINED"


class SourceContentMismatch(RuntimeError):
    """The snapshot's bytes are not the content the extraction recorded."""

    failure_code = "SOURCE_CONTENT_MISMATCH"


@dataclass(frozen=True)
class RetentionResult:
    files: int
    by_status: dict[str, int] = field(default_factory=dict)
    retained_bytes: int = 0


def retain_source_content(conn: Any, analysis_id: UUID, read_bytes: Callable[[str], bytes]) -> RetentionResult:
    """Classify and retain every extracted file of an analysis. Idempotent: content is stored once
    by hash, and re-running records the same statuses."""
    files = conn.fetch_all("SELECT id, path, content_hash FROM core.source_file WHERE analysis_id = %s ORDER BY path",
                           str(analysis_id))
    statuses: Counter = Counter()
    retained_bytes = 0
    batch: list[tuple[Any, str, str, bytes | None]] = []
    batch_bytes = 0
    for file_id, path, recorded_hash in files:
        try:
            data = read_bytes(path)
        except OSError:  # unreadable at extraction too (parse status FAILED); left unassessed
            statuses["UNREADABLE"] += 1
            continue
        if hashlib.sha256(data).hexdigest() != recorded_hash:
            raise SourceContentMismatch(f"{path}: the snapshot does not match the extracted content")
        status = classify_source(path, data)
        statuses[status] += 1
        content = data if status == "RETAINED" else None
        if content is not None:
            retained_bytes += len(content)
            batch_bytes += len(content)
        batch.append((file_id, status, recorded_hash, content))
        if batch_bytes >= _RETENTION_BATCH_BYTES:
            _write(conn, batch)
            batch, batch_bytes = [], 0
    _write(conn, batch)
    return RetentionResult(files=len(files), by_status=dict(sorted(statuses.items())), retained_bytes=retained_bytes)


def _write(conn: Any, batch: list[tuple[Any, str, str, bytes | None]]) -> None:
    if not batch:
        return
    retained = [(content_hash, content) for _, _, content_hash, content in batch if content is not None]
    if retained:
        hashes, contents = zip(*dict(retained).items())
        conn.execute(
            "INSERT INTO core.source_content (content_hash, content, byte_size) "
            "SELECT h, c, octet_length(c) FROM unnest(%s::text[], %s::bytea[]) AS t(h, c) "
            "ON CONFLICT (content_hash) DO NOTHING",
            list(hashes), list(contents))
    conn.execute(
        "UPDATE core.source_file AS f SET content_status = t.status, "
        "retained_content_hash = CASE WHEN t.status = 'RETAINED' THEN f.content_hash END "
        "FROM unnest(%s::uuid[], %s::text[]) AS t(id, status) WHERE f.id = t.id",
        [str(file_id) for file_id, _, _, _ in batch], [status for _, status, _, _ in batch])


def read_retained_source(conn: Any, analysis_id: UUID) -> dict[Any, bytes]:
    """Retained bytes of an analysis's files, by file id."""
    return {file_id: bytes(content) for file_id, content in conn.fetch_all(
        "SELECT f.id, c.content FROM core.source_file f JOIN core.source_content c "
        "ON c.content_hash = f.retained_content_hash WHERE f.analysis_id = %s", str(analysis_id))}
