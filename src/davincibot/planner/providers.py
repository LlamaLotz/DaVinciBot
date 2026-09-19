from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from davincibot.models import ProviderKind


class ProviderError(RuntimeError):
    pass


class PlannerProvider(ABC):
    @abstractmethod
    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("provider response did not contain JSON content")
    value = value.strip()
    if value.startswith("```"):
        try:
            value = value.split("\n", 1)[1].rsplit("```", 1)[0]
        except IndexError as error:
            raise ProviderError("provider returned an incomplete JSON code fence") from error
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ProviderError(f"provider returned invalid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ProviderError("provider response must be a JSON object")
    return payload


def _post(*args, **kwargs):
    try:
        return httpx.post(*args, **kwargs)
    except httpx.HTTPError as error:
        raise ProviderError(f"planner network request failed: {error}") from error


class OpenAIChatProvider(PlannerProvider):
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = _post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "davincibot_edit_plan",
                        "strict": True,
                        "schema": schema,
                    },
                },
            },
            timeout=90,
        )
        if response.is_error:
            raise ProviderError(f"OpenAI-compatible provider error {response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ProviderError(
                "OpenAI-compatible provider returned an unexpected response"
            ) from error
        return _object(content)


class AnthropicProvider(PlannerProvider):
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = _post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 8192,
                "temperature": 0,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"{user}\nReturn only JSON matching this schema:\n{json.dumps(schema)}"
                        ),
                    }
                ],
            },
            timeout=90,
        )
        if response.is_error:
            raise ProviderError(f"Anthropic provider error {response.status_code}")
        try:
            blocks = response.json()["content"]
            content = next(block["text"] for block in blocks if block.get("type") == "text")
        except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as error:
            raise ProviderError("Anthropic returned an unexpected response") from error
        return _object(content)


class GeminiProvider(PlannerProvider):
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            },
            timeout=90,
        )
        if response.is_error:
            raise ProviderError(f"Gemini provider error {response.status_code}")
        try:
            content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ProviderError("Gemini returned an unexpected response") from error
        return _object(content)


def make_provider(
    kind: ProviderKind, api_key: str, model: str, base_url: str | None = None
) -> PlannerProvider:
    if kind in {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE}:
        return OpenAIChatProvider(
            api_key,
            model,
            base_url or "https://api.openai.com/v1",
        )
    if kind is ProviderKind.ANTHROPIC:
        return AnthropicProvider(api_key, model)
    if kind is ProviderKind.GEMINI:
        return GeminiProvider(api_key, model)
    raise ValueError(f"{kind.value} does not use a cloud provider")
