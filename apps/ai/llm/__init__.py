"""
LLM client factory — dependency injection entry point.

Swap providers via Django settings without touching ChatService.
"""
from __future__ import annotations

from django.conf import settings

from .anthropic_client import AnthropicLLMClient
from .base import LLMClient
from .gemini_client import GeminiLLMClient
from .mock_client import MockLLMClient
from .openai_client import OpenAILLMClient

_PROVIDER_MAP: dict[str, type[LLMClient]] = {
    "openai": OpenAILLMClient,
    "gemini": GeminiLLMClient,
    "google": GeminiLLMClient,
    "anthropic": AnthropicLLMClient,
    "claude": AnthropicLLMClient,
    "mock": MockLLMClient,
}


def get_llm_client(provider: str | None = None) -> LLMClient:
    """
    Resolve the configured LLMClient implementation.

    Settings:
      LLM_PROVIDER — openai | gemini | anthropic | mock
      LLM_MODEL — model id passed to the provider
    """
    name = (provider or getattr(settings, "LLM_PROVIDER", "mock") or "mock").strip().lower()
    cls = _PROVIDER_MAP.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{name}'. "
            f"Supported: {', '.join(sorted(_PROVIDER_MAP))}"
        )
    return cls()


__all__ = [
    "LLMClient",
    "get_llm_client",
    "OpenAILLMClient",
    "GeminiLLMClient",
    "AnthropicLLMClient",
    "MockLLMClient",
]
