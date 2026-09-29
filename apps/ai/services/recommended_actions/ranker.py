"""Rank and deduplicate recommended actions."""
from __future__ import annotations

from typing import Any


def _action_score(action: dict[str, Any]) -> float:
    if "action_score" in action:
        try:
            return float(action["action_score"])
        except (TypeError, ValueError):
            pass
    coverage = float(action.get("coverage_score") or 50)
    evidence = float(action.get("evidence_score") or 50)
    feasibility = float(action.get("feasibility_score") or 50)
    # Approximate weighted score from the spec
    nva = 70.0  # directional default — validated elsewhere
    queue = 65.0
    util = 60.0
    urgency = 70.0 if action.get("priority") == "high" else 50.0
    return (
        0.30 * coverage
        + 0.25 * nva
        + 0.15 * queue
        + 0.10 * util
        + 0.10 * feasibility
        + 0.05 * urgency
        + 0.05 * evidence
    )


def rank_recommended_actions(
    actions: list[dict[str, Any]],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    scored = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        clone = dict(action)
        clone["action_score"] = round(_action_score(clone), 2)
        scored.append(clone)

    scored.sort(key=lambda a: (-float(a.get("action_score") or 0), a.get("title") or ""))
    top = scored[:limit]

    priority_cycle = ["high", "medium", "low"]
    for idx, action in enumerate(top):
        action["priority_rank"] = idx + 1
        action["id"] = f"action_{idx + 1}"
        if action.get("priority") not in ("high", "medium", "low"):
            action["priority"] = priority_cycle[min(idx, 2)]
        if idx == 0:
            action["priority"] = "high"
        elif idx == 1 and action.get("priority") == "high":
            action["priority"] = "medium"
    return top
