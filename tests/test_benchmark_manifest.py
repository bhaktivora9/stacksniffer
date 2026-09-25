import asyncio
from pathlib import Path

import pytest

from backend.services.benchmark_manifest import (
    EvaluationPurpose,
    ManifestValidationError,
    load_manifest,
    parse_manifest,
    parse_manifest_yaml,
)
from backend.services.manifest_verification import verify_manifest_commits
from backend.services.repository_resolver import ReferenceNotFound, ResolvedRepository

ROOT = Path(__file__).resolve().parent.parent
SHA_A = "a" * 40
SHA_B = "b" * 40

VALID_YAML = f"""
schema_version: 1
dataset_version: "r1.0.0"
repositories:
  - id: python-service
    url: https://github.com/Example/Python-Service.git
    frozen_commit: "{SHA_A}"
    language: Python
    license: MIT
    evaluation_purpose: retrieval_test
  - id: java-service
    url: https://github.com/example/java-service
    frozen_commit: "{SHA_B}"
    language: java
    license: Apache-2.0
    evaluation_purpose: contrastive_train
"""


def _repo(**overrides):
    repo = {
        "id": "python-service",
        "url": "https://github.com/example/python-service",
        "frozen_commit": SHA_A,
        "language": "python",
        "license": "MIT",
        "evaluation_purpose": "development",
    }
    repo.update(overrides)
    return repo


def _manifest(*repos, **overrides):
    data = {"schema_version": 1, "dataset_version": "r1.0.0", "repositories": list(repos) or [_repo()]}
    data.update(overrides)
    return data


def _errors(data) -> str:
    with pytest.raises(ManifestValidationError) as info:
        parse_manifest(data)
    return "\n".join(info.value.errors)


# --- valid manifests -------------------------------------------------------------------------


def test_valid_manifest_is_typed_and_normalized():
    manifest = parse_manifest_yaml(VALID_YAML)

    first = manifest.repositories[0]
    assert first.url == "https://github.com/example/python-service"
    assert first.canonical_repository_key == "github:example/python-service"
    assert first.language == "python"
    assert first.evaluation_purpose is EvaluationPurpose.RETRIEVAL_TEST
    assert first.dataset_role == "TEST"
    assert manifest.repositories[1].dataset_role == "TRAIN"


def test_checked_in_manifests_are_valid():
    for path in (ROOT / "repositories.yaml", ROOT / "backend" / "evaluation" / "repositories.yaml"):
        assert load_manifest(path).fingerprint.startswith("sha256:")


def test_fingerprint_is_deterministic_and_ignores_formatting():
    fingerprint = parse_manifest_yaml(VALID_YAML).fingerprint
    assert fingerprint == parse_manifest_yaml(VALID_YAML).fingerprint

    reordered = f"""
# comments, key order, entry order and URL spelling do not change the dataset
repositories:
  - evaluation_purpose: contrastive_train
    license: Apache-2.0
    language: JAVA
    frozen_commit: "{SHA_B}"
    url: https://www.github.com/example/java-service/
    id: java-service
  - id: python-service
    url: https://github.com/example/python-service
    frozen_commit: "{SHA_A}"
    language: python
    license: MIT
    evaluation_purpose: retrieval_test
dataset_version: "r1.0.0"
schema_version: 1
"""
    assert parse_manifest_yaml(reordered).fingerprint == fingerprint


def test_fingerprint_changes_when_content_changes():
    base = parse_manifest(_manifest()).fingerprint
    assert parse_manifest(_manifest(_repo(frozen_commit=SHA_B))).fingerprint != base
    assert parse_manifest(_manifest(dataset_version="r1.0.1")).fingerprint != base
    assert parse_manifest(_manifest(_repo(evaluation_purpose="demo"))).fingerprint != base


# --- frozen_commit ---------------------------------------------------------------------------


@pytest.mark.parametrize("reference", ["main", "v1.2.0", "release/2024", "HEAD"])
def test_branch_and_tag_names_are_rejected_as_frozen_commit(reference):
    assert "looks like a branch or tag name" in _errors(_manifest(_repo(frozen_commit=reference)))


def test_abbreviated_and_uppercase_shas_are_rejected():
    assert "use the full 40-character commit SHA" in _errors(_manifest(_repo(frozen_commit="efb2927")))
    assert "39 hex characters" in _errors(_manifest(_repo(frozen_commit="a" * 39)))
    assert "lowercase" in _errors(_manifest(_repo(frozen_commit="A" * 40)))


def test_unquoted_numeric_sha_gets_a_quoting_hint():
    text = VALID_YAML.replace(f'"{SHA_A}"', "1" * 40)
    with pytest.raises(ManifestValidationError) as info:
        parse_manifest_yaml(text)
    assert "quote the value in YAML" in str(info.value)


