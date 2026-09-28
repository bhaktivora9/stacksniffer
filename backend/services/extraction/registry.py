"""Language detection and analyzer selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .analyzers import (
    FileLevelAnalyzer,
    GoAnalyzer,
    JavaAnalyzer,
    JavaScriptAnalyzer,
    PythonAnalyzer,
    ScalaAnalyzer,
)
from .contracts import LanguageAnalyzer

_EXTENSIONS = {
    ".py": "python", ".pyi": "python",
    ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".rb": "ruby", ".rs": "rust", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".cs": "csharp", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".hh": "cpp", ".php": "php", ".swift": "swift", ".groovy": "groovy", ".gradle": "groovy",
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
    ".vue": "vue", ".svelte": "svelte", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".toml": "toml", ".xml": "xml", ".md": "markdown", ".proto": "protobuf", ".tf": "terraform",
}
_FILENAMES = {"dockerfile": "dockerfile", "makefile": "make", "jenkinsfile": "groovy", "gemfile": "ruby"}


def detect_language(path: str) -> str | None:
    """Language from the file name and extension; None when unknown. Content is never executed."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in _FILENAMES:
        return _FILENAMES[name]
    if name.startswith("dockerfile."):
        return "dockerfile"
    dot = name.rfind(".")
    return _EXTENSIONS.get(name[dot:]) if dot > 0 else None


@dataclass(frozen=True)
class Selection:
    language: str | None
    analyzer: LanguageAnalyzer
    # Why a known language fell back to file-level analysis, if it did.
    fallback_reason: str | None = None


class AnalyzerRegistry:
    def __init__(self, analyzers: Iterable[LanguageAnalyzer], fallback: LanguageAnalyzer | None = None):
        self._analyzers = {analyzer.language: analyzer for analyzer in analyzers}
        self.fallback = fallback or FileLevelAnalyzer()

    @classmethod
    def default(cls) -> AnalyzerRegistry:
        return cls([PythonAnalyzer(), JavaAnalyzer(), ScalaAnalyzer(), GoAnalyzer(), JavaScriptAnalyzer()])

    @property
    def analyzers(self) -> tuple[LanguageAnalyzer, ...]:
        return tuple(self._analyzers.values())

    def select(self, path: str) -> Selection:
        language = detect_language(path)
        analyzer = self._analyzers.get(language) if language else None
        if analyzer is None:
            return Selection(language, self.fallback, "no_analyzer" if language else "unknown_language")
        if not analyzer.available:
            return Selection(language, self.fallback, "grammar_unavailable")
        return Selection(language, analyzer)
