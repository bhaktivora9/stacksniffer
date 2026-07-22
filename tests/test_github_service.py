from backend.services.github_service import _build_headers, canonicalize_languages


def test_github_headers_use_requested_rest_api_version():
    headers = _build_headers()

    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2026-03-10"


def test_canonicalize_languages_ranks_bytes_and_exposes_share():
    languages = canonicalize_languages({"Rust": 20, "TypeScript": 80})

    assert languages[0]["name"] == "TypeScript"
    assert languages[0]["is_primary"] is True
    assert languages[0]["byte_share"] == 0.8
    assert sum(language["byte_share"] for language in languages) == 1.0


def test_canonicalize_languages_folds_aliases_and_drops_non_languages():
    languages = canonicalize_languages(
        {"CSS": 30, "SCSS": 20, "Dockerfile": 100, "MDX": 50}
    )

    assert [(language["name"], language["byte_count"]) for language in languages] == [
        ("CSS", 50)
    ]
