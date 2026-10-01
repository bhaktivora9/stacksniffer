"""Chunk and embedding profiles, symbol-aware chunking and strict vector validation (no database)."""

import math
from dataclasses import replace

import pytest

from backend.services.semantic.chunker import ChunkEntity, build_chunks, count_tokens
from backend.services.semantic.embeddings import (
    DeterministicFakeProvider, EmbeddingDimensionError, EmbeddingError, GeminiEmbeddingProvider, validate_vector,
    vector_literal,
)
from backend.services.semantic.profiles import (
    DEFAULT_CHUNK_PROFILE, DEFAULT_EMBEDDING_PROFILE, R1_EMBEDDING_DIMENSION, ProfileError, canonical_json,
)

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
    assert chunks[0].entity_key is None and chunks[2].entity_key == "method:m.py::Cart.add"


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


def test_gemini_provider_requests_the_profiles_dimension_and_task():
    class Client:
        def __init__(self):
            self.calls = []

        def embed_content(self, **kwargs):
            self.calls.append(kwargs)
            return {"embedding": [[0.0] * R1_EMBEDDING_DIMENSION for _ in kwargs["content"]]}

    client = Client()
    results = GeminiEmbeddingProvider(client=client).embed(
        ["a", "b"], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")
    assert client.calls == [{"model": "models/gemini-embedding-001", "content": ["a", "b"],
                             "task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": 3072}]
    assert len(results) == 2 and results[0].metadata["model"] == "gemini-embedding-001"


def test_fake_provider_is_deterministic():
    provider = DeterministicFakeProvider()
    first = provider.embed(["x"], profile=DEFAULT_EMBEDDING_PROFILE, task="t")[0].values
    assert first == provider.embed(["x"], profile=DEFAULT_EMBEDDING_PROFILE, task="t")[0].values
    assert len(first) == R1_EMBEDDING_DIMENSION
