"""Rules for utilities that genuinely have no single architectural layer."""

from __future__ import annotations

import re

_IDENTITIES = {
    "hppc", "carrotsearchhppc", "guava", "commonscollections",
    "eclipsecollections", "fastutil", "trove", "commonsmath", "commonsmath3",
    "apachecommonsmath", "icu4j", "ibmicu4j", "commonslang", "commonslang3",
    "commonsio", "commonscodec", "lodash", "underscore", "ramda", "itertools",
    "either", "typingextensions",
}

LAYER_INFERENCE_NULL_PREFERENCE = """
CROSS-CUTTING LAYER RULE:
General-purpose utilities do not belong to a single architectural tier. Return
architectural_layer=null for collections/data structures, math/statistics,
internationalization/text, functional, string/date, serialization, type, and
reflection helpers. Null is the accurate preferred answer, not missing data.
Never infer backend merely because a utility appears in a server repository.
HPPC, Apache Commons Math, and ICU4J must be null. Assign backend only when a
technology is intrinsically server-side. When choosing between backend and null
for a general utility, choose null.
""".strip()


def normalize_identity(name: str) -> str:
    tail = name.rpartition(":")[2] if ":" in name else name
    return re.sub(r"[^a-z0-9]", "", tail.casefold())


def is_cross_cutting_utility(name: str, packages: list[str] | None = None) -> bool:
    identities = {normalize_identity(name)}
    identities.update(normalize_identity(package) for package in packages or [])
    return bool(identities & _IDENTITIES)


def guard_cross_cutting_layer(
    name: str, ai_layer, *, packages: list[str] | None = None, on_override=None,
):
    if ai_layer is not None and is_cross_cutting_utility(name, packages):
        if on_override is not None:
            on_override({"tech": name, "from": ai_layer, "to": None,
                         "reason": "cross_cutting_utility"})
        return None
    return ai_layer
