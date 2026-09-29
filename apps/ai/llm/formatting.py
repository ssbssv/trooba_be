"""Helpers for normalizing LLM message content across providers."""
from __future__ import annotations

import json
from typing import Any

from .base import LLMMessage


def content_as_json_value(content: str | dict[str, Any] | list[Any]) -> Any:
    """Keep structured content as JSON values; leave strings untouched."""
    return content


def content_as_text(content: str | dict[str, Any] | list[Any]) -> str:
    """
    Convert content for APIs that only accept text parts.

    Structured objects are emitted as compact JSON text at the provider boundary
    only — ChatService always builds structured messages.
    """
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def messages_to_openai_format(messages: list[LLMMessage]) -> list[dict[str, str]]:
    return [
        {"role": m.role, "content": content_as_text(m.content)}
        for m in messages
    ]


def messages_to_anthropic_format(
    messages: list[LLMMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    for m in messages:
        text = content_as_text(m.content)
        if m.role == "system":
            system_parts.append(text)
            continue
        role = "assistant" if m.role == "assistant" else "user"
        chat.append({"role": role, "content": [{"type": "text", "text": text}]})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def messages_to_gemini_format(
    messages: list[LLMMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for m in messages:
        text = content_as_text(m.content)
        if m.role == "system":
            system_parts.append(text)
            continue
        role = "model" if m.role == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, contents
