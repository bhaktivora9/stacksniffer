"""Text-generation providers for community summaries and answers.

A provider takes a system instruction, a prompt and a JSON schema and returns the parsed JSON
object plus token usage. Errors are raised as ``GenerationError`` subclasses whose messages carry
the status, never the prompt (which contains repository source) or credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

# Checked against the API on 2026-10-28: gemini-2.5-flash is still listed but answers 404.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
_RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504)


class GenerationError(RuntimeError):
    failure_code = "GENERATION_FAILED"


class GenerationRateLimited(GenerationError):
    failure_code = "GENERATION_RATE_LIMITED"


class GenerationUnavailable(GenerationError):
    failure_code = "GENERATION_PROVIDER_UNAVAILABLE"


class GenerationRejected(GenerationError):
    failure_code = "GENERATION_REQUEST_REJECTED"


@dataclass(frozen=True)
class Generation:
    data: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict = field(default_factory=dict)


class TextProvider(Protocol):
    name: str
    model: str

    def generate_json(self, system: str, prompt: str, schema: dict) -> Generation:
        ...

    @property
    def profile(self) -> str:
        ...


class GeminiTextProvider:
    """Gemini through the google-genai SDK, with JSON output constrained by a response schema."""

    name = "google"

    def __init__(self, api_key: str | None = None, *, model: str | None = None, client: Any = None,
                 timeout_seconds: float = 120.0, retry_attempts: int = 4):
        self.model = model or os.getenv("ANALYSIS_LLM_MODEL") or DEFAULT_GEMINI_MODEL
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
                http_options=types.HttpOptions(
                    timeout=int(timeout_seconds * 1000),
                    retry_options=types.HttpRetryOptions(
                        attempts=retry_attempts, initial_delay=1.0, max_delay=30.0, exp_base=2.0, jitter=1.0,
                        http_status_codes=list(_RETRYABLE_STATUS)),
                ))
        self._client = client

    @property
    def profile(self) -> str:
        return f"{self.name}/{self.model}"

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def generate_json(self, system: str, prompt: str, schema: dict) -> Generation:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system, temperature=0.0, response_mime_type="application/json",
            response_json_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        try:
            response = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            detail = f"{code} {getattr(exc, 'status', '') or ''}".strip()
            if code == 429:
                raise GenerationRateLimited(f"rate limited ({detail})") from exc
            if isinstance(code, int) and 400 <= code < 500 and code != 408:
                raise GenerationRejected(f"request rejected ({detail})") from exc
            raise GenerationUnavailable(f"provider unavailable ({detail})") from exc
        except Exception as exc:
            if type(exc).__module__.startswith(("httpx", "httpcore", "requests", "urllib3")):
                raise GenerationUnavailable(f"{type(exc).__name__} calling {self.model}") from exc
            raise
        text = response.text or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GenerationError(f"{self.model} returned {len(text)} characters that are not JSON") from exc
        if not isinstance(data, dict):
            raise GenerationError(f"{self.model} returned JSON that is not an object")
        usage = response.usage_metadata
        return Generation(data, getattr(usage, "prompt_token_count", 0) or 0,
                          getattr(usage, "candidates_token_count", 0) or 0, {"model": self.model})


class OfflineTextProvider:
    """Deterministic provider for tests and keyless local runs.

    It writes templated text from the references it is given, so every downstream path (citation
    validation, persistence, the API) runs without a network. It carries no understanding.
    """

    name = "offline"
    model = "stacksniffer-offline-template"

    def __init__(self):
        self.calls: list[str] = []

    @property
    def profile(self) -> str:
        return f"{self.name}/{self.model}"

    def close(self) -> None:
        pass

    def generate_json(self, system: str, prompt: str, schema: dict) -> Generation:
        self.calls.append(prompt)
        refs = list(dict.fromkeys(re.findall(r"\[((?:E|C)\d+)\]", prompt)))[:3]
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        cited = " ".join(f"[{ref}]" for ref in refs)
        properties = schema.get("properties", {})
        if "answer" in properties:
            data = {"answer": f"Offline answer {digest} drawn from the retrieved context {cited}.".strip(),
                    "insufficient_evidence": not refs}
        else:
            data = {"title": f"Component {digest}", "summary": f"Offline summary {digest}.",
                    "key_points": [{"text": f"Groups the listed code {cited}.", "references": refs}] if refs else []}
        return Generation(data, len(prompt) // 4, 32, {"model": self.model})
