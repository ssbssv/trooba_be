"""Shared HTTP helpers for LLM provider adapters (stdlib only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class LLMProviderError(RuntimeError):
    """Raised when an upstream LLM API call fails."""


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **(headers or {}),
    }
    request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMProviderError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMProviderError(f"LLM connection error: {exc.reason}") from exc

    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise LLMProviderError(f"LLM returned invalid JSON: {raw[:240]}") from exc
