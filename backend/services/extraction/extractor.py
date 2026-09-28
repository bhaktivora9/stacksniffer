"""Repository-level extraction: select an analyzer per file, isolate failures, persist in batches."""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from typing import Any, Protocol, Sequence

from .builder import FileFactsBuilder
from .contracts import (
    CanonicalContractError,
    Capability,
    ExtractionCancelled,
    ExtractionContext,
    ExtractionResult,
    FileExtraction,
    FileExtractionError,
    LanguageAnalyzer,
    ParseStatus,
)
from .origin import Origin, classify_origin
from .registry import AnalyzerRegistry, Selection

logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE = 500
# Analyzer crashes (exceptions or contract violations, not syntax errors) are systemic when
# every analyzable first-party file crashes, whatever the repository size, or when more than
# MAX_CRASH_RATE of them crash in a repository with at least CRASH_RATE_MIN_FILES of them.
# Generated and vendored files are reported but never count: their failures are not ours.
MAX_CRASH_RATE = 0.25
CRASH_RATE_MIN_FILES = 20


class ExtractionSystemicFailure(RuntimeError):
    """A repository-level fault, as opposed to a problem with individual files."""

    failure_code = "EXTRACTION_SYSTEMIC_FAILURE"


class ExtractionStore(Protocol):
    def persist(self, files: Sequence[FileExtraction]) -> None:
        """Write one batch atomically."""


class RepositoryExtractor:
    def __init__(self, registry: AnalyzerRegistry, *, batch_size: int = 100,
                 max_crash_rate: float = MAX_CRASH_RATE, crash_rate_min_files: int = CRASH_RATE_MIN_FILES):
        self.registry = registry
        self.batch_size = batch_size
        self.max_crash_rate = max_crash_rate
        self.crash_rate_min_files = crash_rate_min_files

    def extract(self, context: ExtractionContext, store: ExtractionStore) -> ExtractionResult:
        """Run synchronously (in a worker thread); raises ``ExtractionCancelled`` once the token is set.

        Batches committed before cancellation stay committed; nothing is written after it.
        """
        started = time.monotonic()
        token = context.cancellation_token
        if not context.checkout_path.is_dir():
            raise ExtractionSystemicFailure("the repository snapshot is not available")

        tally = _Tally()
        batch: list[FileExtraction] = []
        for path in sorted(context.files):
            token.raise_if_cancelled()
            batch.append(tally.add(self._extract_file(context, path)))
            if len(batch) >= self.batch_size:
                self._flush(batch, store, context)
                batch = []
        self._flush(batch, store, context)

        self._check_crash_rate(tally)
        return tally.result(context, time.monotonic() - started)

    def _check_crash_rate(self, tally: _Tally) -> None:
        analyzed, crashed = tally.analyzed, tally.analyzer_failures
        if not analyzed:
            return
        detail = (f"{crashed} of {analyzed} analyzable first-party files crashed "
                  f"(first: {', '.join(tally.crashed_paths[:5])})")
        if crashed == analyzed:
            raise ExtractionSystemicFailure(f"extraction failed for every file: {detail}")
        if analyzed >= self.crash_rate_min_files and crashed / analyzed > self.max_crash_rate:
            raise ExtractionSystemicFailure(
                f"crash rate {crashed / analyzed:.0%} exceeds {self.max_crash_rate:.0%}: {detail}")

    @staticmethod
    def _flush(batch: list[FileExtraction], store: ExtractionStore, context: ExtractionContext) -> None:
        if not batch:
            return
        context.cancellation_token.raise_if_cancelled()
        store.persist(batch)

    def _extract_file(self, context: ExtractionContext, path: str) -> FileExtraction:
        selection = self.registry.select(path)
        try:
            source = context.checkout_path.joinpath(*path.split("/")).read_bytes()
        except OSError as exc:
            return self._file_level(context, path, selection, b"", ParseStatus.FAILED,
                                    [FileExtractionError(path, "unreadable", type(exc).__name__)],
                                    origin=classify_origin(path, b""))

        origin = classify_origin(path, source)
        analyzer = selection.analyzer
        if analyzer is self.registry.fallback:
            return self._file_level(context, path, selection, source, ParseStatus.UNSUPPORTED, [], origin=origin)

        builder = self._builder(context, path, selection.language, source, analyzer)
        try:
            errors = analyzer.analyze(source, builder, context.cancellation_token)
            return builder.build(ParseStatus.PARTIAL if errors else ParseStatus.PARSED, errors, origin=origin)
        except ExtractionCancelled:
            raise
        except Exception as exc:
            # Discard whatever the analyzer produced: a half-built file is not evidence.
            code = "contract_violation" if isinstance(exc, CanonicalContractError) else "analyzer_error"
            logger.warning("Extraction of %s failed (%s): %s: %s", path, code, type(exc).__name__, exc)
            message = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_MESSAGE]
            return self._file_level(context, path, selection, source, ParseStatus.FAILED,
                                    [FileExtractionError(path, code, message)], analyzer_failed=True, origin=origin)

    def _file_level(self, context, path, selection: Selection, source, status, errors, *, analyzer_failed=False,
                    origin: Origin | None = None):
        builder = self._builder(context, path, selection.language, source, self.registry.fallback)
        metadata = {"fallback_reason": selection.fallback_reason} if selection.fallback_reason else {}
        if analyzer_failed:
            metadata["attempted_analyzer"] = selection.analyzer.extractor
        return builder.build(status, errors, metadata=metadata, origin=origin)

    @staticmethod
    def _builder(context, path, language, source, analyzer: LanguageAnalyzer) -> FileFactsBuilder:
        return FileFactsBuilder(
            path=path, language=language, source=source, commit_sha=context.commit_sha,
            analyzer=analyzer.extractor, extractor=analyzer.extractor,
            extractor_version=analyzer.extractor_version, capabilities=analyzer.capabilities,
        )


