"""
Tests for backend/services/repo_key.py

This module is the primary key of `analyses_result`. Every case that
escapes it becomes a duplicate document in production, so the suite is
deliberately paranoid about the equivalence class:

    "all these strings are the same repository"

Run:  pytest tests/test_repo_key.py -v
"""

import pytest

from backend.services.repo_key import (
    RepoKeyError,
    canonical_repo_key,
    is_repo_key,
    parse_repo_key,
    repo_full_name,
    repo_key_to_url,
    try_canonical_repo_key,
)

# ── The equivalence class: every form of one repo ────────────────────────
# These are the actual strings observed in the wild — pasted from browser
# address bars, copied from git remotes, typed by hand, read out of
# seed_repos.json. All must collapse to one key.

NEXTJS = "github:vercel/next.js"

NEXTJS_FORMS = [
    "https://github.com/vercel/next.js",
    "https://github.com/vercel/next.js/",
    "https://github.com/vercel/next.js.git",
    "https://github.com/vercel/next.js.git/",
    "http://github.com/vercel/next.js",
    "https://www.github.com/vercel/next.js",
    "http://www.github.com/vercel/next.js/",
    "github.com/vercel/next.js",
    "www.github.com/vercel/next.js",
    "vercel/next.js",
    "git@github.com:vercel/next.js.git",
    "git@github.com:vercel/next.js",
    "ssh://git@github.com/vercel/next.js.git",
    "https://github.com/vercel/next.js/tree/canary",
    "https://github.com/vercel/next.js/tree/canary/packages/next",
    "https://github.com/vercel/next.js/blob/canary/package.json",
    "https://github.com/vercel/next.js/pull/12345",
    "https://github.com/vercel/next.js/issues/1",
    "https://github.com/vercel/next.js/commit/a3f9c2b",
    "https://github.com/vercel/next.js/compare/v14...v15",
    "https://github.com/vercel/next.js/releases",
    "https://github.com/vercel/next.js/actions",
    "https://github.com/vercel/next.js?tab=readme-ov-file",
    "https://github.com/vercel/next.js#readme",
    "https://github.com/vercel/next.js/?foo=bar#baz",
    "  https://github.com/vercel/next.js  ",
    "<https://github.com/vercel/next.js>",
    '"https://github.com/vercel/next.js"',
    "github:vercel/next.js",
]

# The case-folding forms. GitHub treats these as the same repository, so
# we must too. This is the class that produced the duplicate next.js rows.
NEXTJS_CASE_FORMS = [
    "https://github.com/Vercel/Next.js",
    "https://github.com/VERCEL/NEXT.JS",
    "https://github.com/Vercel/Next.js.git",
    "GitHub.com/Vercel/Next.js",
    "git@GitHub.com:Vercel/Next.js.git",
    "Vercel/Next.js",
    "GitHub:Vercel/Next.js",
]


@pytest.mark.parametrize("url", NEXTJS_FORMS)
def test_nextjs_forms_collapse_to_one_key(url):
    assert canonical_repo_key(url) == NEXTJS


@pytest.mark.parametrize("url", NEXTJS_CASE_FORMS)
def test_case_variants_collapse_to_one_key(url):
    assert canonical_repo_key(url) == NEXTJS


def test_whole_equivalence_class_is_one_key():
    """The property that actually matters: set of keys has size 1."""
    keys = {canonical_repo_key(u) for u in NEXTJS_FORMS + NEXTJS_CASE_FORMS}
    assert keys == {NEXTJS}, f"equivalence class fractured into {keys}"


# ── Idempotency ──────────────────────────────────────────────────────────
# Called defensively all over the codebase; must be a fixed point.


@pytest.mark.parametrize("url", NEXTJS_FORMS + NEXTJS_CASE_FORMS)
def test_idempotent(url):
    once = canonical_repo_key(url)
    assert canonical_repo_key(once) == once
    assert canonical_repo_key(canonical_repo_key(once)) == once


# ── The seed corpus ──────────────────────────────────────────────────────
# Guards the exact repos in seed_repos.json. A regression here means the
# next re-seed silently duplicates the corpus.


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/fastapi/fastapi", "github:fastapi/fastapi"),
        ("https://github.com/apache/kafka", "github:apache/kafka"),
        ("https://github.com/huggingface/transformers", "github:huggingface/transformers"),
        ("https://github.com/pydantic/pydantic", "github:pydantic/pydantic"),
        ("https://github.com/ray-project/ray", "github:ray-project/ray"),
        ("https://github.com/rust-lang/rust", "github:rust-lang/rust"),
        ("https://github.com/spring-projects/spring-boot", "github:spring-projects/spring-boot"),
        ("https://github.com/gin-gonic/gin", "github:gin-gonic/gin"),
        ("https://github.com/chroma-core/chroma", "github:chroma-core/chroma"),
        ("https://github.com/nestjs/nest", "github:nestjs/nest"),
        ("https://github.com/supabase/supabase", "github:supabase/supabase"),
        ("https://github.com/grafana/grafana", "github:grafana/grafana"),
        ("https://github.com/elastic/elasticsearch", "github:elastic/elasticsearch"),
    ],
)
def test_seed_corpus_repos(url, expected):
    assert canonical_repo_key(url) == expected


