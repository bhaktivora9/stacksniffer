"""Chunk and embedding profiles, symbol-aware chunking and strict vector validation (no database)."""

import math
from dataclasses import replace

import pytest

from backend.services.semantic.chunker import ChunkEntity, build_chunks, count_tokens, file_kind
from backend.services.semantic.embeddings import (
    EmbeddingDimensionError, EmbeddingError, EmbeddingProfileIncompatible, GeminiEmbeddingProvider,
    OfflineEmbeddingProvider, check_compatibility, validate_vector, vector_literal,
)
from backend.services.semantic.profiles import (
    DEFAULT_CHUNK_PROFILE, DEFAULT_EMBEDDING_PROFILE, OFFLINE_EMBEDDING_PROFILE, R1_EMBEDDING_DIMENSION, ProfileError,
    canonical_json,
)
from backend.services.semantic.source_content import classify_source

SOURCE = b"""import os

class Cart:
    TAX = 0.2

    def add(self, item):
        return item

    def total(self):
        return 1

def helper():
    return os.getcwd()
"""
ENTITIES = [
    ChunkEntity("class:m.py::Cart", "CLASS", 3, 10),
    ChunkEntity("method:m.py::Cart.add", "METHOD", 6, 7),
    ChunkEntity("method:m.py::Cart.total", "METHOD", 9, 10),
    ChunkEntity("function:m.py::helper", "FUNCTION", 12, 13),
]


# --- profiles ------------------------------------------------------------------------------------


def test_profile_fingerprints_are_canonical_and_change_with_configuration():
    assert canonical_json({"b": 1, "a": [2, "é"]}) == '{"a":[2,"é"],"b":1}'
    same = replace(DEFAULT_CHUNK_PROFILE)
    assert same.fingerprint == DEFAULT_CHUNK_PROFILE.fingerprint
    assert replace(DEFAULT_CHUNK_PROFILE, max_tokens=256).fingerprint != DEFAULT_CHUNK_PROFILE.fingerprint
    assert replace(DEFAULT_EMBEDDING_PROFILE, profile_version=2).fingerprint != DEFAULT_EMBEDDING_PROFILE.fingerprint


def test_entity_type_order_does_not_change_the_profile():
    reordered = replace(DEFAULT_CHUNK_PROFILE, included_entity_types=tuple(reversed(
        DEFAULT_CHUNK_PROFILE.included_entity_types)))
    assert reordered.fingerprint == DEFAULT_CHUNK_PROFILE.fingerprint


def test_embedding_profiles_must_use_the_r1_dimension_and_a_revision():
    assert DEFAULT_EMBEDDING_PROFILE.dimension == R1_EMBEDDING_DIMENSION == 3072
    with pytest.raises(ProfileError, match="never truncated"):
        replace(DEFAULT_EMBEDDING_PROFILE, dimension=768)
    with pytest.raises(ProfileError, match="revision"):
        replace(DEFAULT_EMBEDDING_PROFILE, model_revision=" ")
    with pytest.raises(ProfileError):
        replace(DEFAULT_CHUNK_PROFILE, included_entity_types=("VARIABLE",))


# --- chunking ------------------------------------------------------------------------------------


def test_each_line_belongs_to_its_innermost_declaration():
    chunks = build_chunks("m.py", SOURCE, ENTITIES, DEFAULT_CHUNK_PROFILE)
    assert [(c.stable_chunk_key, c.start_line, c.end_line) for c in chunks] == [
        ("file:m.py#0", 1, 1),
        ("class:m.py::Cart#0", 3, 4),  # the class keeps its own lines, not its methods'
        ("method:m.py::Cart.add#0", 6, 7),
        ("method:m.py::Cart.total#0", 9, 10),
        ("function:m.py::helper#0", 12, 13),
    ]
    assert [c.ordinal for c in chunks] == list(range(5))
    assert chunks[1].content == "class Cart:\n    TAX = 0.2"
    assert chunks[0].entity_key == "file:m.py" and chunks[2].entity_key == "method:m.py::Cart.add"
    assert [c.chunk_kind for c in chunks] == ["CODE", "CLASS", "METHOD", "METHOD", "FUNCTION"]


def test_chunks_cite_exact_byte_ranges_and_hashes():
    for chunk in build_chunks("m.py", SOURCE, ENTITIES, DEFAULT_CHUNK_PROFILE):
        assert SOURCE[chunk.start_byte:chunk.end_byte].decode() == chunk.content
        assert chunk.token_count == count_tokens(DEFAULT_CHUNK_PROFILE, chunk.content) > 0
        assert len(chunk.content_hash) == 64


