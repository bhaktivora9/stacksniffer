"""
backend/services/github_service.py

Key changes from previous version:
  1. Priority-sort file tree before truncation at 200.
     GitHub returns files alphabetically — docs/ fills the window before source/.
     Now: manifests → source roots → tests → config → docs → other
     Hard-exclude translated docs (docs/{lang}/ where lang != en)

  2. Exempt GitHub Actions from ALWAYS_TRY_ROOT
     (already fetched via workflow_files logic)

  3. file_tree returned includes priority-sorted files, not alphabetical slice
"""
import base64
import asyncio
import os
import re
import time
import warnings
from fnmatch import fnmatch

import httpx
from dotenv import load_dotenv

from backend.models.schemas import RepoData
from backend.services.manifest_parser import is_recognized_manifest, selected_product_manifest_paths
from backend.services.repo_key import parse_repo_key

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
_HEAD_SHA_CACHE: dict[tuple[str, str | None], dict[str, str]] = {}
_LANGUAGES_CACHE: dict[str, dict] = {}

if not GITHUB_TOKEN:
    warnings.warn(
        "GITHUB_TOKEN not set. Rate limit: 60 req/hr. Set GITHUB_TOKEN in .env "
        "to get 5000 req/hr. Analysis may fail after a small number of repos.",
        RuntimeWarning,
        stacklevel=2,
    )

_KEY_FILES = {
    "pom.xml", "package.json", "requirements.txt",
    "build.gradle", "build.gradle.kts", "go.mod",
    "Cargo.toml", "Dockerfile", "docker-compose.yml",
    "setup.py", "pyproject.toml",
    "tsconfig.json", "Chart.yaml", "values.yaml",
    "skaffold.yaml", "angular.json",
    "next.config.js", "next.config.ts",
    "jest.config.js", "jest.config.ts",
    "vitest.config.ts", "playwright.config.ts",
    "setup.cfg", "Cargo.toml", "Gemfile", "composer.json",
    "build.gradle.kts",
}

_FILTER_PATTERNS = (
    "node_modules/",
    ".git/",
    "__pycache__/",
    "*.png", "*.jpg", "*.svg", "*.ico", "*.woff",
    ".github/"
)

# Fetch these from repo root even if not in 200-file tree
ALWAYS_TRY_ROOT = [
    "pyproject.toml", "requirements.txt", "package.json",
    "pom.xml", "build.gradle", "go.mod", "Cargo.toml",
    "tsconfig.json", "setup.py", "Dockerfile",
    "uv.lock",           # lockfile — confirms dependency group structure
    "poetry.lock",       # poetry projects
    "Pipfile",           # pipenv projects
]

_MAX_FILE_BYTES = 50 * 1024
MAX_FILES_TO_FETCH = 20

# Priority tiers for file tree sorting
# Lower number = higher priority = included first in 200-file window
_PRIORITY_TIER = {
    # Tier 1: dependency manifests — MUST be in window
    "pyproject.toml": 1, "requirements.txt": 1, "setup.py": 1,
    "package.json": 1, "pom.xml": 1, "build.gradle": 1,
    "build.gradle.kts": 1, "go.mod": 1, "Cargo.toml": 1,
    "setup.cfg": 1, "Pipfile": 1,
    # Tier 2: lock files — confirm dependency groups
    "uv.lock": 2, "poetry.lock": 2, "yarn.lock": 2, "package-lock.json": 2,
    # Tier 3: key config files
    "tsconfig.json": 3, "Chart.yaml": 3, "Dockerfile": 3,
    "docker-compose.yml": 3, "docker-compose.yaml": 3,
    "next.config.js": 3, "angular.json": 3, "go.sum": 3,
    # Tier 4: CI/CD
    ".github/workflows": 4,  # prefix match — see _path_tier()
    # Tier 5: source files (not docs)
    # matched by exclusion — anything not docs/ or .github/
    # Tier 6: English docs only
    "docs/en/": 6,
    # Tier 7: everything else (translated docs, generated files)
}

_TRANSLATED_DOCS_RE = re.compile(
    r"^docs/(?!en/)([a-z]{2}(?:-[a-zA-Z]{2,4})?)/",
)


def _path_tier(path: str) -> int:
    """
    Assign a priority tier to a file path.
    Lower tier = higher priority = included in 200-file window first.
    """
    basename = path.split("/")[-1]

    # Tier 1-3: exact filename match
    if basename in _PRIORITY_TIER:
        return _PRIORITY_TIER[basename]

    # Tier 4: CI workflows
    if path.startswith(".github/workflows/"):
        return 4

    # Tier 7: translated docs — hard exclude from window
    # (German/French/Japanese docs add zero detection signal)
    if _TRANSLATED_DOCS_RE.match(path):
        return 7

    # Tier 6: English docs
    if path.startswith("docs/en/"):
        return 6

    # Tier 7: other docs
    if path.startswith("docs/"):
        return 7

    # Tier 5: source files, tests, scripts
    return 5


class RepoNotFoundError(Exception):
    pass


class GitHubRateLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"GitHub rate limit hit. Retry after {retry_after}s")


