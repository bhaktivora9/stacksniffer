"""Tree-sitter parser adapters and the shared machinery for analyzers built on them.

Grammars load lazily. When a grammar package is not installed the adapter
reports itself unavailable and the registry falls back to file-level analysis
instead of failing the pipeline.
"""

from __future__ import annotations

import importlib
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Iterator

from .builder import Span
from .contracts import CancellationToken, FileExtractionError

logger = logging.getLogger(__name__)

# Nodes visited between cancellation checks while walking a tree.
CANCELLATION_CHECK_INTERVAL = 500
MAX_REPORTED_SYNTAX_ERRORS = 20


class GrammarUnavailable(RuntimeError):
    """The grammar package for a language is not installed."""


def span_of(node: Any) -> Span:
    """Convert a node's location to plain integers (one-based, inclusive lines)."""
    start_row, _ = node.start_point
    end_row, end_column = node.end_point
    # A node ending at column 0 stops before that line begins.
    if end_column == 0 and end_row > start_row:
        end_row -= 1
    return Span(node.start_byte, node.end_byte, start_row + 1, end_row + 1)


def text_of(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text is not None else ""


class TreeSitterParserAdapter:
    """``ParserAdapter`` over a ``tree-sitter-<language>`` grammar package."""

    def __init__(self, language: str, module: str, package: str):
        self.language = language
        self._module = module
        self._package = package
        self._parser = None
        self._load_error: str | None = None

    def _load(self):
        if self._parser is None and self._load_error is None:
            try:
                from tree_sitter import Language, Parser

                grammar = importlib.import_module(self._module)
                self._parser = Parser(Language(grammar.language()))
            except Exception as exc:  # ImportError, or an ABI mismatch between binding and grammar
                self._load_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Tree-sitter grammar for %s is unavailable: %s", self.language, self._load_error)
        return self._parser

    @property
    def available(self) -> bool:
        return self._load() is not None

    @property
    def unavailable_reason(self) -> str | None:
        self._load()
        return self._load_error

    @property
    def grammar_version(self) -> str:
        try:
            return f"{self._package}@{version(self._package)}"
        except PackageNotFoundError:
            return f"{self._package}@unknown"

    def parse(self, source: bytes) -> Any:
        parser = self._load()
        if parser is None:
            raise GrammarUnavailable(f"{self.language} grammar is unavailable: {self._load_error}")
        return parser.parse(source)


def syntax_errors(root: Any, path: str, token: CancellationToken) -> list[FileExtractionError]:
    """Report ERROR and MISSING nodes; the rest of the tree is still usable."""
    if not root.has_error:
        return []
    errors: list[FileExtractionError] = []
    seen: set[tuple[str, int]] = set()
    for index, node in enumerate(walk(root)):
        if index % CANCELLATION_CHECK_INTERVAL == 0:
            token.raise_if_cancelled()
        if node.type == "ERROR" or node.is_missing:
            line = span_of(node).start_line
            code = "missing_syntax" if node.is_missing else "syntax_error"
            if (code, line) in seen:
                continue
            seen.add((code, line))
            message = f"missing {node.type!r}" if node.is_missing else "source could not be parsed"
            errors.append(FileExtractionError(path, code, message, line))
            if len(errors) >= MAX_REPORTED_SYNTAX_ERRORS:
                break
    return errors or [FileExtractionError(path, "syntax_error", "tree contains errors", None)]


def walk(root: Any) -> Iterator[Any]:
    """Pre-order traversal without recursion, so deeply nested code cannot overflow the stack."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))