def test_without_file_chunks_code_outside_declarations_is_not_indexed():
    profile = replace(DEFAULT_CHUNK_PROFILE, included_entity_types=("CLASS", "METHOD", "FUNCTION"))
    keys = [c.stable_chunk_key for c in build_chunks("m.py", SOURCE, ENTITIES, profile)]
    assert not any(k.startswith("file:") for k in keys)


def test_long_declarations_split_into_overlapping_windows_within_budget():
    body = "".join(f"    value_{i:02d} = 12345\n" for i in range(20)).encode()  # 22 bytes -> 8 tokens a line
    source = b"def big():\n" + body
    profile = replace(DEFAULT_CHUNK_PROFILE, max_tokens=40, overlap_lines=2)
    chunks = build_chunks("b.py", source, [ChunkEntity("function:b.py::big", "FUNCTION", 1, 21)], profile)
    assert len(chunks) > 1
    assert all(c.token_count <= profile.max_tokens for c in chunks)
    assert [c.stable_chunk_key for c in chunks] == [f"function:b.py::big#{i}" for i in range(len(chunks))]
    for first, second in zip(chunks, chunks[1:]):
        assert second.start_line == first.end_line - 1  # two lines repeated
    assert chunks[0].start_line == 1 and chunks[-1].end_line == 21


def test_a_line_longer_than_the_budget_is_cut_at_character_boundaries():
    line = ("é" * 200).encode() + b"\n"
    profile = replace(DEFAULT_CHUNK_PROFILE, max_tokens=50)
    chunks = build_chunks("long.txt", line, [], profile)
    assert all(c.token_count <= 50 for c in chunks)
    assert "".join(c.content for c in chunks) == "é" * 200
    assert all(line[c.start_byte:c.end_byte].decode() == c.content for c in chunks)


def test_chunking_is_deterministic():
    runs = [build_chunks("m.py", SOURCE, ENTITIES, DEFAULT_CHUNK_PROFILE) for _ in range(2)]
    assert runs[0] == runs[1]


# --- vectors -------------------------------------------------------------------------------------


def test_vectors_of_another_dimension_are_rejected_not_coerced():
    with pytest.raises(EmbeddingDimensionError, match="768-dimensional"):
        validate_vector([0.1] * 768, DEFAULT_EMBEDDING_PROFILE)
    with pytest.raises(EmbeddingDimensionError):
        validate_vector([0.1] * (R1_EMBEDDING_DIMENSION + 1), DEFAULT_EMBEDDING_PROFILE)
    with pytest.raises(EmbeddingError, match="NaN"):
        validate_vector([math.nan] + [0.1] * (R1_EMBEDDING_DIMENSION - 1), DEFAULT_EMBEDDING_PROFILE)


def test_l2_normalization_is_applied_explicitly():
    vector = validate_vector([3.0, 4.0] + [0.0] * (R1_EMBEDDING_DIMENSION - 2), DEFAULT_EMBEDDING_PROFILE)
    assert vector[:2] == [0.6, 0.8]
    raw = replace(DEFAULT_EMBEDDING_PROFILE, normalization_policy="NONE", profile_version=2)
    assert validate_vector([3.0, 4.0] + [0.0] * (R1_EMBEDDING_DIMENSION - 2), raw)[:2] == [3.0, 4.0]
    assert vector_literal([0.5, 1.0]) == "[0.5,1.0]"


def test_offline_provider_is_deterministic_and_3072_dimensional():
    provider = OfflineEmbeddingProvider()
    first = provider.embed(["x"], profile=OFFLINE_EMBEDDING_PROFILE, task="t")[0].values
    assert first == OfflineEmbeddingProvider().embed(["x"], profile=OFFLINE_EMBEDDING_PROFILE, task="t")[0].values
    assert first != provider.embed(["y"], profile=OFFLINE_EMBEDDING_PROFILE, task="t")[0].values
    assert len(first) == R1_EMBEDDING_DIMENSION == OFFLINE_EMBEDDING_PROFILE.dimension


def test_a_provider_must_be_the_one_its_profile_names():
    check_compatibility(OfflineEmbeddingProvider(), OFFLINE_EMBEDDING_PROFILE)
    with pytest.raises(EmbeddingProfileIncompatible, match="'google' provider, not 'offline'") as caught:
        check_compatibility(OfflineEmbeddingProvider(), DEFAULT_EMBEDDING_PROFILE)
    assert caught.value.failure_code == "EMBEDDING_PROFILE_INCOMPATIBLE"
    assert GeminiEmbeddingProvider.name == DEFAULT_EMBEDDING_PROFILE.provider == "google"


