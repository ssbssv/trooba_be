"""
Model-agnostic LLM client abstraction.

Business logic depends only on LLMClient. Providers (OpenAI, Gemini, Claude, …)
are swappable via settings without changing ChatService.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMMessage:
    """
    One chat turn.

    ``content`` may be a string or structured JSON (dict/list).
    Providers convert structured content at the HTTP boundary only.
    """

    role: str
    content: str | dict[str, Any] | list[Any]


@dataclass
class LLMResponse:
    content: str
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class LLMClient(ABC):
    """Provider-independent interface. Version 2 tool calling can extend this."""

    @abstractmethod
    def chat(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        """Send a chat completion request and return the assistant text."""

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate used for conversation summarization thresholds."""
        if not text:
            return 0
        # ~4 chars per token heuristic — provider-independent.
        return max(1, len(text) // 4)