def _parse_repo_url(repo_url: str) -> tuple[str, str]:
    repo_url = repo_url.strip().rstrip("/")

    full_url = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:/tree/[^/]+)?$", repo_url
    )
    if full_url:
        return full_url.group(1), full_url.group(2)

    short = re.match(r"^([^/]+)/([^/]+)$", repo_url)
    if short:
        return short.group(1), short.group(2)

    raise ValueError(f"Invalid GitHub repo URL or identifier: {repo_url!r}")


def _build_headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


async def get_languages(repo_key: str) -> dict[str, int]:
    """Fetch GitHub Linguist byte totals without ever sinking an analysis."""
    try:
        provider, owner, repo = parse_repo_key(repo_key)
        if provider != "github":
            return {}

        headers = _build_headers()
        cached = _LANGUAGES_CACHE.get(repo_key)
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/languages",
                headers=headers,
            )

        if resp.status_code == 304 and cached:
            return dict(cached.get("languages", {}))
        if resp.status_code == 404 or _is_rate_limited(resp):
            return {}
        resp.raise_for_status()

        languages = {
            str(name): int(byte_count)
            for name, byte_count in resp.json().items()
            if int(byte_count) > 0
        }
        _LANGUAGES_CACHE[repo_key] = {
            "etag": resp.headers.get("ETag"),
            "languages": languages,
        }
        return languages
    except Exception:
        return {}


_LANGUAGE_ALIASES = {"SCSS": "CSS"}
_NON_LANGUAGES = {"Dockerfile", "MDX", "Makefile"}
_LANGUAGE_NOISE_FLOOR = 0.001


def canonicalize_languages(raw: dict[str, int]) -> list[dict]:
    """Fold Linguist aliases and return byte-ranked, UI-ready languages."""
    folded: dict[str, int] = {}
    for raw_name, raw_bytes in raw.items():
        name = _LANGUAGE_ALIASES.get(raw_name, raw_name)
        byte_count = int(raw_bytes or 0)
        if name in _NON_LANGUAGES or byte_count <= 0:
            continue
        folded[name] = folded.get(name, 0) + byte_count

    total = sum(folded.values())
    if not total:
        return []

    ranked = sorted(folded.items(), key=lambda item: (-item[1], item[0]))
    return [
        {
            "name": name,
            "category": "languages",
            "confidence": 1.0,
            "detection_source": "github_linguist",
            "byte_count": byte_count,
            "byte_share": byte_count / total,
            "is_primary": index == 0,
            "below_noise_floor": byte_count / total < _LANGUAGE_NOISE_FLOOR,
        }
        for index, (name, byte_count) in enumerate(ranked)
    ]


def _retry_after(resp: httpx.Response, default: int = 60) -> int:
    value = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset")
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    if resp.headers.get("X-RateLimit-Reset") and not resp.headers.get("Retry-After"):
        return max(parsed - int(time.time()), 1)
    return parsed


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining == "0":
        return True
    try:
        message = resp.json().get("message", "").lower()
    except Exception:
        message = resp.text.lower()
    return "rate limit" in message


async def get_head_sha(repo_key: str, branch: str | None = None) -> str:
    """1 API call. Use ETag caching (If-None-Match -> 304 costs no rate
    limit). Falls back to default_branch when branch is None."""
    provider, owner, repo = parse_repo_key(repo_key)
    if provider != "github":
        raise ValueError(f"Unsupported provider for GitHub fetch: {provider}")

    cache_key = (repo_key, branch)
    headers = _build_headers()
    cached = _HEAD_SHA_CACHE.get(cache_key)
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]

    if branch:
        url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
        params = None
    else:
        url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        params = {"per_page": "1"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers, params=params)

    if resp.status_code == 304 and cached and cached.get("sha"):
        return cached["sha"]
    if resp.status_code == 404:
        raise RepoNotFoundError(f"Repository {owner}/{repo} not found")
    if _is_rate_limited(resp):
        raise GitHubRateLimitError(retry_after=_retry_after(resp))
    resp.raise_for_status()

    payload = resp.json()
    if branch:
        sha = payload.get("sha")
    else:
        sha = payload[0].get("sha") if payload else None
    if not sha:
        raise ValueError(f"Could not resolve head SHA for {repo_key}")

    etag = resp.headers.get("ETag")
    if etag:
        _HEAD_SHA_CACHE[cache_key] = {"etag": etag, "sha": sha}
    return sha


async def _fetch_file(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    path: str,
    headers: dict,
    max_bytes: int,
) -> tuple[str, str | None]:
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
            headers=headers,
        )
        if _is_rate_limited(resp):
            raise GitHubRateLimitError(retry_after=_retry_after(resp))
        if resp.status_code != 200:
            return path, None
        payload = resp.json()
        if payload.get("size", 0) > max_bytes:
            return path, None
        encoded = payload.get("content", "")
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        return path, decoded
    except GitHubRateLimitError:
        raise
    except Exception:
        return path, None


def _is_filtered(path: str) -> bool:
    for pattern in _FILTER_PATTERNS:
        if pattern.endswith("/"):
            if path.startswith(pattern) or f"/{pattern}" in path:
                return True
        elif fnmatch(path, pattern) or fnmatch(path.split("/")[-1], pattern):
            return True
    return False


