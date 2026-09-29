"""Google Gemini provider (Generative Language API)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.conf import settings

from .base import LLMClient, LLMMessage, LLMResponse
from .formatting import messages_to_gemini_format
from .http import LLMProviderError, post_json


class GeminiLLMClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or getattr(settings, "GEMINI_API_KEY", "") or ""
        self.model = model or getattr(settings, "LLM_MODEL", "gemini-2.0-flash")
        self.base_url = (
            base_url
            or getattr(
                settings,
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            )
        ).rstrip("/")

    def chat(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        if not self.api_key:
            raise LLMProviderError("GEMINI_API_KEY is not configured.")

        model = kwargs.get("model") or self.model
        system, contents = messages_to_gemini_format(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        generation_config: dict[str, Any] = {}
        if kwargs.get("temperature") is not None:
            generation_config["temperature"] = kwargs["temperature"]
        if kwargs.get("max_tokens") is not None:
            generation_config["maxOutputTokens"] = kwargs["max_tokens"]
        if generation_config:
            payload["generationConfig"] = generation_config

        query = urlencode({"key": self.api_key})
        url = f"{self.base_url}/models/{model}:generateContent?{query}"
        data = post_json(url, payload, timeout=float(kwargs.get("timeout", 120)))

        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(f"Unexpected Gemini response: {data}") from exc

        usage = data.get("usageMetadata") or {}
        return LLMResponse(content=content.strip(), model=model, usage=usage, raw=data)
