"""Deduplicate manifest technologies and AI display-name twins globally."""

from __future__ import annotations

import re


def _get(record, key: str, default=None):
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _identity(value: str) -> str:
    """Aggressive identity scoped strictly to one analysis."""
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def _coordinate_parts(record) -> tuple[str, str] | None:
    name = _get(record, "name", "") or ""
    if ":" not in name:
        return None
    group, _, artifact = name.rpartition(":")
    if not group or not artifact:
        return None
    return _identity(group.rsplit(".", 1)[-1]), _identity(artifact)


def _same_technology(left, right) -> bool:
    """Compare coordinates asymmetrically with bare AI display names.

    Two real coordinates match only by artifact id, preserving sibling modules.
    A bare name may match either a coordinate artifact or its group-family tail.
    """
    left_coordinate = _coordinate_parts(left)
    right_coordinate = _coordinate_parts(right)
    left_name = _identity(_get(left, "name", ""))
    right_name = _identity(_get(right, "name", ""))

    if left_coordinate and right_coordinate:
        return left_coordinate[1] == right_coordinate[1]
    if left_coordinate:
        return right_name in left_coordinate
    if right_coordinate:
        return left_name in right_coordinate
    return bool(left_name and left_name == right_name)


def _is_manifest_record(record) -> bool:
    source = (_get(record, "detection_source", "") or "").casefold()
    return bool(_get(record, "matched_file")) or source.startswith("manifest")


def _prefer(left, right):
    """Prefer manifest evidence, then a source path, then higher confidence."""
    left_manifest = _is_manifest_record(left)
    right_manifest = _is_manifest_record(right)
    if left_manifest != right_manifest:
        return left if left_manifest else right

    left_file = bool(_get(left, "matched_file"))
    right_file = bool(_get(right, "matched_file"))
    if left_file != right_file:
        return left if left_file else right

    left_confidence = _get(left, "confidence", 0) or 0
    right_confidence = _get(right, "confidence", 0) or 0
    return left if left_confidence >= right_confidence else right


def dedup_records(records: list, *, on_merge=None) -> list:
    survivors: list = []
    for candidate in records:
        match_index = next(
            (index for index, existing in enumerate(survivors)
             if _same_technology(existing, candidate)),
            None,
        )
        if match_index is None:
            survivors.append(candidate)
            continue

        existing = survivors[match_index]
        preferred = _prefer(existing, candidate)
        removed = candidate if preferred is existing else existing
        survivors[match_index] = preferred
        if on_merge is not None:
            on_merge({
                "kept": _get(preferred, "name", ""),
                "removed": _get(removed, "name", ""),
                "reason": "analysis_identity_twin",
            })
    return survivors


def dedup_stack(stack: dict[str, list], *, on_merge=None) -> dict[str, list]:
    """Deduplicate across all role buckets, then rebuild from survivor roles."""
    flattened = [record for records in stack.values() for record in records]
    survivors = dedup_records(flattened, on_merge=on_merge)
    rebuilt = {role: [] for role in stack}
    for record in survivors:
        role = _get(record, "technology_role", "library") or "library"
        rebuilt.setdefault(role, []).append(record)
    return rebuilt
