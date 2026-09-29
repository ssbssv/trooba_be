"""Deduplicate and rank factory-analysis problem candidates."""
from __future__ import annotations

import re
from typing import Any


SEVERITIES = frozenset({"high", "medium", "low"})


def _norm_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text


def _entity_set(problem: dict[str, Any]) -> frozenset[str]:
    parts: list[str] = []
    for key in ("affected_resources", "affected_products", "affected_operations"):
        raw = problem.get(key)
        if isinstance(raw, list):
            parts.extend(_norm_text(x) for x in raw if str(x).strip())
    area = _norm_text(problem.get("area"))
    if area:
        parts.append(area)
    category = _norm_text(problem.get("category"))
    if category:
        parts.append(category)
    return frozenset(p for p in parts if p)


def _score(problem: dict[str, Any]) -> float:
    try:
        return float(problem.get("problem_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def deduplicate_problems(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Collapse symptom duplicates that share the same causal chain / entities.
    Keep the higher-scoring problem when duplicates collide.
    """
    kept: list[dict[str, Any]] = []
    for problem in sorted(problems, key=_score, reverse=True):
        entities = _entity_set(problem)
        title = _norm_text(problem.get("problem"))
        root = _norm_text(problem.get("root_cause"))
        duplicate = False
        for existing in kept:
            existing_entities = _entity_set(existing)
            overlap = len(entities & existing_entities)
            title_sim = (
                title
                and _norm_text(existing.get("problem"))
                and (
                    title in _norm_text(existing.get("problem"))
                    or _norm_text(existing.get("problem")) in title
                )
            )
            root_sim = (
                root
                and _norm_text(existing.get("root_cause"))
                and (
                    root in _norm_text(existing.get("root_cause"))
                    or _norm_text(existing.get("root_cause")) in root
                )
            )
            same_category = _norm_text(problem.get("category")) == _norm_text(
                existing.get("category")
            )
            if (overlap >= 2 and same_category) or (overlap >= 1 and (title_sim or root_sim)):
                duplicate = True
                break
        if not duplicate:
            kept.append(problem)
    return kept


def rank_top_problems(problems: list[dict[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    cleaned = deduplicate_problems(problems)
    ranked = sorted(cleaned, key=_score, reverse=True)[:limit]
    for index, problem in enumerate(ranked, start=1):
        problem["rank"] = index
        problem["id"] = f"problem_{index}"
        score = _score(problem)
        problem["problem_score"] = max(0.0, min(100.0, score))
        severity = str(problem.get("severity") or "medium").strip().lower()
        problem["severity"] = severity if severity in SEVERITIES else "medium"
    return ranked
