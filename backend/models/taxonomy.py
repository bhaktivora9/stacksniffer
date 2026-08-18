"""Locked v2 taxonomy and input canonicalization.

The enum values in this module are persistence/API values.  Aliases exist only
at ingestion boundaries and must never be emitted by v2 models.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeVar


class SoftwareType(str, Enum):
    DATABASE = "database"
    DATA_PIPELINE = "data_pipeline"
    WEB_APPLICATION = "web_application"
    DEPLOYABLE_SERVICE = "deployable_service"
    CLI_TOOL = "cli_tool"
    LIBRARY = "library"
    SDK = "sdk"
    FRAMEWORK = "framework"
    BUILD_TOOL = "build_tool"
    INFRASTRUCTURE_TOOL = "infrastructure_tool"
    DESKTOP_APPLICATION = "desktop_application"
    APPLICATION_PLATFORM = "application_platform"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SoftwareTypeDefinition:
    software_type: SoftwareType
    label: str
    consumer: str
    predicate: str
    aliases: tuple[str, ...] = ()
    sentinel: bool = False


# The enum is the canonical ID authority. Every consumer derives IDs from these
# enum members rather than repeating persistence strings.
SOFTWARE_TYPE_DEFINITIONS: tuple[SoftwareTypeDefinition, ...] = (
    SoftwareTypeDefinition(SoftwareType.DATABASE, "Database", "Other systems read/write data via a protocol.", "Primary artifact is a storage/search/cache/vector-store engine. Vector databases resolve here, not to novelty."),
    SoftwareTypeDefinition(SoftwareType.DATA_PIPELINE, "Data Pipeline", "Data flows through it between systems.", "Stream processor, batch ETL, orchestrator, or message broker; data movement, not storage."),
    SoftwareTypeDefinition(SoftwareType.WEB_APPLICATION, "Web Application", "End users interact via browser.", "A dominant frontend drives an end-user UI; classify the consumer app, not its framework.", ("web_app",)),
    SoftwareTypeDefinition(SoftwareType.DEPLOYABLE_SERVICE, "Deployable Service", "Other services call its endpoints, or operators run it as a standalone process.", "HTTP server or deployable process with no dominant frontend.", ("web_api", "api_service", "api", "service")),
    SoftwareTypeDefinition(SoftwareType.CLI_TOOL, "CLI Tool", "End users interact via terminal.", "A terminal binary is the primary artifact; CLI parser present and no HTTP server.", ("cli",)),
    SoftwareTypeDefinition(SoftwareType.LIBRARY, "Library", "Developers import it into their own code.", "Importable package. Check low in the tree so stronger product signals win. Imported AI libraries belong here."),
    SoftwareTypeDefinition(SoftwareType.SDK, "SDK", "Developers import it to integrate with a specific external service or platform.", "Client library scoped to one vendor or platform API; when ambiguous, use library."),
    SoftwareTypeDefinition(SoftwareType.FRAMEWORK, "Framework", "Developers build applications with it.", "Repository is reusable application scaffolding developers extend; build-time tooling is build_tool."),
    SoftwareTypeDefinition(SoftwareType.BUILD_TOOL, "Build Tool", "Developers use it to compile, bundle, or transform source at build time.", "Bundler, compiler, transpiler, package manager, or CSS/asset tooling."),
    SoftwareTypeDefinition(SoftwareType.INFRASTRUCTURE_TOOL, "Infrastructure Tool", "Operators deploy, monitor, or manage other systems.", "IaC, monitoring, service mesh, or secrets management; a browser UI alone does not make it a web application.", ("infra_tool", "infrastructure")),
    SoftwareTypeDefinition(SoftwareType.DESKTOP_APPLICATION, "Desktop Application", "End users interact through a native desktop application.", "Electron, Tauri, Qt, or another native desktop application targeting end users.", ("desktop_app",)),
    SoftwareTypeDefinition(SoftwareType.APPLICATION_PLATFORM, "Application Platform", "Users or developers interact with an integrated multi-surface product.", "Requires positive evidence of multiple artifacts under one product identity. Never use as a fallback for no clean match."),
    SoftwareTypeDefinition(SoftwareType.UNKNOWN, "Unknown", "There is insufficient signal to decide.", "Low-confidence or low-coverage terminal fallback. Strong evidence fitting no canonical type is novelty, not unknown.", sentinel=True),
)


class TechnologyRole(str, Enum):
    """What a technology is, independent of how a repository uses it."""

    LANGUAGES = "languages"
    FRAMEWORKS = "frameworks"
    DATABASES = "databases"
    MESSAGING = "messaging"
    AI_ML = "ai_ml"
    INFRA = "infra"
    TESTING = "testing"
    LIBRARY = "library"
    BUILD = "build"
    OBSERVABILITY = "observability"
    LANGUAGE_RUNTIME = "language_runtime"


# Roles promoted into the live analysis contract. Schema-known roles outside
# this set are representable proposals, not valid detection/correction targets.
ACTIVE_TECHNOLOGY_ROLES: tuple[str, ...] = tuple(role.value for role in (
    TechnologyRole.LANGUAGES,
    TechnologyRole.FRAMEWORKS,
    TechnologyRole.DATABASES,
    TechnologyRole.MESSAGING,
    TechnologyRole.AI_ML,
    TechnologyRole.INFRA,
    TechnologyRole.TESTING,
    TechnologyRole.LIBRARY,
))


_SOFTWARE_TYPE_ALIASES = {
    alias: definition.software_type
    for definition in SOFTWARE_TYPE_DEFINITIONS
    for alias in definition.aliases
}

_TECHNOLOGY_ROLE_ALIASES = {
    "language": TechnologyRole.LANGUAGES,
    "framework": TechnologyRole.FRAMEWORKS,
    "database": TechnologyRole.DATABASES,
    "ai/ml": TechnologyRole.AI_ML,
    "ai-ml": TechnologyRole.AI_ML,
    "aiml": TechnologyRole.AI_ML,
    "infrastructure": TechnologyRole.INFRA,
    "test": TechnologyRole.TESTING,
    "libraries": TechnologyRole.LIBRARY,
    "build_tool": TechnologyRole.BUILD,
    "build_tools": TechnologyRole.BUILD,
    "runtime": TechnologyRole.LANGUAGE_RUNTIME,
}

_EnumT = TypeVar("_EnumT", bound=Enum)


def _normalise(value: str | Enum) -> str:
    raw = value.value if isinstance(value, Enum) else value
    return str(raw).strip().casefold().replace("-", "_").replace(" ", "_")


def canonicalize_software_type(value: str | SoftwareType | None) -> SoftwareType:
    """Coerce legacy/spelling variants to a locked v2 software type."""

    if value is None:
        return SoftwareType.UNKNOWN
    normalized = _normalise(value)
    if normalized in _SOFTWARE_TYPE_ALIASES:
        return _SOFTWARE_TYPE_ALIASES[normalized]
    try:
        return SoftwareType(normalized)
    except ValueError:
        return SoftwareType.UNKNOWN


def canonicalize_technology_role(
    value: str | TechnologyRole,
) -> TechnologyRole:
    """Map accepted spelling drift to a locked role; reject unknown roles."""

    normalized = _normalise(value)
    if normalized in _TECHNOLOGY_ROLE_ALIASES:
        return _TECHNOLOGY_ROLE_ALIASES[normalized]
    return TechnologyRole(normalized)


# ─────────────────────────────────────────────────────────────────────────────
# specific_identity — observation layer for the deferred promotion decision.
#
# A repo's software_type stays canonical (one of the 13 above). specific_identity
# records the NARROWER category the classifier perceived when the canonical type
# only loosely covers the repo — e.g. a deployable_service whose purpose is model
# inference carries specific_identity "model_serving". This changes no shipped
# label; it captures the near-miss signal that was previously discarded into prose
# so that promotion candidates can be found by aggregation, on frequency evidence,
# rather than by a threshold guessed in advance.
#
# These are NOT canonical software types and MUST NOT be emitted in software_type.
# They are candidate emergent types, pending promotion. A value graduates to a
# SoftwareType member only when it recurs across the corpus and is operationally
# checkable — at which point it moves into SoftwareType and leaves this registry.
# ─────────────────────────────────────────────────────────────────────────────

# Known candidate identities observed in the corpus. This is a registry of
# EXPECTED values, not a closed set: the classifier may emit a novel snake_case
# identity not listed here (that is the discovery path). Listed values carry the
# canonical software_type they most commonly coerce to, for drift auditing.
CANDIDATE_SPECIFIC_IDENTITIES: dict[str, SoftwareType] = {
    "model_serving": SoftwareType.DEPLOYABLE_SERVICE,
    "workflow_orchestration": SoftwareType.DEPLOYABLE_SERVICE,
    "ci_cd_engine": SoftwareType.BUILD_TOOL,
    "game_engine": SoftwareType.FRAMEWORK,
    "mobile_app": SoftwareType.APPLICATION_PLATFORM,
    "browser_engine": SoftwareType.DESKTOP_APPLICATION,
}

# is_new (novelty) vs specific_identity are MUTUALLY EXCLUSIVE resolution paths:
#   - specific_identity: a canonical software_type fits loosely; record the refinement
#     (model_serving under deployable_service, ci_cd_engine under build_tool, ...).
#   - is_new: NO canonical software_type fits even loosely; emit emergent_software_type
#     and leave specific_identity null.
# language_runtime lives here, NOT in CANDIDATE_SPECIFIC_IDENTITIES, because a
# compiler/interpreter is not honestly a build_tool (build_tool transforms source at
# BUILD time; a runtime EXECUTES it). Calling cpython a build_tool asserts a canonical
# fit that isn't real, so language runtimes route through is_new. operating_system is
# the other member: a kernel fits none of the 13 even loosely.
# A value must NOT appear in both sets (enforced by _assert_novelty_partition below).
IS_NEW_EMERGENT_TYPES: frozenset[str] = frozenset({
    "language_runtime",
    "operating_system",
})


def _assert_novelty_partition() -> None:
    """Fail loudly if a type is registered in both novelty channels. A value in
    both sets is a resolution-path contradiction (one repo could emit is_new AND a
    specific_identity)."""
    overlap = set(CANDIDATE_SPECIFIC_IDENTITIES) & set(IS_NEW_EMERGENT_TYPES)
    if overlap:
        raise ValueError(
            f"Type(s) in BOTH specific_identity and is_new channels: {sorted(overlap)}. "
            "These paths are mutually exclusive; a type belongs to exactly one."
        )


_assert_novelty_partition()


def normalize_specific_identity(value: str | None) -> str | None:
    """Normalize a specific_identity string, or None when the canonical type
    fully captures the repo (the expected case for plain library/database/etc).

    Unlike software_type, this is intentionally OPEN: an unrecognized value is
    preserved (normalized), not coerced away — a novel identity is the discovery
    signal, and discarding it would defeat the field's purpose. This is the
    deliberate inverse of canonicalize_software_type's closed-set coercion.
    """
    if value is None:
        return None
    normalized = _normalise(value)
    if not normalized or normalized == "null" or normalized == "none":
        return None
    # If someone put a canonical software_type here, that's a category error:
    # specific_identity is for SUB-canonical identity only. Surface it as None so
    # it doesn't masquerade as novelty, rather than silently accepting it.
    try:
        SoftwareType(normalized)
        return None  # a canonical type is not a "specific identity"
    except ValueError:
        pass
    # An is_new-channel type must never appear as a specific_identity — the two
    # resolution paths are mutually exclusive. If the model tagged one here it
    # should have set software_type_is_new=true instead. Surface None so it can't
    # persist on the wrong channel; callers use specific_identity_belongs_to_is_new
    # to detect the misroute and flip it to is_new rather than dropping the signal.
    if normalized in IS_NEW_EMERGENT_TYPES:
        return None
    return normalized


def specific_identity_belongs_to_is_new(value: str | None) -> bool:
    """True when a value the model put in specific_identity actually belongs to the
    is_new channel (language_runtime, operating_system). Lets the pipeline detect a
    misrouted emergent type and flip it to software_type_is_new=true with
    emergent_software_type=<value>, instead of silently dropping it."""
    if value is None:
        return False
    return _normalise(value) in IS_NEW_EMERGENT_TYPES


def is_known_candidate_identity(value: str | None) -> bool:
    """True when value is a recognized (already-observed) candidate identity.
    False for None AND for genuinely novel identities — callers distinguishing
    'recurring candidate' from 'first sighting' should use this plus the
    normalized value."""
    normalized = normalize_specific_identity(value)
    return normalized is not None and normalized in CANDIDATE_SPECIFIC_IDENTITIES


# ─────────────────────────────────────────────────────────────────────────────
# Consistency guards — make the file the drift-free authority it claims to be.
# ─────────────────────────────────────────────────────────────────────────────

# ACTIVE_TECHNOLOGY_ROLES intentionally excludes BUILD, OBSERVABILITY, and
# LANGUAGE_RUNTIME: they are schema-known but not live detection targets. Expose
# that split explicitly so downstream code can check membership instead of
# rediscovering it as a runtime rejection.
INACTIVE_TECHNOLOGY_ROLES: tuple[str, ...] = tuple(
    role.value for role in TechnologyRole
    if role.value not in ACTIVE_TECHNOLOGY_ROLES
)


def is_active_technology_role(value: str | TechnologyRole) -> bool:
    """True when the (canonicalized) role is a live detection/correction target.
    A role can be schema-valid yet inactive; callers that emit detections should
    gate on this, not merely on canonicalize_technology_role succeeding."""
    try:
        role = canonicalize_technology_role(value)
    except ValueError:
        return False
    return role.value in ACTIVE_TECHNOLOGY_ROLES


def canonicalize_technology_role_safe(
    value: str | TechnologyRole,
) -> TechnologyRole | None:
    """Non-raising sibling of canonicalize_technology_role.

    canonicalize_technology_role raises ValueError on an unknown role, which is
    inconsistent with canonicalize_software_type (that degrades to UNKNOWN). Use
    this at ingestion boundaries where a novel/garbled role should degrade to
    None rather than crash the pipeline. Reserve the raising version for places
    that genuinely want to fail loudly on an unexpected role.
    """
    try:
        return canonicalize_technology_role(value)
    except ValueError:
        return None