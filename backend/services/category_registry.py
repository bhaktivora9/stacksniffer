"""
backend/services/category_registry.py

Single source of truth for which tech categories are valid.

THE PROBLEM THIS SOLVES
-----------------------
The 8 standard categories were hardcoded as a set literal in at least four
places: dep_classifier._VALID_CATS, dep_classifier._store_emergent_categories,
analyze._STANDARD_CATEGORIES, and the React category renderer. When Gemini
invented a useful category (e.g. "bundler") and a human approved it, there was
no way to make the pipeline accept it — every one of those hardcoded gates
dropped it, and a set literal cannot be updated at runtime.

THE MODEL
---------
Categories live in the `dep_categories` collection with a `standard` flag:

    { _id: "bundler", standard: true,  status: "promoted",  ... }
    { _id: "orchestration", standard: false, status: "pending", ... }

The 8 originals are seeded standard=true. Emergent ones arrive standard=false
(pending review). Promoting a category flips standard=true, and from that moment
every gate that calls is_valid_category() accepts it — no code change, no
redeploy. Discarding removes it from the valid set.

A short in-process cache keeps this off the hot path; promotion busts it so the
change takes effect on the next analysis.
"""
from __future__ import annotations

import time
from backend.services.storage_service import BUILTIN_CATEGORIES

# The immutable core. These can never be discarded — they are the schema the
# frontend, the seeder, and the ground truth are all built around. Emergent
# categories layer ON TOP of these.
_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL = 30.0  # seconds — short, because promotion should feel immediate


def _now() -> float:
    return time.monotonic()


async def _load() -> set[str]:
    """
    Valid categories = builtins + every promoted emergent category.
    Cached for _CACHE_TTL; invalidate_cache() forces a reload after promotion.
    """
    global _CACHE, _CACHE_AT
    if _CACHE is not None and (_now() - _CACHE_AT) < _CACHE_TTL:
        return _CACHE

    valid = set(BUILTIN_CATEGORIES)
    try:
        import backend.services.storage_service as storage_service
        valid.update(await storage_service.get_valid_categories())
    except Exception:
        # Storage down or function missing -> fall back to builtins. Never let a
        # registry failure empty the valid set; that would drop every detection.
        pass

    _CACHE = valid
    _CACHE_AT = _now()
    return valid


async def valid_categories() -> set[str]:
    """The full set a detection's category may take right now."""
    return await _load()


async def is_valid_category(cat: str) -> bool:
    return cat in await _load()


async def is_emergent(cat: str) -> bool:
    """A real, non-builtin category — i.e. promoted from review."""
    return cat not in BUILTIN_CATEGORIES and cat in await _load()


def is_builtin(cat: str) -> bool:
    return cat in BUILTIN_CATEGORIES


def invalidate_cache() -> None:
    """Call after promote/discard so the next analysis sees the change."""
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0
