"""The Gemini embedding provider on google-genai, tested offline at the client boundary
(`client.models.embed_content`): no network, no API key."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types

from backend.services.semantic.embeddings import (
    GEMINI_MAX_BATCH, EmbeddingDimensionError, EmbeddingProviderUnavailable, EmbeddingRateLimited,
    EmbeddingRequestError, GeminiEmbeddingProvider, validate_vector,
)
from backend.services.semantic.profiles import DEFAULT_EMBEDDING_PROFILE, R1_EMBEDDING_DIMENSION


class FakeModels:
    def __init__(self, dimension=R1_EMBEDDING_DIMENSION, error=None, truncated=None):
        self.dimension, self.error, self.truncated = dimension, error, truncated
        self.calls = []

    def embed_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=list(contents), config=config))
        if self.error is not None:
            raise self.error
        statistics = SimpleNamespace(token_count=7, truncated=self.truncated)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.25] * self.dimension, statistics=statistics)
                                           for _ in contents])


class FakeClient:
    def __init__(self, **options):
        self.models = FakeModels(**options)
        self.closed = False

    def close(self):
        self.closed = True


def provider(**options):
    client = FakeClient(**options)
    return GeminiEmbeddingProvider(client=client), client


def api_error(kind, code, status):
    return kind(code, {"error": {"code": code, "message": "details withheld", "status": status}})


def test_requests_the_profiles_dimension_task_and_model_explicitly():
    gemini, client = provider()
    results = gemini.embed(["a", "b"], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")
    call = client.models.calls[0]
    assert (call.model, call.contents) == ("gemini-embedding-001", ["a", "b"])
    assert isinstance(call.config, types.EmbedContentConfig)
    assert (call.config.task_type, call.config.output_dimensionality) == ("RETRIEVAL_DOCUMENT", 3072)
    assert len(results) == 2 and len(results[0].values) == R1_EMBEDDING_DIMENSION
    assert results[0].metadata == {"provider": "google", "sdk": "google-genai", "model": "gemini-embedding-001",
                                   "task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": 3072,
                                   "token_count": 7}
    validate_vector(results[0].values, DEFAULT_EMBEDDING_PROFILE)


def test_a_vector_of_another_dimension_is_an_error_not_a_coercion():
    gemini, _ = provider(dimension=768)
    with pytest.raises(EmbeddingDimensionError, match="768-dimensional vector although 3072"):
        gemini.embed(["a"], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")


def test_inputs_that_could_be_truncated_are_refused_before_any_call():
    gemini, client = provider()
    with pytest.raises(EmbeddingRequestError, match="truncated"):
        gemini.embed(["x" * 7000], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")
    assert client.models.calls == []
    truncating, _ = provider(truncated=True)
    with pytest.raises(EmbeddingRequestError, match="truncated"):
        truncating.embed(["a"], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")


def test_large_requests_are_split_into_api_sized_batches():
    gemini, client = provider()
    results = gemini.embed([f"t{i}" for i in range(GEMINI_MAX_BATCH * 2 + 5)], profile=DEFAULT_EMBEDDING_PROFILE,
                           task="RETRIEVAL_DOCUMENT")
    assert [len(call.contents) for call in client.models.calls] == [100, 100, 5]
    assert len(results) == 205


@pytest.mark.parametrize("error, expected", [
    (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), EmbeddingRateLimited),
    (api_error(errors.ClientError, 400, "INVALID_ARGUMENT"), EmbeddingRequestError),
    (api_error(errors.ClientError, 403, "PERMISSION_DENIED"), EmbeddingRequestError),
    (api_error(errors.ServerError, 503, "UNAVAILABLE"), EmbeddingProviderUnavailable),
    (httpx.ReadTimeout("timed out"), EmbeddingProviderUnavailable),
    (httpx.ConnectError("refused"), EmbeddingProviderUnavailable),
])
def test_provider_failures_are_translated_without_leaking_details(error, expected):
    gemini, _ = provider(error=error)
    with pytest.raises(expected) as raised:
        gemini.embed(["secret source text"], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")
    assert "secret source text" not in str(raised.value) and "details withheld" not in str(raised.value)


def test_unexpected_errors_are_not_disguised():
    gemini, _ = provider(error=KeyError("bug"))
    with pytest.raises(KeyError):
        gemini.embed(["a"], profile=DEFAULT_EMBEDDING_PROFILE, task="RETRIEVAL_DOCUMENT")


def test_the_provider_closes_its_client():
    with GeminiEmbeddingProvider(client=FakeClient()) as gemini:
        client = gemini._client
    assert client.closed


def test_the_real_client_is_built_with_timeout_and_retries_without_calling_the_network():
    gemini = GeminiEmbeddingProvider(api_key="offline-test-key", timeout_seconds=12, retry_attempts=3)
    try:
        options = gemini._client._api_client._http_options
        assert options.timeout == 12000
        assert options.retry_options.attempts == 3 and 429 in options.retry_options.http_status_codes
    finally:
        gemini.close()


def test_the_profile_decides_the_requested_dimension():
    gemini, client = provider()
    custom = replace(DEFAULT_EMBEDDING_PROFILE, profile_version=9, provider_options={})
    gemini.embed(["a"], profile=custom, task=None)
    assert client.models.calls[0].config.output_dimensionality == custom.dimension == 3072
