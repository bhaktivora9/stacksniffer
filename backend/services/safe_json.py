"""
Robust parsing for Gemini JSON responses.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict


_REPAIR_STATS: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


def _strip_fences(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _try(raw: str):
    try:
        return json.loads(raw), True
    except (json.JSONDecodeError, TypeError):
        return None, False


def _repair_close(s: str) -> str:
    in_str = False
    escaped = False
    stack: list[str] = []
    for ch in s:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    repaired = s
    if in_str:
        repaired += '"'
    repaired = re.sub(r",\s*$", "", repaired)
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


def _salvage_array(s: str) -> list | None:
    start = s.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    obj_start = None
    objects: list = []
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                chunk = s[obj_start : i + 1]
                obj, ok = _try(chunk)
                if ok:
                    objects.append(obj)
                obj_start = None
    return objects or None


def _object_prefix(s: str) -> str:
    start = s.find("{")
    if start == -1:
        return s
    depth = 0
    in_str = False
    escaped = False
    last_complete = None
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 1:
            last_complete = i
    if last_complete is None:
        return s
    return s[start:last_complete] + "}"


def _bump_repair(site: str, stage: str) -> None:
    _REPAIR_STATS[site][stage] += 1


def bump_repair_counter(site: str, stage: str) -> None:
    _bump_repair(site, stage)


def get_repair_counters() -> dict[str, dict[str, int]]:
    return {site: dict(stages) for site, stages in _REPAIR_STATS.items()}


def safe_parse_gemini_json(
    raw: str,
    *,
    expect: str = "auto",
    on_repair=None,
    site: str = "unknown",
):
    """Parse Gemini JSON with defensive fallback."""
    s = _strip_fences(raw)

    value, ok = _try(s)
    if ok:
        return value

    looks_array = expect == "array" or (expect == "auto" and s.lstrip().startswith("["))
    looks_object = expect == "object" or (expect == "auto" and s.lstrip().startswith("{"))

    if looks_array:
        salvaged = _salvage_array(s)
        if salvaged:
            _bump_repair(site, "array_salvage")
            if on_repair:
                on_repair("array_salvage")
            return salvaged

    repaired = _repair_close(s)
    value, ok = _try(repaired)
    if ok:
        _bump_repair(site, "repair_close")
        if on_repair:
            on_repair("repair_close")
        return value

    if looks_object:
        value, ok = _try(_object_prefix(s))
        if ok:
            _bump_repair(site, "object_prefix")
            if on_repair:
                on_repair("object_prefix")
            return value

    _bump_repair(site, "unrecoverable")
    if on_repair:
        on_repair("unrecoverable")
    return None
