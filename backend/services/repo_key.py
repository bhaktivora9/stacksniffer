"""
Canonical repository identity for StackSniffer.

Every repository URL that enters the system is reduced to exactly one
canonical key:

    https://github.com/Vercel/Next.js.git    ->  "github:vercel/next.js"
    git@github.com:vercel/next.js.git        ->  "github:vercel/next.js"
    github.com/vercel/next.js/tree/canary    ->  "github:vercel/next.js"
    vercel/next.js                           ->  "github:vercel/next.js"

This key is the primary key of the `analyses_result` collection
(``_id = repo_key``), which means a bug in this module is a
duplicate-document bug. It is the single chokepoint that makes
deduplication structural rather than cosmetic.

Rules:
  - Call ``canonical_repo_key()`` ONCE, at the API boundary, the moment a
    URL enters the system.
  - Never touch the raw URL string again internally. Store it in
    ``analyses_request.repo_url_raw`` for audit and forget it.
  - ``canonical_repo_key()`` is idempotent: feeding it a key returns the
    key. Safe to call defensively.

Case folding
------------
Owner and repo are lowercased for ALL providers.

  - GitHub: correct. Owner and repo names are case-insensitive
    (github.com/Vercel/Next.js and github.com/vercel/next.js are the same
    repository), so folding is required for dedupe to work at all.
  - Bitbucket: correct. Workspace and repo slugs are lowercase by
    definition.
  - GitLab: technically lossy. GitLab project paths are case-sensitive,
    so two projects differing only in case would collide onto one key.
    This is accepted: it is vanishingly rare, GitLab itself discourages
    it, and one key per repo is worth more to us than the edge case.
    Revisit if GitLab support ever ships for real.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

__all__ = [
    "RepoKeyError",
    "canonical_repo_key",
    "try_canonical_repo_key",
    "parse_repo_key",
    "repo_key_to_url",
    "repo_full_name",
    "is_repo_key",
]


class RepoKeyError(ValueError):
    """Raised when a URL cannot be reduced to a canonical repo key."""


# ── Provider registry ────────────────────────────────────────────────────

_HOST_TO_PROVIDER: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}

_PROVIDER_TO_HOST: dict[str, str] = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
}

# Path segments that mark the end of the repository path and the start of
# provider UI routes. Only consulted at index >= 2, so a repo legitimately
# named "tree" or "compare" is unaffected.
#
#   github.com/vercel/next.js/tree/canary   -> stop at "tree"
#   github.com/vercel/next.js/pull/1234     -> stop at "pull"
_UI_SEGMENTS: frozenset[str] = frozenset(
    {
        "actions",
        "archive",
        "blame",
        "blob",
        "branches",
        "commit",
        "commits",
        "compare",
        "discussions",
        "graphs",
        "issues",
        "labels",
        "milestones",
        "network",
        "packages",
        "projects",
        "pull",
        "pulls",
        "raw",
        "releases",
        "security",
        "settings",
        "src",
        "tags",
        "tree",
        "wiki",
    }
)

# scp-style SSH:  git@github.com:vercel/next.js.git
# The (?!\d) guard prevents matching host:port forms.
_SCP_SSH_RE = re.compile(r"^(?:ssh://)?[\w.\-]+@([\w.\-]+):(?!\d)(.+)$")

# A canonical key. Owner may contain "/" (GitLab nested groups); the
# greedy owner group therefore splits on the LAST slash.
#
# Case-insensitive on purpose: a hand-typed "GitHub:Vercel/Next.js" must be
# recognised as a key and folded, NOT fall through to URL parsing (which
# would read "GitHub:Vercel" as a host and produce garbage).
_KEY_RE = re.compile(r"^(github|gitlab|bitbucket):([^:\s]+)/([^/:\s]+)$", re.IGNORECASE)

# A single path segment. Owner segments and repo names must match this.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")


# ── Internal helpers ─────────────────────────────────────────────────────


def _coerce_to_url(raw: str) -> str:
    """Turn any accepted input shape into something urlsplit can parse."""
    s = raw.strip().strip("<>\"'")
    if not s:
        raise RepoKeyError("empty repository URL")

    # scp-style SSH -> https
    m = _SCP_SSH_RE.match(s)
    if m:
        host, path = m.group(1), m.group(2)
        return f"https://{host}/{path.lstrip('/')}"

    if "://" in s:
        return s

    first = s.split("/", 1)[0].lower()
    if first.removeprefix("www.") in _HOST_TO_PROVIDER:
        # "github.com/vercel/next.js"
        return "https://" + s
    if "." not in first and "/" in s:
        # bare shorthand: "vercel/next.js" -> assume GitHub
        return "https://github.com/" + s
    return "https://" + s


def _extract_host(netloc: str) -> str:
    host = netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]  # ssh://git@github.com/...
    if ":" in host:
        host = host.split(":", 1)[0]  # strip port
    return host.removeprefix("www.")


def _truncate_ui_segments(segments: list[str]) -> list[str]:
    """Drop provider UI routes: [vercel, next.js, tree, canary] -> [vercel, next.js]"""
    out: list[str] = []
    for i, seg in enumerate(segments):
        if i >= 2 and seg.lower() in _UI_SEGMENTS:
            break
        out.append(seg)
    return out


def _validate_segment(seg: str, kind: str, source: str) -> None:
    if not seg:
        raise RepoKeyError(f"empty {kind} in {source!r}")
    if not _SEGMENT_RE.match(seg):
        raise RepoKeyError(f"invalid {kind} {seg!r} in {source!r}")


# ── Public API ───────────────────────────────────────────────────────────


def canonical_repo_key(url: str) -> str:
    """
    Reduce a repository URL to its canonical key.

    Returns
    -------
    str
        ``"{provider}:{owner}/{name}"`` — e.g. ``"github:vercel/next.js"``.
        For GitLab nested groups, ``owner`` may contain slashes:
        ``"gitlab:group/subgroup/project"``.

    Raises
    ------
    RepoKeyError
        On empty input, unsupported host, or an unparseable path.

    Idempotent: ``canonical_repo_key(canonical_repo_key(x)) == canonical_repo_key(x)``.
    """
    if url is None:
        raise RepoKeyError("repository URL is None")
    if not isinstance(url, str):
        raise RepoKeyError(f"repository URL must be str, got {type(url).__name__}")

    stripped = url.strip()
    if _KEY_RE.match(stripped):
        return stripped.lower()  # already a key

    parts = urlsplit(_coerce_to_url(url))
    host = _extract_host(parts.netloc)

    provider = _HOST_TO_PROVIDER.get(host)
    if provider is None:
        raise RepoKeyError(f"unsupported host {host!r} (from {url!r})")

    segments = [unquote(s) for s in parts.path.split("/") if s]

    # GitLab uses "/-/" to separate the project path from UI routes:
    #   gitlab.com/group/sub/proj/-/tree/main
    if "-" in segments:
        segments = segments[: segments.index("-")]

    segments = _truncate_ui_segments(segments)

    if len(segments) < 2:
        raise RepoKeyError(f"cannot parse owner/repo from {url!r}")

    if provider == "gitlab":
        # Nested groups are legal and part of the identity.
        owner_segments = segments[:-1]
        name = segments[-1]
    else:
        owner_segments = [segments[0]]
        name = segments[1]

    name = name.removesuffix(".git")

    for seg in owner_segments:
        _validate_segment(seg, "owner segment", url)
    _validate_segment(name, "repo name", url)

    owner = "/".join(s.lower() for s in owner_segments)
    return f"{provider}:{owner}/{name.lower()}"


def try_canonical_repo_key(url: str) -> str | None:
    """Non-raising variant. Returns None instead of raising RepoKeyError."""
    try:
        return canonical_repo_key(url)
    except RepoKeyError:
        return None


def is_repo_key(value: str) -> bool:
    """True if ``value`` is already a well-formed canonical key."""
    return bool(isinstance(value, str) and _KEY_RE.match(value.strip()))


def parse_repo_key(key: str) -> tuple[str, str, str]:
    """
    Split a canonical key into its parts.

    >>> parse_repo_key("github:vercel/next.js")
    ('github', 'vercel', 'next.js')
    >>> parse_repo_key("gitlab:group/subgroup/project")
    ('gitlab', 'group/subgroup', 'project')
    """
    if not isinstance(key, str):
        raise RepoKeyError(f"repo key must be str, got {type(key).__name__}")
    m = _KEY_RE.match(key.strip())
    if not m:
        raise RepoKeyError(f"malformed repo key: {key!r}")
    return m.group(1), m.group(2), m.group(3)


def repo_key_to_url(key: str) -> str:
    """Canonical HTTPS URL for a key. Inverse of canonical_repo_key (lossy on case)."""
    provider, owner, name = parse_repo_key(key)
    return f"https://{_PROVIDER_TO_HOST[provider]}/{owner}/{name}"


def repo_full_name(key: str) -> str:
    """
    ``"owner/name"`` without the provider prefix.

    Migration shim: existing code (github_service, seed_repos.json) speaks
    in GitHub full_name. Prefer the full key for storage; use this only at
    the GitHub API boundary.
    """
    _, owner, name = parse_repo_key(key)
    return f"{owner}/{name}"