# ── Names with dots, dashes, underscores ─────────────────────────────────
# next.js is the reason. Dot-stripping bugs eat the ".js" or the ".git".


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/vercel/next.js", "github:vercel/next.js"),
        ("https://github.com/vercel/next.js.git", "github:vercel/next.js"),
        ("https://github.com/socketio/socket.io", "github:socketio/socket.io"),
        ("https://github.com/socketio/socket.io.git", "github:socketio/socket.io"),
        ("https://github.com/foo/bar.git.git", "github:foo/bar.git"),
        ("https://github.com/foo/my_repo", "github:foo/my_repo"),
        ("https://github.com/foo/my-repo", "github:foo/my-repo"),
        ("https://github.com/foo/a.b.c.d", "github:foo/a.b.c.d"),
        ("https://github.com/my-org-2/repo_v2.1", "github:my-org-2/repo_v2.1"),
    ],
)
def test_punctuation_in_names(url, expected):
    assert canonical_repo_key(url) == expected


def test_git_suffix_stripped_once_only():
    """.git is a suffix, not a substring to purge."""
    assert canonical_repo_key("https://github.com/foo/gitignore") == "github:foo/gitignore"
    assert canonical_repo_key("https://github.com/git/git") == "github:git/git"
    assert canonical_repo_key("https://github.com/git/git.git") == "github:git/git"


# ── Repos named like UI routes ───────────────────────────────────────────
# UI truncation must only apply at index >= 2, or github.com/foo/tree dies.


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/foo/tree", "github:foo/tree"),
        ("https://github.com/foo/issues", "github:foo/issues"),
        ("https://github.com/foo/compare", "github:foo/compare"),
        ("https://github.com/tree/tree", "github:tree/tree"),
        ("https://github.com/pull/pull", "github:pull/pull"),
        ("https://github.com/foo/tree/tree/main", "github:foo/tree"),
    ],
)
def test_repo_named_like_ui_route(url, expected):
    assert canonical_repo_key(url) == expected


# ── GitLab: nested groups are part of the identity ───────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://gitlab.com/group/project", "gitlab:group/project"),
        ("https://gitlab.com/group/project.git", "gitlab:group/project"),
        ("https://gitlab.com/group/subgroup/project", "gitlab:group/subgroup/project"),
        (
            "https://gitlab.com/group/sub1/sub2/project",
            "gitlab:group/sub1/sub2/project",
        ),
        ("https://gitlab.com/group/sub/proj/-/tree/main", "gitlab:group/sub/proj"),
        ("https://gitlab.com/group/proj/-/blob/main/README.md", "gitlab:group/proj"),
        ("https://gitlab.com/group/proj/-/merge_requests/42", "gitlab:group/proj"),
        ("https://gitlab.com/group/proj/tree/main", "gitlab:group/proj"),
        ("git@gitlab.com:group/subgroup/project.git", "gitlab:group/subgroup/project"),
        ("gitlab.com/gitlab-org/gitlab", "gitlab:gitlab-org/gitlab"),
    ],
)
def test_gitlab(url, expected):
    assert canonical_repo_key(url) == expected


def test_gitlab_subgroup_is_not_the_same_as_flat():
    """group/sub/proj and group/proj are different repositories."""
    a = canonical_repo_key("https://gitlab.com/group/sub/proj")
    b = canonical_repo_key("https://gitlab.com/group/proj")
    assert a != b


# ── Bitbucket ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://bitbucket.org/team/repo", "bitbucket:team/repo"),
        ("https://bitbucket.org/team/repo.git", "bitbucket:team/repo"),
        ("https://bitbucket.org/team/repo/src/master/", "bitbucket:team/repo"),
        ("git@bitbucket.org:team/repo.git", "bitbucket:team/repo"),
    ],
)
def test_bitbucket(url, expected):
    assert canonical_repo_key(url) == expected


# ── Providers do not collide ─────────────────────────────────────────────


def test_same_path_different_provider_are_different_repos():
    keys = {
        canonical_repo_key("https://github.com/foo/bar"),
        canonical_repo_key("https://gitlab.com/foo/bar"),
        canonical_repo_key("https://bitbucket.org/foo/bar"),
    }
    assert len(keys) == 3