def test_documents_carry_identifiers_only_and_queries_are_bare():
    text = OFFLINE_EMBEDDING_PROFILE.render("def add(): {path}", path="app/cart.py", kind="METHOD",
                                            symbol="Cart.add")
    assert text == "path: app/cart.py\nkind: METHOD\nsymbol: Cart.add\n\ndef add(): {path}"  # content is literal
    with pytest.raises(ProfileError, match="unknown template fields"):
        replace(OFFLINE_EMBEDDING_PROFILE, text_template="{repo} {text}")


# --- configuration and documentation -------------------------------------------------------------


@pytest.mark.parametrize("path, language, kind", [
    ("src/app.py", "python", "CODE"), ("pom.xml", "xml", "CONFIGURATION"), ("k8s/deploy.yaml", "yaml", "CONFIGURATION"),
    ("Dockerfile", "dockerfile", "CONFIGURATION"), ("src/application.properties", None, "CONFIGURATION"),
    ("build.gradle", "groovy", "CONFIGURATION"), (".env.example", None, "CONFIGURATION"),
    ("README.md", "markdown", "DOCUMENTATION"), ("docs/guide.rst", None, "DOCUMENTATION"),
    ("LICENSE", None, "DOCUMENTATION"), ("main.rs", "rust", "CODE"),
])
def test_files_are_classified_by_name_and_language(path, language, kind):
    assert file_kind(path, language) == kind


def test_a_configuration_file_is_chunked_whole_as_its_file_entity():
    source = b"server:\n  port: 8080\nspring:\n  datasource:\n    url: jdbc:h2:mem:test\n"
    chunks = build_chunks("src/main/resources/application.yml", source, [], DEFAULT_CHUNK_PROFILE, language="yaml")
    assert [(c.chunk_kind, c.entity_key, c.start_line, c.end_line) for c in chunks] == [
        ("CONFIGURATION", "file:src/main/resources/application.yml", 1, 5)]
    assert source[chunks[0].start_byte:chunks[0].end_byte].decode() == chunks[0].content


def test_markdown_is_split_at_headings_but_not_inside_code_fences():
    source = (b"# Service\nIntro.\n\n## Running\nUse make.\n```sh\n# not a heading\nmake run\n```\n"
              b"## Config\nSet PORT.\n")
    chunks = build_chunks("README.md", source, [], DEFAULT_CHUNK_PROFILE, language="markdown")
    assert [(c.section, c.start_line, c.end_line) for c in chunks] == [
        ("# Service", 1, 2), ("## Running", 4, 9), ("## Config", 10, 11)]
    assert {c.chunk_kind for c in chunks} == {"DOCUMENTATION"} and {c.entity_key for c in chunks} == {"file:README.md"}
    assert [c.stable_chunk_key for c in chunks] == ["file:README.md#0", "file:README.md#1", "file:README.md#2"]
    for chunk in chunks:
        assert source[chunk.start_byte:chunk.end_byte].decode() == chunk.content


def test_configuration_and_documentation_follow_the_profile():
    code_only = replace(DEFAULT_CHUNK_PROFILE, included_entity_types=("CLASS", "METHOD", "FUNCTION", "FILE"))
    assert build_chunks("README.md", b"# Title\ntext\n", [], code_only) == []
    assert build_chunks("app.yaml", b"a: 1\n", [], code_only, language="yaml") == []
    assert build_chunks("m.py", SOURCE, ENTITIES, code_only)  # code is unaffected


# --- source retention policy ---------------------------------------------------------------------


@pytest.mark.parametrize("path, data, status", [
    ("app/cart.py", b"def add():\n    return 1\n", "RETAINED"),
    (".env", b"DEBUG=1\n", "SECRET"),
    ("config/.env.production", b"DEBUG=1\n", "SECRET"),
    ("deploy/server.pem", b"anything\n", "SECRET"),
    ("home/.ssh/id_rsa", b"anything\n", "SECRET"),
    (".env.example", b"API_KEY=\n", "RETAINED"),  # a template documents names, not values
    (".env.example", b"KEY=AKIA" + b"ABCDEFGHIJKLMNOP\n", "SECRET"),  # ...but its content is still scanned
    ("app/settings.py", b"KEY = '-----BEGIN RSA PRIVATE KEY-----'\n", "SECRET"),
    ("app/client.py", b"TOKEN = 'ghp_" + b"a" * 36 + b"'\n", "SECRET"),
    ("assets/logo.svg", b"<svg>\x00</svg>", "BINARY"),
    ("data/latin1.txt", "café".encode("latin-1"), "BINARY"),
    pytest.param("data/huge.json", b"[" + b"1," * 600_000 + b"1]", "OVERSIZED", id="oversized"),
])
def test_source_retention_policy(path, data, status):
    assert classify_source(path, data) == status
