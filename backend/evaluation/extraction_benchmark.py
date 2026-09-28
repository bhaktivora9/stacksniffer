"""Performance characterization of structural extraction on a real repository.

Runs the production path end to end — resolve a pinned reference, acquire the snapshot
(shallow fetch + materialize), discover files, extract, persist to PostgreSQL — and
records per-phase timings, peak memory, throughput, batch statistics and outcome counts.

Writes only to a disposable database: TEST_DATABASE_URL, checked with the same guard as
the test suite (local, or explicitly allow-listed, and never a production URL).

    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/stacksniffer \\
      python -m backend.evaluation.extraction_benchmark https://github.com/pandas-dev/pandas v2.2.2 \\
        --json backend/evaluation/results/extraction-performance-pandas.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.services.analysis_persistence import create_or_reuse_analysis, run_in_transaction
from backend.services.extraction.contracts import CancellationToken, ExtractionContext
from backend.services.extraction.extractor import RepositoryExtractor
from backend.services.extraction.origin import classify_origin
from backend.services.extraction.registry import AnalyzerRegistry
from backend.services.extraction.store import PostgresExtractionStore, build_batch_payload, write_batch
from backend.services.postgres import make_connection_factory
from backend.services.repository_acquisition import AcquisitionRequest, GitRepositoryAcquirer
from backend.services.repository_resolver import resolve_repository_reference
from backend.tests.database_safety import production_database_urls, unsafe_reason

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def peak_memory_bytes() -> int | None:
    """Peak resident memory of this process (the OS high-water mark, all allocations)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize)
        return None
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


@dataclass
class BatchTiming:
    files: int
    rows: dict[str, int]
    payload_seconds: float
    write_seconds: float
    statement_seconds: dict[str, float] = field(default_factory=dict)


class _TimedConnection:
    """Times each write statement by the table it writes (identified from the SQL)."""

    def __init__(self, conn, seconds: dict[str, float]):
        self._conn, self._seconds = conn, seconds

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def fetch_all_named(self, query, params):
        from backend.services.extraction.store import STATEMENTS

        table = next((name for name, sql in STATEMENTS.items() if sql == query), "other")
        started = time.perf_counter()
        try:
            return self._conn.fetch_all_named(query, params)
        finally:
            self._seconds[table] = self._seconds.get(table, 0.0) + time.perf_counter() - started


