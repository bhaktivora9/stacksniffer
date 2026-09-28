# Structural extraction performance (SS-BE-201)

A performance characterization, not a target. Measured with
`backend/evaluation/extraction_benchmark.py`, which runs the production path end to end:
resolve a pinned reference, acquire the snapshot (shallow fetch and materialize), discover
files, extract, and persist to PostgreSQL. It writes only to a disposable database
(`TEST_DATABASE_URL`, same guard as the test suite) and deletes its analysis afterwards.
Raw results: `backend/evaluation/results/extraction-performance-pandas.json`.

## Subject and environment

| | |
|---|---|
| Repository | `pandas-dev/pandas` at `v2.2.2` (`d9cdd2ee5a58015ef6f4d15c7226110c9aab8140`) |
| Snapshot | 2,205 files, 32.2 MB after acquisition exclusions (363 binary, 2 over 2 MB) |
| Languages | 1,552 Python; 653 others (HTML, YAML, C, Markdown, …) get file-level analysis |
| Extractor | `stacksniffer-python/2+tree-sitter-python@0.25.0`, batches of 100 files |
| Machine | Windows 11 laptop, Python 3.13.7, PostgreSQL 16 + pgvector in local Docker |

## Results

| Phase | Seconds |
|---|---|
| Resolve reference (GitHub API) | 0.9 |
| Acquisition (shallow fetch + materialize) | 8.9 |
| Discovery (language + origin for every file) | 0.01 |
| Parse and normalize | 59.6 |
| Persistence (payload 3.0 + database 41.8) | 44.8 |
| **Total** | **114.8** |

| Measure | Value |
|---|---|
| Peak memory (process high-water mark) | 194 MB |
| Throughput | 21.1 files/s · 482 entities/s · 1,703 edges/s |
| Stored | 2,205 files · 50,298 entities · 177,811 edges · 274,312 evidence rows |
| Outcomes | 1,552 PARSED · 653 UNSUPPORTED · 0 PARTIAL · 0 FAILED · 0 errors |
| Origin | 2,191 first-party · 2 generated · 12 vendored files (pandas' `vendored/ujson`) |
| Batches | 23; median 100 files, 8,144 edges and 13,425 edge-evidence links per batch |
| Database time per batch | median 2.0 s, p95 3.3 s, max 4.2 s; no statement over 1.1 s |

## What the benchmark found and fixed

The first runs exposed a planner problem that small fixtures could not show. Row estimates
for an analysis whose rows were inserted moments earlier in the same transaction are
stale (the planner believes "one row"), so any join it may reorder ran quadratically:

| Run | Database time | Slowest batch | Cause |
|---|---|---|---|
| natural-key joins | 163 s | 49 s | nested loops over the whole analysis per input row |
| same, second run on a warm database | stalled > 10 min on one statement | — | same, worse with more rows |
| id-based writes, PK re-check as a join | 191 s | 75 s (one `edge_links` statement 71.5 s) | planner scanned by `analysis_id` in the outer loop |
| id-based writes, PK probes fenced with `OFFSET 0` | **42 s** | **4.2 s** | — |

The store now takes each row's id from the upsert's `RETURNING` and refers to it directly;
the only remaining lookups are primary-key probes that cannot be reordered. The
same-analysis guarantee is unchanged and still covered by the cross-analysis tests.

## Follow-ups

- **Deleting an analysis was quadratic (fixed in the canonical schema).**
  `entity_evidence.evidence_id` and `edge_evidence.evidence_id` are foreign keys whose
  primary keys lead with the other column, so every evidence row a cascade deleted scanned
  both link tables. Deleting one pandas-sized analysis ran for minutes, and re-analysis
  cleanup and repository deletion both cascade through this. `002_core_repository_ir.sql`
  now defines `entity_evidence_evidence_id_idx` and `edge_evidence_evidence_id_idx`.
  `test_deleting_an_analysis_finds_evidence_links_by_index` deletes an analysis with about
  1,600 evidence rows and requires index scans only. Without the indexes, the same delete
  ran 1,621 sequential scans of the link tables.

- **Parsing is now the larger cost** (60 s of 104 s extraction, ~38 ms per Python file). It
  runs on one thread; per-file extraction is independent, so it parallelizes if needed.
- **Single run, single machine.** Timings on shared cloud hardware and Neon will differ;
  re-run the benchmark there before sizing workers.