# ── Rejections ───────────────────────────────────────────────────────────
# Failing loudly at the API boundary beats writing a garbage _id.


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "\n",
        "https://gitea.com/foo/bar",  # unsupported host
        "https://example.com/foo/bar",
        "https://github.com",  # no path
        "https://github.com/",
        "https://github.com/vercel",  # owner only
        "https://github.com/vercel/",
        "next.js",  # no owner
        "not a url at all",
        "https://github.com//bar",
        "https://github.com/foo/-",  # invalid repo name
        "https://github.com/-foo/bar",  # segment cannot start with -
        "github:foo",  # malformed key
        "gitea:foo/bar",  # unknown provider in key form
    ],
)
def test_rejects_garbage(bad):
    with pytest.raises(RepoKeyError):
        canonical_repo_key(bad)


@pytest.mark.parametrize("bad", [None, 42, [], {}, b"https://github.com/foo/bar"])
def test_rejects_non_string(bad):
    with pytest.raises(RepoKeyError):
        canonical_repo_key(bad)


def test_ftp_scheme_still_resolves_host():
    """Documented behaviour: we key on host, not scheme. Kept explicit so a
    future change to reject non-http schemes is a deliberate decision."""
    # ftp://github.com/foo/bar has a supported host -> accepted.
    # If this ever needs to reject, change the code and this test together.
    assert canonical_repo_key("ftp://github.com/foo/bar") == "github:foo/bar"


# ── try_ variant ─────────────────────────────────────────────────────────


def test_try_returns_none_instead_of_raising():
    assert try_canonical_repo_key("https://example.com/x/y") is None
    assert try_canonical_repo_key("") is None
    assert try_canonical_repo_key(None) is None
    assert try_canonical_repo_key("https://github.com/foo/bar") == "github:foo/bar"


# ── is_repo_key ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("github:vercel/next.js", True),
        ("gitlab:group/sub/proj", True),
        ("bitbucket:team/repo", True),
        ("  github:foo/bar  ", True),
        ("https://github.com/foo/bar", False),
        ("foo/bar", False),
        ("github:foo", False),
        ("gitea:foo/bar", False),
        ("", False),
        (None, False),
        (42, False),
    ],
)
def test_is_repo_key(value, expected):
    assert is_repo_key(value) is expected


# ── parse_repo_key ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key,expected",
    [
        ("github:vercel/next.js", ("github", "vercel", "next.js")),
        ("gitlab:group/project", ("gitlab", "group", "project")),
        ("gitlab:group/subgroup/project", ("gitlab", "group/subgroup", "project")),
        ("gitlab:a/b/c/d", ("gitlab", "a/b/c", "d")),
        ("bitbucket:team/repo", ("bitbucket", "team", "repo")),
    ],
)
def test_parse_repo_key(key, expected):
    assert parse_repo_key(key) == expected


@pytest.mark.parametrize("bad", ["github:foo", "foo/bar", "", "gitea:a/b", None, 7])
def test_parse_repo_key_rejects_malformed(bad):
    with pytest.raises(RepoKeyError):
        parse_repo_key(bad)


# ── Round-trip ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "github:vercel/next.js",
        "github:fastapi/fastapi",
        "gitlab:group/subgroup/project",
        "bitbucket:team/repo",
    ],
)
def test_key_to_url_round_trips(key):
    assert canonical_repo_key(repo_key_to_url(key)) == key


def test_key_to_url_shape():
    assert repo_key_to_url("github:vercel/next.js") == "https://github.com/vercel/next.js"
    assert repo_key_to_url("gitlab:g/s/p") == "https://gitlab.com/g/s/p"
    assert repo_key_to_url("bitbucket:t/r") == "https://bitbucket.org/t/r"


# ── repo_full_name (GitHub API boundary shim) ────────────────────────────


def test_repo_full_name():
    assert repo_full_name("github:vercel/next.js") == "vercel/next.js"
    assert repo_full_name("github:fastapi/fastapi") == "fastapi/fastapi"
    assert repo_full_name("gitlab:group/sub/proj") == "group/sub/proj"


# ── URL encoding ─────────────────────────────────────────────────────────


def test_percent_encoding_is_decoded():
    assert canonical_repo_key("https://github.com/foo/bar%2Ebaz") == "github:foo/bar.baz"


# ── Ports ────────────────────────────────────────────────────────────────


def test_port_is_stripped():
    assert canonical_repo_key("https://github.com:443/foo/bar") == "github:foo/bar"


# ── The regression this module exists to prevent ─────────────────────────


def test_duplicate_nextjs_rows_are_impossible():
    """
    The observed bug: `vercel/next.js` appeared twice in the similar-repos
    panel because two documents existed for one repository. With _id =
    repo_key, that requires two distinct keys for one repo. Prove it can't
    happen for the forms that actually reached the API.
    """
    observed_in_the_wild = [
        "https://github.com/vercel/next.js",
        "https://github.com/vercel/next.js/",
        "https://github.com/vercel/next.js.git",
        "https://github.com/Vercel/Next.js",
        "github.com/vercel/next.js",
        "vercel/next.js",
        "git@github.com:vercel/next.js.git",
        "https://github.com/vercel/next.js/tree/canary",
    ]
    assert len({canonical_repo_key(u) for u in observed_in_the_wild}) == 1