# --- field validation ------------------------------------------------------------------------


def test_unknown_evaluation_purpose_is_rejected():
    assert "repositories[0].evaluation_purpose" in _errors(_manifest(_repo(evaluation_purpose="training")))


@pytest.mark.parametrize(
    "url, fragment",
    [
        ("https://gitlab.com/example/repo", "unsupported repository host"),
        ("http://github.com/example/repo", "https"),
        ("https://user:token@github.com/example/repo", "credentials"),
        ("https://github.com/example", "point directly"),
    ],
)
def test_invalid_urls_are_rejected(url, fragment):
    assert fragment in _errors(_manifest(_repo(url=url)))


def test_invalid_license_id_and_schema_fields_are_rejected():
    assert "SPDX" in _errors(_manifest(_repo(license="MIT License")))
    assert "unsupported schema_version" in _errors(_manifest(schema_version=2))
    assert "dataset_version" in _errors(_manifest(dataset_version=""))
    assert "repositories" in _errors(_manifest(repositories=[]))
    assert "Extra inputs are not permitted" in _errors(_manifest(_repo(branch="main")))


def test_missing_fields_are_reported_by_location():
    repo = _repo()
    del repo["license"]
    assert "repositories[0].license: Field required" in _errors(_manifest(repo))


# --- cross-entry rules -----------------------------------------------------------------------


def test_duplicate_ids_fail_with_both_positions():
    errors = _errors(_manifest(_repo(), _repo(url="https://github.com/example/other", frozen_commit=SHA_B)))
    assert "repositories[1]: duplicate id 'python-service' (first used at repositories[0])" in errors


def test_duplicate_repository_commit_identity_fails_even_with_different_url_spelling():
    errors = _errors(_manifest(
        _repo(),
        _repo(id="python-service-copy", url="https://github.com/EXAMPLE/python-service.git"),
    ))
    assert "is already listed as 'python-service'" in errors


def test_same_repository_at_different_commits_is_allowed():
    manifest = parse_manifest(_manifest(_repo(), _repo(id="python-service-v2", frozen_commit=SHA_B)))
    assert len(manifest.repositories) == 2


def test_contrastive_train_and_retrieval_test_cannot_share_a_repository():
    errors = _errors(_manifest(
        _repo(id="train", evaluation_purpose="contrastive_train"),
        _repo(id="test", frozen_commit=SHA_B, evaluation_purpose="retrieval_test"),
    ))
    assert "'test' (retrieval_test) and 'train' (contrastive_train)" in errors


def test_all_problems_are_reported_together():
    errors = _errors(_manifest(
        _repo(frozen_commit="main"),
        _repo(license="not a license"),
    ))
    assert "frozen_commit" in errors and "license" in errors


# --- YAML loading ----------------------------------------------------------------------------


def test_duplicate_yaml_keys_are_rejected():
    text = VALID_YAML.replace("    license: MIT\n", "    license: MIT\n    license: Apache-2.0\n", 1)
    with pytest.raises(ManifestValidationError) as info:
        parse_manifest_yaml(text)
    assert "duplicate key 'license'" in str(info.value)


def test_malformed_yaml_and_non_mapping_are_rejected():
    with pytest.raises(ManifestValidationError, match="invalid YAML"):
        parse_manifest_yaml("repositories: [unclosed")
    with pytest.raises(ManifestValidationError, match="top level must be a mapping"):
        parse_manifest_yaml("- just\n- a list\n")


def test_load_manifest_names_the_file_in_errors(tmp_path):
    path = tmp_path / "repositories.yaml"
    path.write_text(VALID_YAML.replace(f'"{SHA_A}"', "main"), encoding="utf-8")
    with pytest.raises(ManifestValidationError) as info:
        load_manifest(path)
    assert str(path) in str(info.value)


# --- remote verification is separate ---------------------------------------------------------


def _resolved(url, sha):
    owner, name = url.removeprefix("https://github.com/").split("/")
    return ResolvedRepository("github", owner, name, f"github:{owner}/{name}", url, sha, sha, "main")


def test_verification_reports_matches_mismatches_and_missing_commits():
    manifest = parse_manifest_yaml(VALID_YAML)

    async def fake_resolve(url, reference):
        if "java" in url:
            raise ReferenceNotFound("reference was not found")
        return _resolved(url, reference)

    results = {r.repository_id: r for r in asyncio.run(verify_manifest_commits(manifest, fake_resolve))}
    assert results["python-service"].ok is True
    assert results["java-service"].ok is False
    assert "ReferenceNotFound" in results["java-service"].error

    async def wrong_commit(url, reference):
        return _resolved(url, "c" * 40)

    results = asyncio.run(verify_manifest_commits(manifest, wrong_commit))
    assert all(not r.ok and "not the declared commit" in r.error for r in results)
