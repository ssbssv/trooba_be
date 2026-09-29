"""OpenAI Chat Completions provider."""
from __future__ import annotations

from typing import Any

from django.conf import settings

from .base import LLMClient, LLMMessage, LLMResponse
from .formatting import messages_to_openai_format
from .http import LLMProviderError, post_json


class OpenAILLMClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or getattr(settings, "OPENAI_API_KEY", "") or ""
        self.model = model or getattr(settings, "LLM_MODEL", "gpt-4o-mini")
        self.base_url = (
            base_url
            or getattr(settings, "OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")

    def chat(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        if not self.api_key:
            raise LLMProviderError("OPENAI_API_KEY is not configured.")

        model = kwargs.get("model") or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages_to_openai_format(messages),
        }
        temperature = kwargs.get("temperature")
        if temperature is not None:
            payload["temperature"] = temperature
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        data = post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=float(kwargs.get("timeout", 120)),
        )
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(f"Unexpected OpenAI response: {data}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(content=content.strip(), model=model, usage=usage, raw=data)
