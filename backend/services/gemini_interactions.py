"""
Gemini client helper that prefers the Interactions endpoint.

Why this exists:
  * Google now recommends the Interactions API for Gemini calls.
  * The currently pinned SDKs in this environment don't yet expose
    `client.interactions`, so we call the REST endpoint directly.
  * We keep a legacy fallback through `google.generativeai.GenerativeModel`
    so behavior stays stable even when the interactions endpoint is unavailable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urljoin

import requests

try:
    import google.generativeai as legacy_genai
except Exception:  # pragma: no cover
    legacy_genai = None


_INTERACTIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta2/interactions"


def _snake_to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def _to_api_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {_snake_to_camel(k): _to_api_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_api_dict(v) for v in value]
    return value


def _generation_config_dict(config: Any) -> dict:
    if config is None:
        return {}
    if isinstance(config, dict):
        return _to_api_dict(dict(config))
    if hasattr(config, "to_dict"):
        try:
            return _to_api_dict(config.to_dict())
        except Exception:
            pass
    if hasattr(config, "__dict__"):
        return _to_api_dict(
            {
                k: v
                for k, v in vars(config).items()
                if not k.startswith("_")
            }
        )
    return {}


def _extract_text(payload: dict) -> str:
    output = payload.get("outputText") or payload.get("output_text")
    if output:
        return str(output)

    parts = []
    for step in payload.get("steps", []) or []:
        if step.get("type") != "model_output":
            continue
        for content in step.get("content", []) or []:
            if content.get("type") == "text":
                text = content.get("text")
                if text:
                    parts.append(str(text))
    return "".join(parts)


def _build_response(payload: dict) -> "InteractionLikeResponse":
    text = _extract_text(payload)
    finish_reason = payload.get("finishReason") or payload.get("status")
    candidate = SimpleNamespace(finish_reason=finish_reason)
    usage = payload.get("usageMetadata") or payload.get("usage_metadata")
    usage_metadata = None
    if isinstance(usage, dict):
        usage_metadata = SimpleNamespace(**{
            "candidates_token_count": usage.get("candidatesTokenCount")
            if usage.get("candidatesTokenCount") is not None
            else usage.get("candidates_token_count"),
        })
    return InteractionLikeResponse(text=text, candidates=[candidate], usage_metadata=usage_metadata)


@dataclass
class InteractionLikeResponse:
    text: str
    candidates: list[Any]
    usage_metadata: Any = None


class InteractionsModel:
    """
    A tiny adapter used where the repo currently expects a
    `generate_content(prompt, request_options=?, generation_config=?)` object.
    """

    def __init__(
        self,
        model: str,
        generation_config: Any | None = None,
        legacy_model_factory: Callable[[], Any] | None = None,
    ):
        self._model = model
        self._generation_config = generation_config
        self._legacy_model = None
        self._legacy_model_factory = legacy_model_factory

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            self._api_key = None
        else:
            self._api_key = api_key

    def _legacy(self):
        if self._legacy_model is None:
            if self._legacy_model_factory is None:
                return None
            self._legacy_model = self._legacy_model_factory()
        return self._legacy_model

    def _normalize_timeout(self, request_options: dict | None) -> float:
        if not request_options:
            return 30.0
        try:
            timeout = request_options.get("timeout", 30.0)
            if timeout is None:
                return 30.0
            return float(timeout)
        except Exception:
            return 30.0

    def _post_interactions(self, input_text: str, request_options: dict | None, config: Any) -> dict:
        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY missing for interactions call")

        payload = {
            "model": self._model,
            "input": input_text,
        }
        api_config = _generation_config_dict(config or self._generation_config)
        if api_config:
            payload["config"] = api_config

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        resp = requests.post(
            _INTERACTIONS_ENDPOINT,
            headers=headers,
            data=json.dumps(payload),
            timeout=self._normalize_timeout(request_options),
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"INTERACTIONS ERROR {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def generate_content(
        self,
        prompt: str,
        request_options: dict | None = None,
        generation_config: Any | None = None,
        **_kwargs,
    ) -> InteractionLikeResponse:
        cfg = generation_config or self._generation_config
        try:
            payload = self._post_interactions(prompt, request_options, cfg)
            return _build_response(payload)
        except Exception as e:
            legacy_model = self._legacy()
            if legacy_model is None:
                raise
            return legacy_model.generate_content(
                prompt,
                request_options=request_options,
                generation_config=cfg,
            )
