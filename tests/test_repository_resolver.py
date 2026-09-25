import asyncio

import httpx
import pytest

from backend.services.repository_resolver import (
    GITHUB_TIMEOUT,
    GitHubRateLimited,
    GitHubTimeout,
    GitHubUnavailable,
    InvalidReference,
    InvalidRepositoryUrl,
    ReferenceNotFound,
    RepositoryNotFound,
    RepositoryResolutionError,
    UnsupportedRepositoryHost,
    canonicalize_repository_url,
    parse_github_repository_url,
    resolve_repository_reference,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


# --- URL parsing (no network) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/OpenAI/Example",
        "https://github.com/openai/example.git",
        "https://github.com/openai/example/",
        "https://www.github.com/OpenAI/example.git/",
        "  https://github.com/openai/example  ",
    ],
)
def test_urls_normalize_suffix_slash_and_casing(url):
    assert parse_github_repository_url(url) == ("openai", "example")
    assert canonicalize_repository_url(url) == "github:openai/example"


def test_canonicalize_also_accepts_ssh_and_canonical_keys():
    assert canonicalize_repository_url("git@github.com:OpenAI/example.git") == "github:openai/example"
    assert canonicalize_repository_url("github:OpenAI/Example") == "github:openai/example"


@pytest.mark.parametrize(
    "url, error",
    [
        ("", InvalidRepositoryUrl),
        ("not-a-url", InvalidRepositoryUrl),
        ("http://github.com/openai/example", InvalidRepositoryUrl),
        ("git@github.com:openai/example.git", InvalidRepositoryUrl),
        ("https://user:token@github.com/openai/example", InvalidRepositoryUrl),
        ("https://github.com/openai/example?tab=readme", InvalidRepositoryUrl),
        ("https://github.com/openai/example/issues", InvalidRepositoryUrl),
        ("https://github.com/openai", InvalidRepositoryUrl),
        ("/home/me/repos/example", InvalidRepositoryUrl),
        ("./example", InvalidRepositoryUrl),
        (r"C:\repos\example", InvalidRepositoryUrl),
        ("file:///home/me/example", InvalidRepositoryUrl),
        ("https://gitlab.com/openai/example", UnsupportedRepositoryHost),
        ("https://github.com.evil.example/openai/example", UnsupportedRepositoryHost),
        ("https://localhost/openai/example", UnsupportedRepositoryHost),
        ("https://git.localhost/openai/example", UnsupportedRepositoryHost),
        ("https://127.0.0.1/openai/example", UnsupportedRepositoryHost),
        ("https://10.0.0.5/openai/example", UnsupportedRepositoryHost),
        ("https://192.168.1.10/openai/example", UnsupportedRepositoryHost),
        ("https://169.254.169.254/openai/example", UnsupportedRepositoryHost),
        ("https://[::1]/openai/example", UnsupportedRepositoryHost),
        ("https://140.82.112.3/openai/example", UnsupportedRepositoryHost),
        ("https://github.com:8443/openai/example", UnsupportedRepositoryHost),
    ],
)
def test_unsafe_or_unsupported_urls_raise_typed_errors(url, error):
    with pytest.raises(error):
        parse_github_repository_url(url)


def test_typed_errors_share_a_base_class_and_carry_http_status():
    assert issubclass(UnsupportedRepositoryHost, RepositoryResolutionError)
    assert RepositoryNotFound("x").status_code == 404
    assert GitHubRateLimited("x").status_code == 429
    assert GitHubTimeout("x").status_code == 504


def test_timeouts_are_explicit_per_phase():
    assert GITHUB_TIMEOUT.connect == 5.0
    assert GITHUB_TIMEOUT.read == 10.0


# --- resolution with mocked GitHub -----------------------------------------------------------


