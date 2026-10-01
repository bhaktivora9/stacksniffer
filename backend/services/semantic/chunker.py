"""Symbol-aware chunking of one source file (pure: source bytes and entities in, chunks out).

`innermost_declaration`: every line belongs to the innermost included declaration covering it
(a method's lines belong to the method, not also to its class), and lines outside any included
declaration belong to the file when FILE is included. Each owner's contiguous runs of lines are
cut into windows of at most `max_tokens`, repeating `overlap_lines` lines between consecutive
windows of one run. No line is ever cut unless it alone exceeds the budget.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from dataclasses import dataclass
from typing import Iterable

from .profiles import ChunkProfile


@dataclass(frozen=True)
class ChunkEntity:
    stable_key: str
    entity_type: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Chunk:
    stable_chunk_key: str
    entity_key: str | None  # None for file-level code outside any included declaration
    content: str
    content_hash: str
    token_count: int
    ordinal: int
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


# tokenizer -> (token count of a text, UTF-8 bytes that always fit in a token budget)
TOKENIZERS = {
    "utf8_bytes_div_3_ceil": (lambda text: math.ceil(len(text.encode("utf-8")) / 3), lambda tokens: tokens * 3),
}


def _tokenizer(profile: ChunkProfile):
    try:
        return TOKENIZERS[profile.tokenizer]
    except KeyError:
        raise ValueError(f"unknown tokenizer {profile.tokenizer!r}") from None


def count_tokens(profile: ChunkProfile, text: str) -> int:
    return _tokenizer(profile)[0](text)


def _line_offsets(source: bytes) -> list[tuple[int, int]]:
    """(start, end) byte offsets of each line, end including its newline."""
    offsets, start = [], 0
    while start < len(source):
        newline = source.find(b"\n", start)
        end = len(source) if newline < 0 else newline + 1
        offsets.append((start, end))
        start = end
    return offsets


def build_chunks(path: str, source: bytes, entities: Iterable[ChunkEntity], profile: ChunkProfile) -> list[Chunk]:
    lines = _line_offsets(source)
    starts = [start for start, _ in lines]
    types = set(profile.included_entity_types)
    included = sorted((e for e in entities if e.entity_type in types and e.entity_type != "FILE"),
                      key=lambda e: (e.start_line, -e.end_line))
    # Innermost owner per line: later-starting (nested) declarations overwrite their parents.
    owner: list[str | None] = ["FILE" if "FILE" in types else None] * len(lines)
    for entity in included:
        for line in range(max(entity.start_line, 1), min(entity.end_line, len(lines)) + 1):
            owner[line - 1] = entity.stable_key

    runs: list[tuple[str, list[int]]] = []  # (owner, 1-based line numbers)
    for number, key in enumerate(owner, start=1):
        if key is None:
            continue
        if runs and runs[-1][0] == key and runs[-1][1][-1] == number - 1:
            runs[-1][1].append(number)
        else:
            runs.append((key, [number]))

    parts: dict[str, int] = {}
    chunks: list[Chunk] = []
    for key, numbers in runs:
        for first, last in _windows(source, lines, numbers, profile):
            start_byte, end_byte = lines[first - 1][0], lines[last - 1][1]
            for piece_start, piece_end in _pieces(source, start_byte, end_byte, profile):
                content = source[piece_start:piece_end].decode("utf-8", errors="replace")
                trimmed = content.rstrip()
                if not trimmed.strip():
                    continue
                piece_end = piece_start + len(trimmed.encode("utf-8"))
                owner_key = f"file:{path}" if key == "FILE" else key
                part = parts.get(owner_key, 0)
                parts[owner_key] = part + 1
                chunks.append(Chunk(
                    stable_chunk_key=f"{owner_key}#{part}",
                    entity_key=None if key == "FILE" else key,
                    content=trimmed,
                    content_hash=hashlib.sha256(trimmed.encode("utf-8")).hexdigest(),
                    token_count=count_tokens(profile, trimmed),
                    ordinal=len(chunks),
                    start_line=bisect.bisect_right(starts, piece_start),
                    end_line=bisect.bisect_right(starts, piece_end - 1),
                    start_byte=piece_start,
                    end_byte=piece_end,
                ))
    return chunks


def _windows(source, lines, numbers, profile) -> list[tuple[int, int]]:
    """Consecutive line windows over one run, each within the token budget where lines allow."""
    windows, start = [], 0
    while start < len(numbers):
        end, tokens = start, 0
        while end < len(numbers):
            line_start, line_end = lines[numbers[end] - 1]
            cost = count_tokens(profile, source[line_start:line_end].decode("utf-8", errors="replace"))
            if end > start and tokens + cost > profile.max_tokens:
                break
            tokens += cost
            end += 1
        windows.append((numbers[start], numbers[end - 1]))
        if end >= len(numbers):
            break
        start = max(end - profile.overlap_lines, start + 1)  # always progress
    return windows


def _pieces(source, start_byte, end_byte, profile) -> list[tuple[int, int]]:
    """Split a span that is still over budget (a single very long line) at character boundaries."""
    text = source[start_byte:end_byte].decode("utf-8", errors="replace")
    if count_tokens(profile, text) <= profile.max_tokens:
        return [(start_byte, end_byte)]
    budget = _tokenizer(profile)[1](profile.max_tokens)
    pieces, offset, size = [], start_byte, 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size and size + width > budget:
            pieces.append((offset, offset + size))
            offset, size = offset + size, 0
        size += width
    if size:
        pieces.append((offset, offset + size))
    return pieces
