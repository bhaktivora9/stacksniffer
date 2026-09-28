"""Shared helpers for the SS-BE-202 accuracy and contract tests (not collected as tests)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from backend.evaluation import extraction_metrics as metrics
from backend.services.extraction.analyzers import GoAnalyzer, JavaAnalyzer, PythonAnalyzer, ScalaAnalyzer

requires_grammars = pytest.mark.skipif(
    not all(analyzer().available for analyzer in (PythonAnalyzer, JavaAnalyzer, ScalaAnalyzer, GoAnalyzer)),
    reason="tree-sitter grammars are not installed",
)

LANGUAGES = ("python", "java", "scala", "go")
REGENERATE = ("python -m backend.evaluation.extraction_metrics "
              "--json backend/evaluation/results/structural-extraction-baseline.json "
              "--markdown docs/evaluation/structural-extraction-baseline.md")


@lru_cache(maxsize=1)
def benchmark() -> dict:
    return metrics.evaluate()


def language_result(language: str) -> dict:
    return benchmark()["languages"][language]


def gold_cases(language: str) -> list:
    return [case for case in metrics.load_gold() if case.language == language]


def fixture_root(case) -> Path:
    return metrics.FIXTURES_DIR / case.fixture


def committed_results() -> dict:
    return json.loads(metrics.RESULTS_PATH.read_text(encoding="utf-8"))


def gate_report(language: str) -> str:
    data = language_result(language)
    lines = [f"{g['gate']}: {g['actual']} (required {g['required']})" for g in data["gates"]]
    return "\n".join(lines)
