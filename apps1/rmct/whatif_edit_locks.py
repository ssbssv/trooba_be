"""Org-wide what-if scenario edit locks (one editor at a time per scenario)."""

from __future__ import annotations

import time
from typing import Optional

# key -> (user_id, display_name, expires_at)
_MEM_WHATIF_EDIT: dict[str, tuple[str, str, float]] = {}

WHATIF_EDIT_TTL_SECONDS = 4 * 60 * 60


def whatif_edit_lock_key(*, org_id: str, model_id: str, scenario_id: str) -> str:
    return f"whatifedit:{org_id}:{model_id}:{scenario_id}"


def _purge_expired(now: float | None = None) -> None:
    t = now if now is not None else time.time()
    expired = [k for k, v in _MEM_WHATIF_EDIT.items() if v[2] <= t]
    for k in expired:
        _MEM_WHATIF_EDIT.pop(k, None)


def mem_try_whatif_edit(
    *, key: str, user_id: str, name: str, ttl_seconds: int = WHATIF_EDIT_TTL_SECONDS
) -> bool:
    now = time.time()
    _purge_expired(now)
    current = _MEM_WHATIF_EDIT.get(key)
    if current and current[2] > now and current[0] != str(user_id):
        return False
    _MEM_WHATIF_EDIT[key] = (str(user_id), name or "Someone", now + ttl_seconds)
    return True


def mem_get_whatif_edit(key: str) -> Optional[tuple[str, str]]:
    now = time.time()
    _purge_expired(now)
    current = _MEM_WHATIF_EDIT.get(key)
    if not current or current[2] <= now:
        _MEM_WHATIF_EDIT.pop(key, None)
        return None
    return current[0], current[1]


def mem_release_whatif_edit(*, key: str, user_id: str) -> bool:
    current = _MEM_WHATIF_EDIT.get(key)
    if current and current[0] == str(user_id):
        _MEM_WHATIF_EDIT.pop(key, None)
        return True
    return False


def mem_release_user_whatif_edits(*, user_id: str) -> list[str]:
    uid = str(user_id)
    keys = [k for k, v in _MEM_WHATIF_EDIT.items() if v[0] == uid]
    for k in keys:
        _MEM_WHATIF_EDIT.pop(k, None)
    return keys


def whatif_edit_payload_from_key(key: str, *, active: bool) -> dict | None:
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != "whatifedit":
        return None
    model_id = parts[2]
    scenario_id = parts[3]
    return {
        "type": "whatif_edit_changed",
        "model_id": model_id,
        "scenario_id": scenario_id,
        "active": active,
    }
