"""Opt-in live smoke test against the Gemini API (google-genai).

Skipped unless STACKSNIFFER_LIVE_GEMINI=1 and GEMINI_API_KEY (or GOOGLE_API_KEY) are set in the
environment; backend/.env is never loaded by the test suite. Costs a few embedding calls.

    STACKSNIFFER_LIVE_GEMINI=1 GEMINI_API_KEY=... python -m pytest tests/test_gemini_live.py -q
"""

import math
import os

import pytest

from backend.services.semantic.embeddings import GeminiEmbeddingProvider, validate_vector
from backend.services.semantic.profiles import DEFAULT_EMBEDDING_PROFILE, R1_EMBEDDING_DIMENSION

pytestmark = pytest.mark.skipif(
    os.getenv("STACKSNIFFER_LIVE_GEMINI") != "1" or not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
    reason="live Gemini tests are opt-in: set STACKSNIFFER_LIVE_GEMINI=1 and GEMINI_API_KEY",
)


def test_live_embeddings_have_the_r1_dimension_and_rank_sensibly():
    profile = DEFAULT_EMBEDDING_PROFILE
    with GeminiEmbeddingProvider(timeout_seconds=30) as gemini:
        documents = gemini.embed(["def add(a, b):\n    return a + b", "class HttpServer:\n    def listen(self): ..."],
                                 profile=profile, task=profile.document_task)
        query = gemini.embed(["function that sums two numbers"], profile=profile, task=profile.query_task)[0]
    vectors = [validate_vector(result.values, profile) for result in documents]
    query_vector = validate_vector(query.values, profile)
    assert all(len(vector) == R1_EMBEDDING_DIMENSION for vector in vectors)

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    assert cosine(query_vector, vectors[0]) > cosine(query_vector, vectors[1])