class _Tally:
    def __init__(self) -> None:
        self.files = 0
        self.by_status: Counter = Counter()
        self.entities = self.relationships = self.evidence = self.errors = 0
        self.analyzed = self.analyzer_failures = 0
        self.crashed_paths: list[str] = []
        self.files_by_origin: Counter = Counter()
        self.entities_by_origin: Counter = Counter()
        self.languages: dict[str, dict[str, Any]] = defaultdict(lambda: {"files": 0, "by_status": Counter()})
        self.coverage: dict[str, Counter] = {capability.value: Counter() for capability in Capability}
        self.versions: dict[str, str] = {}

    def add(self, result: FileExtraction) -> FileExtraction:
        self.files += 1
        self.by_status[result.parse_status.value] += 1
        self.entities += len(result.entities)
        self.relationships += len(result.relationships)
        self.evidence += len(result.evidence)
        self.errors += len(result.errors)
        origin = "vendored" if result.is_vendored else "generated" if result.is_generated else "first_party"
        self.files_by_origin[origin] += 1
        self.entities_by_origin[origin] += len(result.entities)
        analyzable = result.parse_status is not ParseStatus.UNSUPPORTED and "fallback_reason" not in result.metadata
        if analyzable and result.first_party:
            self.analyzed += 1
            if "attempted_analyzer" in result.metadata:
                self.analyzer_failures += 1
                self.crashed_paths.append(result.path)

        language = self.languages[result.language or "unknown"]
        language["files"] += 1
        language["by_status"][result.parse_status.value] += 1
        if result.parse_status in (ParseStatus.PARSED, ParseStatus.PARTIAL):
            language["analyzer"] = result.analyzer
            language["capabilities"] = result.capabilities.as_dict()
            self.versions[result.language or "unknown"] = result.extractor_version
        elif "fallback_reason" in result.metadata:
            language["fallback_reason"] = result.metadata["fallback_reason"]
        for capability, level in result.capabilities.levels.items():
            self.coverage[capability.value][level.value] += 1
        return result

    def result(self, context: ExtractionContext, duration: float) -> ExtractionResult:
        languages = {}
        for name, stats in sorted(self.languages.items()):
            languages[name] = {**stats, "by_status": dict(stats["by_status"]),
                               "capabilities": stats.get("capabilities", {c.value: "UNSUPPORTED" for c in Capability})}
        return ExtractionResult(
            analysis_id=context.analysis_id,
            attempt_id=context.attempt_id,
            commit_sha=context.commit_sha,
            files_total=self.files,
            files_by_status=dict(self.by_status),
            entities=self.entities,
            relationships=self.relationships,
            evidence=self.evidence,
            errors=self.errors,
            languages=languages,
            capability_coverage={c: dict(levels) for c, levels in self.coverage.items()},
            extractor_versions=dict(sorted(self.versions.items())),
            duration_seconds=duration,
            files_by_origin=dict(sorted(self.files_by_origin.items())),
            entities_by_origin=dict(sorted(self.entities_by_origin.items())),
        )