def _resolve(handler, url="https://github.com/OpenAI/Example.git", reference=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve_repository_reference(url, reference, client=client)

    return asyncio.run(run())


def _github(repo=None, commits=None, repo_status=200):
    """Mock GitHub: ``commits`` maps a ref to a SHA; unknown refs return 422 like the real API."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.raw_path.decode()
        if path == "/repos/openai/example":
            return httpx.Response(repo_status, json=repo if repo is not None else {"default_branch": "main", "private": False})
        prefix = "/repos/openai/example/commits/"
        if path.startswith(prefix):
            ref = httpx.URL(path).path.removeprefix(prefix)
            if ref in (commits or {}):
                return httpx.Response(200, json={"sha": commits[ref]})
            return httpx.Response(422, json={"message": f"No commit found for SHA: {ref}"})
        return httpx.Response(404, json={"message": "Not Found"})

    handler.requests = requests
    return handler


def test_default_branch_is_resolved_when_reference_is_absent():
    handler = _github(commits={"main": SHA})
    resolved = _resolve(handler)

    assert resolved.provider == "github"
    assert resolved.owner == "openai"
    assert resolved.repository_name == "example"
    assert resolved.canonical_repository_key == "github:openai/example"
    assert resolved.canonical_url == "https://github.com/openai/example"
    assert resolved.clone_url == "https://github.com/openai/example.git"
    assert resolved.requested_reference == "main"
    assert resolved.default_branch == "main"
    assert resolved.commit_sha == SHA
    assert handler.requests[0].headers["accept"] == "application/vnd.github+json"


@pytest.mark.parametrize("reference", ["develop", "v1.2.0", "feature/login", SHA[:7], SHA])
def test_branches_tags_and_shas_resolve_to_one_sha(reference):
    resolved = _resolve(_github(commits={reference: SHA}), reference=reference)
    assert resolved.requested_reference == reference
    assert resolved.default_branch == "main"
    assert resolved.commit_sha == SHA


def test_reference_is_url_encoded():
    handler = _github(commits={"v1.0#rc": SHA})
    _resolve(handler, reference="v1.0#rc")
    assert handler.requests[1].url.raw_path.decode().endswith("/commits/v1.0%23rc")


def test_missing_repository_raises_repository_not_found():
    with pytest.raises(RepositoryNotFound):
        _resolve(_github(repo={"message": "Not Found"}, repo_status=404))


def test_private_repository_is_treated_as_not_found():
    with pytest.raises(RepositoryNotFound, match="not public"):
        _resolve(_github(repo={"default_branch": "main", "private": True}))


def test_empty_repository_without_default_branch_is_reported():
    with pytest.raises(ReferenceNotFound, match="default branch"):
        _resolve(_github(repo={"default_branch": None, "private": False}))


def test_missing_reference_raises_reference_not_found():
    with pytest.raises(ReferenceNotFound, match="'does-not-exist'"):
        _resolve(_github(commits={"main": SHA}), reference="does-not-exist")


@pytest.mark.parametrize("reference", ["", "   ", "bad ref", "a..b", "-rf", "refs/", "x.lock", "a:b"])
def test_invalid_references_fail_before_calling_github(reference):
    handler = _github(commits={"main": SHA})
    with pytest.raises(InvalidReference):
        _resolve(handler, reference=reference)
    assert handler.requests == []


def test_invalid_url_fails_before_calling_github():
    handler = _github()
    with pytest.raises(UnsupportedRepositoryHost):
        _resolve(handler, url="https://gitlab.com/openai/example")
    assert handler.requests == []


def test_primary_rate_limit_is_detected_from_headers():
    def handler(request):
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "retry-after": "60"},
            json={"message": "API rate limit exceeded"},
        )

    with pytest.raises(GitHubRateLimited) as info:
        _resolve(handler)
    assert info.value.retry_after_seconds == 60


def test_secondary_rate_limit_and_429_are_detected():
    with pytest.raises(GitHubRateLimited):
        _resolve(lambda request: httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}))
    with pytest.raises(GitHubRateLimited):
        _resolve(lambda request: httpx.Response(429, json={}))


def test_forbidden_without_rate_limit_is_a_github_error_not_rate_limit():
    with pytest.raises(GitHubUnavailable):
        _resolve(lambda request: httpx.Response(403, headers={"x-ratelimit-remaining": "42"}, json={"message": "Forbidden"}))


def test_timeouts_raise_github_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(GitHubTimeout):
        _resolve(handler)


def test_connection_errors_raise_github_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GitHubUnavailable):
        _resolve(handler)


def test_server_errors_and_malformed_payloads_raise_github_unavailable():
    with pytest.raises(GitHubUnavailable):
        _resolve(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(GitHubUnavailable, match="invalid commit SHA"):
        _resolve(_github(commits={"main": "not-a-sha"}))
    with pytest.raises(GitHubUnavailable, match="malformed"):
        _resolve(lambda request: httpx.Response(200, text="<html>"))
