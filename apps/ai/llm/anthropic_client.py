"""Anthropic Claude Messages API provider."""
from __future__ import annotations

from typing import Any

from django.conf import settings

from .base import LLMClient, LLMMessage, LLMResponse
from .formatting import messages_to_anthropic_format
from .http import LLMProviderError, post_json


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or getattr(settings, "ANTHROPIC_API_KEY", "") or ""
        self.model = model or getattr(settings, "LLM_MODEL", "claude-sonnet-4-20250514")
        self.base_url = (
            base_url
            or getattr(settings, "ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        ).rstrip("/")

    def chat(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        if not self.api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY is not configured.")

        model = kwargs.get("model") or self.model
        system, chat = messages_to_anthropic_format(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": chat,
            "max_tokens": int(kwargs.get("max_tokens") or 4096),
        }
        if system:
            payload["system"] = system
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]

        data = post_json(
            f"{self.base_url}/v1/messages",
            payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": getattr(
                    settings, "ANTHROPIC_API_VERSION", "2023-06-01"
                ),
            },
            timeout=float(kwargs.get("timeout", 120)),
        )
        try:
            blocks = data.get("content") or []
            content = "".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (TypeError, AttributeError) as exc:
            raise LLMProviderError(f"Unexpected Anthropic response: {data}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(content=content.strip(), model=model, usage=usage, raw=data)
