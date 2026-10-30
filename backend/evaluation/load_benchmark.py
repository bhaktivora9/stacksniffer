"""Check, load and freeze a benchmark question file.

    python -m backend.evaluation.load_benchmark backend/evaluation/benchmark/r1-retrieval-test.draft.yaml
    python -m backend.evaluation.load_benchmark FILE --load [--replace-draft] [--freeze]

Without --load every check runs in a transaction that is rolled back, so nothing is written.
--load creates the dataset as a DRAFT; --replace-draft first deletes an existing DRAFT of the
same key and version (a FROZEN dataset can never be replaced). --freeze then freezes it, which
requires every judgment to be HUMAN-reviewed. The database is DATABASE_URL; backend/.env is
not read, so the target is always explicit.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlsplit

from backend.services.benchmark_manifest import ManifestValidationError, load_manifest
from backend.services.postgres import make_connection_factory
from backend.services.semantic.benchmark_file import (
    BenchmarkFileError,
    freeze_reviewed,
    load_benchmark,
    parse_benchmark_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path")
    parser.add_argument("--manifest", default="repositories.yaml")
    parser.add_argument("--load", action="store_true", help="write the dataset as a DRAFT")
    parser.add_argument("--replace-draft", action="store_true", help="delete an existing DRAFT of this version first")
    parser.add_argument("--freeze", action="store_true", help="freeze after loading (requires human review)")
    args = parser.parse_args(argv)
    if (args.freeze or args.replace_draft) and not args.load:
        parser.error("--freeze and --replace-draft require --load")

    try:
        spec = parse_benchmark_file(args.path)
        manifest = load_manifest(args.manifest)
    except (BenchmarkFileError, ManifestValidationError, OSError) as exc:
        print(f"invalid input:\n{exc}", file=sys.stderr)
        return 2
    questions = sum(len(r.questions) for r in spec.repositories)
    judgments = sum(len(q.judgments) for r in spec.repositories for q in r.questions)
    print(f"{args.path}: {spec.dataset_key} v{spec.dataset_version} ({spec.status}), {questions} questions, "
          f"{judgments} judgments, {spec.unreviewed()} not yet HUMAN-reviewed")

    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    target = urlsplit(url)
    print(f"database: {target.hostname}:{target.port or 5432}{target.path}")

    conn = make_connection_factory(url)()
    try:
        if args.replace_draft:
            deleted = conn.execute("DELETE FROM evaluation.dataset WHERE dataset_key = %s AND dataset_version = %s "
                                   "AND status = 'DRAFT'", spec.dataset_key, spec.dataset_version)
            if deleted:
                print("replaced the existing DRAFT")
        dataset_id = load_benchmark(conn, spec, manifest)
        if not args.load:
            conn.rollback()
            print("all checks passed (nothing written; use --load to write the DRAFT)")
            return 0
        message = f"loaded DRAFT dataset {dataset_id}"
        if args.freeze:
            message = f"loaded and froze dataset {dataset_id}: sha256:{freeze_reviewed(conn, spec, dataset_id)}"
        conn.commit()
        print(message)
        return 0
    except BenchmarkFileError as exc:
        conn.rollback()
        print(f"{len(exc.problems)} problem(s):\n" + "\n".join(f"  {p}" for p in exc.problems), file=sys.stderr)
        return 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