async def fetch_repo(repo_url: str) -> RepoData:
    owner, repo = _parse_repo_url(repo_url)

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── Metadata ──────────────────────────────────────────────────────────
        meta_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=_build_headers(),
        )
        if meta_resp.status_code == 404:
            raise RepoNotFoundError(f"Repository {owner}/{repo} not found")
        if _is_rate_limited(meta_resp):
            raise GitHubRateLimitError(retry_after=_retry_after(meta_resp))
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        default_branch = meta.get("default_branch", "main")
        license_id = None
        if meta.get("license"):
            license_id = meta["license"].get("spdx_id")

        # ── Topics ────────────────────────────────────────────────────────────
        topics_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/topics",
            headers=_build_headers(
                accept="application/vnd.github.mercy-preview+json"
            ),
        )
        topics: list[str] = []
        if topics_resp.status_code == 200:
            topics = topics_resp.json().get("names", [])

        # ── File tree (priority-sorted before truncation) ─────────────────────
        tree_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}",
            headers=_build_headers(),
            params={"recursive": "1"},
        )
        if _is_rate_limited(tree_resp):
            raise GitHubRateLimitError(retry_after=_retry_after(tree_resp))
        tree_resp.raise_for_status()

        raw_blobs = [
            item["path"]
            for item in tree_resp.json().get("tree", [])
            if item.get("type") == "blob" and not _is_filtered(item["path"])
        ]

        # Sort by priority tier BEFORE truncating to 200
        # This ensures manifests/source always make it in, docs don't crowd them out
        raw_blobs.sort(key=_path_tier)
        manifest_paths = [p for p in raw_blobs if is_recognized_manifest(p)]
        file_tree = list(dict.fromkeys(raw_blobs[:200] + manifest_paths))
        tree_set  = set(file_tree)

        # ── Files to fetch content for ────────────────────────────────────────
        workflow_files = sorted([
            p for p in tree_set
            if p.startswith(".github/workflows/") and p.endswith(".yml")
        ])[:3]

        # Build the fetch list DETERMINISTICALLY and manifest-first.
        #
        # This used to be `list(_KEY_FILES & tree_set)` — a set. Python
        # randomises string hashing per process (PYTHONHASHSEED), so set
        # iteration order changed on every uvicorn restart. Combined with the
        # MAX_FILES_TO_FETCH truncation below, a DIFFERENT subset of files was
        # analysed run to run for identical input. That is a source of "fixed in
        # one run, regressed in the next" — the detection code was never the
        # variable.
        #
        # Order is priority, because the tail gets truncated:
        #   1. product manifests  — the authoritative detection layer
        #   2. key files in tree  — tier-sorted (manifests, locks, config)
        #   3. root fallbacks     — not in tree; most 404 on any given repo
        #   4. CI workflows       — weakest evidence, first to be cut
        product_manifests = list(selected_product_manifest_paths(file_tree))
        key_files_in_tree = sorted(_KEY_FILES & tree_set, key=lambda p: (_path_tier(p), p))
        root_fallbacks = [f for f in ALWAYS_TRY_ROOT if f not in tree_set]

        files_to_fetch: list[str] = []
        for group in (product_manifests, key_files_in_tree, root_fallbacks, workflow_files):
            for path in group:
                if path not in files_to_fetch:
                    files_to_fetch.append(path)
        files_to_fetch = files_to_fetch[:MAX_FILES_TO_FETCH]

        # ── Fetch file contents ───────────────────────────────────────────────
        tasks = [
            _fetch_file(client, owner, repo, path, _build_headers(), _MAX_FILE_BYTES)
            for path in files_to_fetch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        file_contents = {path: content for path, content in results if content}

    return RepoData(
        owner=owner,
        name=meta["name"],
        full_name=meta["full_name"],
        description=meta.get("description"),
        stars=meta.get("stargazers_count", 0),
        forks=meta.get("forks_count", 0),
        topics=topics,
        license=license_id,
        default_branch=default_branch,
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        file_tree=file_tree,
        file_contents=file_contents,
    )
def to_repo_metadata(repo: RepoData) -> dict:
    """
    RepoData -> the `repo` sub-document stored on analyses_result.

    Takes RepoData, NOT the raw GitHub API payload: fetch_repo() already
    normalised the payload and is the only caller. Passing the RepoData model
    straight into Mongo raises InvalidDocument (bson cannot encode a pydantic
    model), so this must produce a plain dict.

    full_name comes from the API's own response, which follows renames, so a
    repo that moved (tiangolo/fastapi -> fastapi/fastapi) reports its current
    name here even though repo_key still reflects the URL the user pasted.
    """
    return {
        "full_name":      repo.full_name,
        "description":    repo.description,
        "stars":          repo.stars,
        "forks":          repo.forks,
        "default_branch": repo.default_branch,
        "html_url":       f"https://github.com/{repo.full_name}",
        "topics":         list(repo.topics or []),
        "license":        repo.license,
        "created_at":     repo.created_at,
        "updated_at":     repo.updated_at,
    }