@dataclass
class TimedStore(PostgresExtractionStore):
    """The production store, instrumented: payload building and database writes timed separately."""

    timings: list[BatchTiming] = field(default_factory=list)

    def __init__(self, connection_factory, analysis_id, attempt_id):
        PostgresExtractionStore.__init__(self, connection_factory, analysis_id, attempt_id)
        self.timings = []

    def persist(self, files):
        self.batches += 1
        started = time.perf_counter()
        payload = build_batch_payload(files)
        built = time.perf_counter()
        seconds: dict[str, float] = {}
        conn = _TimedConnection(self._connection_factory(), seconds)
        try:
            write_batch(conn, self.analysis_id, payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.timings.append(BatchTiming(len(files), {k: len(v) for k, v in payload.items()},
                                        built - started, time.perf_counter() - built, seconds))


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {"min": round(ordered[0], 4), "median": round(statistics.median(ordered), 4),
            "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 4), "max": round(ordered[-1], 4),
            "total": round(sum(ordered), 4)}


async def run(url: str, reference: str, *, database_url: str, batch_size: int, workdir: Path | None,
              keep: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    resolved = await resolve_repository_reference(url, reference)
    resolve_seconds = time.perf_counter() - started

    factory = make_connection_factory(database_url)
    creation = run_in_transaction(factory, lambda conn: create_or_reuse_analysis(
        conn, canonical_repository_key=resolved.canonical_repository_key, owner_name=resolved.owner,
        repository_name=resolved.repository_name, clone_url=resolved.clone_url,
        requested_reference=resolved.requested_reference, resolved_commit_sha=resolved.commit_sha,
        structural_pipeline_version=f"benchmark-{uuid4().hex[:8]}",
    ))
    attempt_id = uuid4()
    acquirer = GitRepositoryAcquirer(workdir_root=workdir)
    request = AcquisitionRequest(resolved.canonical_repository_key, resolved.commit_sha, creation.analysis_id, attempt_id)

    t0 = time.perf_counter()
    async with acquirer.acquire(request) as snapshot:
        acquisition_seconds = time.perf_counter() - t0

        registry = AnalyzerRegistry.default()
        t1 = time.perf_counter()
        languages: dict[str, int] = {}
        for path in snapshot.files:  # discovery: language selection and origin for every file
            selection = registry.select(path)
            languages[selection.language or "unknown"] = languages.get(selection.language or "unknown", 0) + 1
            classify_origin(path, b"")
        discovery_seconds = time.perf_counter() - t1

        store = TimedStore(factory, creation.analysis_id, attempt_id)
        context = ExtractionContext(creation.analysis_id, attempt_id, snapshot.root, snapshot.commit_sha,
                                    CancellationToken(), snapshot.files)
        t2 = time.perf_counter()
        result = RepositoryExtractor(registry, batch_size=batch_size).extract(context, store)
        extraction_seconds = time.perf_counter() - t2
        snapshot_facts = {"files": snapshot.file_count, "checkout_bytes": snapshot.checkout_bytes,
                          "excluded": dict(snapshot.excluded)}
    total_seconds = time.perf_counter() - started

    conn = factory()
    try:
        stored = {table: conn.fetch_scalar(f"SELECT count(*) FROM core.{table} WHERE analysis_id = %s",
                                           str(creation.analysis_id))
                  for table in ("source_file", "entity", "evidence", "repository_edge")}
        if not keep:
            # Leave nothing behind: a lingering QUEUED analysis would sit ahead of real work in the queue.
            conn.execute("DELETE FROM core.analysis WHERE id = %s", str(creation.analysis_id))
            conn.commit()
    finally:
        conn.close()

    payload_seconds = sum(t.payload_seconds for t in store.timings)
    write_seconds = sum(t.write_seconds for t in store.timings)
    parse_seconds = extraction_seconds - payload_seconds - write_seconds
    persistence_seconds = payload_seconds + write_seconds
    return {
        "repository": resolved.canonical_repository_key,
        "reference": resolved.requested_reference,
        "commit_sha": resolved.commit_sha,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "extractor_versions": result.extractor_versions, "batch_size_files": batch_size,
                        "database": "local PostgreSQL 16 + pgvector (Docker)"},
        "snapshot": snapshot_facts,
        "seconds": {
            "resolve": round(resolve_seconds, 3),
            "acquisition": round(acquisition_seconds, 3),
            "discovery": round(discovery_seconds, 3),
            "parse": round(parse_seconds, 3),
            "persistence": round(persistence_seconds, 3),
            "persistence_payload": round(payload_seconds, 3),
            "persistence_database": round(write_seconds, 3),
            "extraction_total": round(extraction_seconds, 3),
            "total": round(total_seconds, 3),
        },
        "peak_memory_mb": round((peak_memory_bytes() or 0) / 2**20, 1),
        "throughput": {
            "files_per_second": round(result.files_total / extraction_seconds, 1),
            "entities_per_second": round(stored["entity"] / extraction_seconds, 1),
            "edges_per_second": round(stored["repository_edge"] / extraction_seconds, 1),
        },
        "counts": {
            "files": result.files_total,
            "files_by_language": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
            "files_by_status": dict(result.files_by_status),
            "files_by_origin": dict(result.files_by_origin),
            "entities_by_origin": dict(result.entities_by_origin),
            "stored": stored,
            "errors": result.errors,
        },
        "batches": {
            "count": len(store.timings),
            "files_per_batch": _distribution([float(t.files) for t in store.timings]),
            "rows_per_batch": {table: _distribution([float(t.rows[table]) for t in store.timings])
                               for table in ("entities", "edges", "evidence", "edge_links")},
            "database_seconds_per_batch": _distribution([t.write_seconds for t in store.timings]),
            "database_seconds_by_statement": {
                table: _distribution([t.statement_seconds.get(table, 0.0) for t in store.timings])
                for table in ("source_files", "evidence", "entities", "parents", "entity_links", "edges", "edge_links")
            },
            "slowest_batch": max(
                ({"files": t.files, "database_seconds": round(t.write_seconds, 3), "rows": t.rows,
                  "statement_seconds": {k: round(v, 3) for k, v in t.statement_seconds.items()}}
                 for t in store.timings), key=lambda b: b["database_seconds"], default=None),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url")
    parser.add_argument("reference")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the benchmark analysis in the database")
    args = parser.parse_args(argv)

    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        parser.error("TEST_DATABASE_URL must point at a disposable database")
    problem = unsafe_reason(database_url, production_database_urls(REPOSITORY_ROOT, dict(os.environ)),
                            os.environ.get("TEST_DATABASE_ALLOWED_HOSTS", "").split(","))
    if problem:
        parser.error(f"refusing to benchmark: {problem}")

    result = asyncio.run(run(args.url, args.reference, database_url=database_url,
                             batch_size=args.batch_size, workdir=args.workdir, keep=args.keep))
    text = json.dumps(result, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
