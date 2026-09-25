"""Resolve public GitHub repository references to immutable commit SHAs.

This module only talks to the GitHub REST API. It never persists anything and
never downloads repository content.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from os import getenv
from urllib.parse import quote, urlparse

import httpx

PROVIDER = "github"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_HOSTS = {"github.com", "www.github.com"}
# Explicit per-phase limits so a slow TLS handshake and a slow body fail differently.
GITHUB_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_REPOSITORY_PATTERN = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?/?$")
_WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


class RepositoryResolutionError(Exception):
    """Base class for repository URL and reference resolution failures."""

    status_code = 400

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class InvalidRepositoryUrl(RepositoryResolutionError):
    """The input is not a well-formed public GitHub HTTPS repository URL."""


class UnsupportedRepositoryHost(RepositoryResolutionError):
    """The URL points at a provider or network location R1 does not accept."""


class InvalidReference(RepositoryResolutionError):
    """The requested reference cannot be a valid Git branch, tag or SHA."""


class RepositoryNotFound(RepositoryResolutionError):
    """GitHub has no public repository at this owner/name."""

    status_code = 404


class ReferenceNotFound(RepositoryResolutionError):
    """The repository exists but the branch, tag or SHA does not."""

    status_code = 404


class GitHubRateLimited(RepositoryResolutionError):
    """GitHub refused the request because the API rate limit was exceeded."""

    status_code = 429

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GitHubTimeout(RepositoryResolutionError):
    """GitHub did not respond within the configured timeouts."""

    status_code = 504


class GitHubUnavailable(RepositoryResolutionError):
    """GitHub returned an unexpected error or malformed response."""

    status_code = 502


@dataclass(frozen=True)
class ResolvedRepository:
    provider: str
    owner: str
    repository_name: str
    canonical_repository_key: str
    canonical_url: str
    requested_reference: str
    commit_sha: str
    default_branch: str

    @property
    def clone_url(self) -> str:
        return f"{self.canonical_url}.git"


def _canonicalize_owner_repo(owner: str, repository: str) -> tuple[str, str]:
    owner = (owner or "").strip()
    repository = (repository or "").strip().removesuffix(".git").rstrip("/")
    if not owner or not repository:
        raise InvalidRepositoryUrl("repository owner and name are required")
    if not _REPO_NAME_PATTERN.fullmatch(owner) or not _REPO_NAME_PATTERN.fullmatch(repository):
        raise InvalidRepositoryUrl("repository owner and name contain unsupported characters")
    return owner.lower(), repository.lower()


def _reject_unsupported_host(hostname: str) -> None:
    host = hostname.lower().rstrip(".")
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsupportedRepositoryHost("local hosts are not allowed")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise UnsupportedRepositoryHost("private-network and loopback addresses are not allowed")
        raise UnsupportedRepositoryHost("IP-address hosts are not supported; use github.com")
    if host not in GITHUB_HOSTS:
        raise UnsupportedRepositoryHost(f"unsupported repository host: {host}; only github.com is supported")


def parse_github_repository_url(url: str) -> tuple[str, str]:
    """Return the lowercased owner and repository name from a public GitHub HTTPS URL."""
    if not isinstance(url, str):
        raise InvalidRepositoryUrl("repository URL must be a string")
    value = url.strip()
    if not value:
        raise InvalidRepositoryUrl("repository URL is required")
    if any(ch.isspace() for ch in value):
        raise InvalidRepositoryUrl("repository URL cannot contain whitespace")
    if value.startswith(("/", "\\", "~", ".")) or _WINDOWS_PATH_PATTERN.match(value):
        raise InvalidRepositoryUrl("local paths are not supported; use an https://github.com URL")

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        raise InvalidRepositoryUrl("local paths are not supported; use an https://github.com URL")
    if scheme != "https":
        raise InvalidRepositoryUrl("repository URL must use https://")
    if parsed.username or parsed.password:
        raise InvalidRepositoryUrl("unsafe repository URL: embedded credentials are not allowed")
    if not parsed.hostname:
        raise InvalidRepositoryUrl("repository URL has no host")
    _reject_unsupported_host(parsed.hostname)
    if parsed.port is not None:
        raise UnsupportedRepositoryHost("repository URL must not specify a port")
    if parsed.query or parsed.fragment:
        raise InvalidRepositoryUrl("repository URL must not include query strings or fragments")

    match = _REPOSITORY_PATTERN.fullmatch(parsed.path)
    if not match:
        raise InvalidRepositoryUrl("url must point directly to a GitHub repository")
    return _canonicalize_owner_repo(match.group(1), match.group(2))


def canonicalize_repository_url(url: str) -> str:
    """Return a stable canonical repository identity: github:<owner>/<repo>.

    Besides public HTTPS URLs this also accepts SSH-style and already-canonical
    references, so stored identities can be normalized without a network call.
    """
    if not isinstance(url, str):
        raise InvalidRepositoryUrl("repository URL must be a string")
    value = url.strip()

    for prefix, label in (("github:", "canonical keys"), ("git@github.com:", "GitHub SSH URLs")):
        if value.lower().startswith(prefix):
            remainder = value[len(prefix):]
            if remainder.count("/") != 1:
                raise InvalidRepositoryUrl(f"{label} must point to one repository")
            owner, repository = _canonicalize_owner_repo(*remainder.split("/", 1))
            return f"{PROVIDER}:{owner}/{repository}"

    owner, repository = parse_github_repository_url(value)
    return f"{PROVIDER}:{owner}/{repository}"


def _validate_reference(reference: str) -> str:
    value = reference.strip()
    if not value:
        raise InvalidReference("reference must not be empty")
    if any(ch.isspace() or ord(ch) < 32 or ch in "~^:?*[\\" for ch in value):
        raise InvalidReference("reference contains characters that are not allowed in Git refs")
    if ".." in value or value.startswith(("/", "-")) or value.endswith(("/", ".", ".lock")):
        raise InvalidReference("reference is not a valid Git branch, tag or commit")
    return value


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _raise_for_rate_limit(response: httpx.Response) -> None:
    if response.status_code not in (403, 429):
        return
    remaining = response.headers.get("x-ratelimit-remaining")
    message = ""
    try:
        message = str(response.json().get("message", ""))
    except ValueError:
        pass
    if response.status_code == 429 or remaining == "0" or "rate limit" in message.lower():
        retry_after = response.headers.get("retry-after")
        raise GitHubRateLimited(
            "GitHub API rate limit exceeded; set GITHUB_TOKEN or retry later",
            retry_after_seconds=int(retry_after) if retry_after and retry_after.isdigit() else None,
        )


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    try:
        response = await client.get(f"{GITHUB_API_BASE}{path}", headers=_github_headers())
    except httpx.TimeoutException as exc:
        raise GitHubTimeout("GitHub did not respond in time") from exc
    except httpx.TransportError as exc:
        raise GitHubUnavailable("unable to reach GitHub") from exc
    _raise_for_rate_limit(response)
    return response


def _json_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubUnavailable("GitHub returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise GitHubUnavailable("GitHub returned a malformed response")
    return payload


async def _resolve(client: httpx.AsyncClient, url: str, reference: str | None) -> ResolvedRepository:
    owner, repository = parse_github_repository_url(url)
    requested = _validate_reference(reference) if reference is not None else None
    repo_path = f"/repos/{owner}/{repository}"

    repo_response = await _get(client, repo_path)
    if repo_response.status_code in (404, 451):
        raise RepositoryNotFound(f"repository github.com/{owner}/{repository} was not found")
    if repo_response.is_error:
        raise GitHubUnavailable(f"unable to inspect repository (GitHub {repo_response.status_code})")
    repo_payload = _json_object(repo_response)
    if repo_payload.get("private"):
        raise RepositoryNotFound(f"repository github.com/{owner}/{repository} is not public")
    default_branch = repo_payload.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ReferenceNotFound("repository has no default branch (it may be empty)")

    requested = requested or default_branch
    commit_response = await _get(client, f"{repo_path}/commits/{quote(requested, safe='/')}")
    if commit_response.status_code in (404, 409, 422):
        raise ReferenceNotFound(f"reference '{requested}' was not found in github.com/{owner}/{repository}")
    if commit_response.is_error:
        raise GitHubUnavailable(f"unable to resolve reference (GitHub {commit_response.status_code})")
    sha = _json_object(commit_response).get("sha")
    if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
        raise GitHubUnavailable("GitHub returned an invalid commit SHA")

    return ResolvedRepository(
        provider=PROVIDER,
        owner=owner,
        repository_name=repository,
        canonical_repository_key=f"{PROVIDER}:{owner}/{repository}",
        canonical_url=f"https://github.com/{owner}/{repository}",
        requested_reference=requested,
        commit_sha=sha,
        default_branch=default_branch,
    )


async def resolve_repository_reference(
    url: str,
    reference: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> ResolvedRepository:
    """Resolve a branch, tag or commit (default branch when omitted) to one commit SHA.

    Pass ``client`` to reuse a connection pool or to inject a mock transport.
    """
    if client is not None:
        return await _resolve(client, url, reference)
    async with httpx.AsyncClient(timeout=GITHUB_TIMEOUT) as owned_client:
        return await _resolve(owned_client, url, reference)
