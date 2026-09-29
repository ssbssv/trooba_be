"""Validate and sanitize Recommended Actions AI output."""
from __future__ import annotations

import re
from typing import Any

from apps.ai.services.recommended_actions.input_builder import ALLOWED_ACTION_TYPES

PRIORITIES = {"high", "medium", "low"}
IMPACT_LEVELS = {"high", "medium", "low"}
TIME_HORIZONS = {"immediate", "near_term", "medium_term", "long_term"}
DIRECTIONS = {"decrease", "increase", "improve", "stabilize"}
AREA_TYPES = {
    "work_center",
    "equipment",
    "labor",
    "department",
    "operation",
    "product",
    "product_flow",
    "routing",
    "factory",
}
STATUSES = {"complete", "limited_actions", "unavailable"}

FINANCIAL_MARKERS = (
    "roi",
    "payback",
    "$",
    "usd",
    "cost savings",
    "save $",
    "million",
    "guarantee",
    "guaranteed",
)


def _clean_str(value: Any, *, max_len: int = 2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _strip_financial(text: str) -> str:
    lower = text.lower()
    if any(m in lower for m in FINANCIAL_MARKERS):
        text = re.sub(r"\$[\d,]+(?:\.\d+)?", "", text)
        text = re.sub(
            r"\b\d+(?:\.\d+)?\s*%",
            "a directional amount",
            text,
        )
    return _clean_str(text)


def _known_entities(input_payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    evidence = input_payload.get("simulation_evidence") or {}
    for key, id_keys in (
        ("products", ("product_id", "product_name", "display_name")),
        ("equipment", ("equipment_id", "equipment_name", "display_name")),
        ("labor", ("labor_id", "labor_name", "display_name")),
        ("operations", ("operation_id", "operation_name")),
    ):
        for row in evidence.get(key) or []:
            if not isinstance(row, dict):
                continue
            for ik in id_keys:
                val = str(row.get(ik) or "").strip()
                if val:
                    names.add(val)
    for p in input_payload.get("validated_problems") or []:
        if not isinstance(p, dict):
            continue
        for field in ("area", "affected_products", "affected_operations", "affected_resources"):
            val = p.get(field)
            if isinstance(val, list):
                for item in val:
                    if item:
                        names.add(str(item))
            elif val:
                names.add(str(val))
    names.add("factory")
    return names


def _problem_ids(input_payload: dict[str, Any]) -> set[str]:
    return {
        str(p["id"])
        for p in (input_payload.get("validated_problems") or [])
        if isinstance(p, dict) and p.get("id")
    }


def _impact_ids(input_payload: dict[str, Any]) -> set[str]:
    return {
        str(i["id"])
        for i in (input_payload.get("validated_business_impacts") or [])
        if isinstance(i, dict) and i.get("id")
    }


def _metric_keys(input_payload: dict[str, Any]) -> set[str]:
    catalog = input_payload.get("metric_catalog") or {}
    keys = set(str(k) for k in catalog.keys())
    # Also allow evidence keys from problems/impacts even if catalog trimmed
    for p in input_payload.get("validated_problems") or []:
        if isinstance(p, dict):
            for k in p.get("evidence_keys") or []:
                if k:
                    keys.add(str(k))
    for i in input_payload.get("validated_business_impacts") or []:
        if isinstance(i, dict):
            for k in i.get("evidence_keys") or []:
                if k:
                    keys.add(str(k))
    return keys


def _map_action_type(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if text in ALLOWED_ACTION_TYPES:
        return text
    aliases = {
        "setup_reduction": "reduce_setup_time",
        "lot_size": "adjust_lot_size",
        "transfer_batch": "adjust_transfer_batch",
        "capacity": "increase_capacity",
        "routing": "review_routing",
        "workload": "rebalance_workload",
        "inspection": "improve_inspection_flow",
        "monitor": "monitor_emerging_constraint",
    }
    for key, mapped in aliases.items():
        if key in text:
            return mapped
    return "rebalance_workload"


def _map_area_type(raw: Any, area_id: str, entities: set[str]) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_")
    if text in AREA_TYPES:
        return text
    aid = area_id.lower()
    if "inspect" in aid:
        return "department"
    if any(x in aid for x in ("labor", "operator", "crew")):
        return "labor"
    if area_id in entities:
        return "work_center"
    return "factory"


def _direction(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in ("down", "reduce", "reduction", "lower"):
        return "decrease"
    if text in ("up", "raise", "higher"):
        return "increase"
    if text in DIRECTIONS:
        return text
    return "improve"


def validate_recommended_actions_payload(
    raw: dict[str, Any],
    *,
    input_payload: dict[str, Any],
    analysis_id: str,
) -> dict[str, Any]:
    problem_ids = _problem_ids(input_payload)
    impact_ids = _impact_ids(input_payload)
    metric_keys = _metric_keys(input_payload)
    entities = _known_entities(input_payload)
    catalog = input_payload.get("metric_catalog") or {}

    actions_raw = raw.get("recommended_actions")
    if not isinstance(actions_raw, list):
        actions_raw = []

    validated: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, tuple[str, ...]]] = set()

    for item in actions_raw:
        if not isinstance(item, dict):
            continue

        title = _strip_financial(_clean_str(item.get("title"), max_len=120))
        summary = _strip_financial(_clean_str(item.get("summary"), max_len=600))
        why = _strip_financial(_clean_str(item.get("why"), max_len=800))
        if not title or not summary:
            continue

        related_problems = [
            str(x)
            for x in (item.get("related_problem_ids") or [])
            if str(x) in problem_ids
        ]
        if not related_problems and problem_ids:
            related_problems = [next(iter(problem_ids))]

        related_impacts = [
            str(x)
            for x in (item.get("related_business_impact_ids") or [])
            if str(x) in impact_ids
        ]
        if not related_impacts and impact_ids:
            # Prefer impacts linked to the related problems
            for impact in input_payload.get("validated_business_impacts") or []:
                if not isinstance(impact, dict):
                    continue
                linked = set(impact.get("related_problem_ids") or [])
                if linked.intersection(related_problems):
                    related_impacts.append(str(impact["id"]))
            related_impacts = related_impacts[:3] or (
                [next(iter(impact_ids))] if impact_ids else []
            )

        evidence_keys = [
            str(x)
            for x in (item.get("evidence_keys") or [])
            if str(x) in metric_keys
        ]
        if not evidence_keys:
            # Pull from related problems
            for p in input_payload.get("validated_problems") or []:
                if isinstance(p, dict) and str(p.get("id")) in related_problems:
                    for k in p.get("evidence_keys") or []:
                        if str(k) in metric_keys:
                            evidence_keys.append(str(k))
            evidence_keys = list(dict.fromkeys(evidence_keys))[:6]

        action_type = _map_action_type(item.get("action_type"))
        # Block unsupported capacity addition unless explicitly allowed
        if action_type == "increase_capacity":
            flex = (input_payload.get("available_action_context") or {}).get(
                "equipment_flexibility"
            ) or []
            can_add = any(isinstance(f, dict) and f.get("can_add_units") for f in flex)
            if not can_add:
                action_type = "rebalance_workload"

        if action_type == "use_alternative_resource":
            flex = (input_payload.get("available_action_context") or {}).get(
                "equipment_flexibility"
            ) or []
            has_alt = any(
                isinstance(f, dict) and (f.get("alternative_equipment") or [])
                for f in flex
            )
            if not has_alt:
                action_type = "rebalance_workload"

        if action_type == "cross_train_labor":
            flex = (input_payload.get("available_action_context") or {}).get(
                "labor_flexibility"
            ) or []
            has_cross = any(
                isinstance(f, dict) and (f.get("cross_trained_labor") or [])
                for f in flex
            )
            if not has_cross:
                action_type = "change_labor_assignment"

        # Reject run-time reduction language without evidence
        lower_blob = f"{title} {summary} {why}".lower()
        if "run time" in lower_blob or "runtime" in lower_blob:
            if "abnormal" not in lower_blob and "process performance" not in lower_blob:
                continue

        area_raw = item.get("impact_area") if isinstance(item.get("impact_area"), dict) else {}
        area_id = _clean_str(area_raw.get("id") or area_raw.get("name") or "factory", max_len=80)
        area_name = _clean_str(area_raw.get("name") or area_id, max_len=80)
        if area_id not in entities and area_name not in entities:
            # Prefer first related problem area
            for p in input_payload.get("validated_problems") or []:
                if isinstance(p, dict) and str(p.get("id")) in related_problems:
                    area_id = _clean_str(p.get("area") or area_id, max_len=80)
                    area_name = area_id
                    break
        area_type = _map_area_type(area_raw.get("type"), area_id, entities)

        steps_raw = item.get("implementation_steps")
        steps: list[dict[str, Any]] = []
        if isinstance(steps_raw, list):
            for idx, step in enumerate(steps_raw[:4], start=1):
                if not isinstance(step, dict):
                    continue
                stitle = _strip_financial(_clean_str(step.get("title"), max_len=120))
                sdesc = _strip_financial(_clean_str(step.get("description"), max_len=400))
                if not stitle:
                    continue
                steps.append(
                    {
                        "step": idx,
                        "title": stitle,
                        "description": sdesc or stitle,
                    }
                )
        if len(steps) < 2:
            steps = [
                {
                    "step": 1,
                    "title": "Review the validated constraint",
                    "description": (
                        f"Confirm the current operating pattern around {area_name} "
                        "using the supplied simulation evidence."
                    ),
                },
                {
                    "step": 2,
                    "title": "Test an operational change",
                    "description": (
                        "Pilot a feasible adjustment from the allowed action types, "
                        "then re-run the scenario to validate directional improvement."
                    ),
                },
            ]

        expected_impacts: list[dict[str, Any]] = []
        for ei in item.get("expected_impacts") or []:
            if not isinstance(ei, dict):
                continue
            mk = ei.get("metric_key")
            mk_str = str(mk) if mk not in (None, "", "null") else None
            if mk_str and mk_str not in metric_keys:
                mk_str = None
            label = _clean_str(
                ei.get("display_label") or ei.get("kpi") or "Flow",
                max_len=80,
            )
            if mk_str and isinstance(catalog.get(mk_str), dict):
                label = _clean_str(catalog[mk_str].get("label") or label, max_len=80)
            expected_impacts.append(
                {
                    "kpi": _clean_str(ei.get("kpi") or label, max_len=80),
                    "direction": _direction(ei.get("direction")),
                    "display_label": label,
                    "metric_key": mk_str,
                }
            )
        if not expected_impacts:
            expected_impacts = [
                {
                    "kpi": "Manufacturing Cycle Time",
                    "direction": "decrease",
                    "display_label": "MCT",
                    "metric_key": None,
                },
                {
                    "kpi": "Queue",
                    "direction": "decrease",
                    "display_label": "Queue",
                    "metric_key": None,
                },
                {
                    "kpi": "Flow Efficiency",
                    "direction": "improve",
                    "display_label": "Flow Efficiency",
                    "metric_key": None,
                },
            ]

        success_indicators: list[dict[str, Any]] = []
        for si in item.get("success_indicators") or []:
            if not isinstance(si, dict):
                continue
            mk = str(si.get("metric_key") or "")
            if mk not in metric_keys:
                continue
            label = _clean_str(si.get("label") or mk, max_len=80)
            if isinstance(catalog.get(mk), dict):
                label = _clean_str(catalog[mk].get("label") or label, max_len=80)
            success_indicators.append(
                {
                    "label": label,
                    "metric_key": mk,
                    "desired_direction": _direction(si.get("desired_direction")),
                }
            )

        priority = str(item.get("priority") or "medium").lower()
        if priority not in PRIORITIES:
            priority = "medium"
        impact_level = str(item.get("expected_impact_level") or priority).lower()
        if impact_level not in IMPACT_LEVELS:
            impact_level = priority
        horizon = str(item.get("time_horizon") or "near_term").lower()
        if horizon not in TIME_HORIZONS:
            horizon = "near_term"

        dedupe_key = (
            area_id.lower(),
            action_type,
            tuple(sorted(related_problems)),
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        validated.append(
            {
                "id": f"action_{len(validated) + 1}",
                "priority_rank": len(validated) + 1,
                "priority": priority,
                "expected_impact_level": impact_level,
                "title": title,
                "summary": summary,
                "why": why or summary,
                "action_type": action_type,
                "impact_area": {
                    "id": area_id,
                    "name": area_name,
                    "type": area_type,
                },
                "related_problem_ids": related_problems,
                "related_business_impact_ids": related_impacts,
                "implementation_steps": steps,
                "expected_impacts": expected_impacts[:5],
                "success_indicators": success_indicators[:5],
                "time_horizon": horizon,
                "confidence": _clamp(item.get("confidence"), 0.0, 1.0, 0.75),
                "evidence_keys": evidence_keys,
                # backend-only scoring hints (stripped later)
                "coverage_score": min(100.0, 40.0 + 20.0 * len(related_problems)),
                "feasibility_score": 85.0 if action_type != "increase_capacity" else 55.0,
                "evidence_score": min(100.0, 50.0 + 10.0 * len(evidence_keys)),
            }
        )

        if len(validated) >= 3:
            break

    status = str(raw.get("recommendation_status") or "").lower()
    if status not in STATUSES:
        if len(validated) >= 3:
            status = "complete"
        elif validated:
            status = "limited_actions"
        else:
            status = "unavailable"

    return {
        "analysis_id": analysis_id,
        "recommendation_status": status,
        "action_count": len(validated),
        "recommended_actions": validated,
    }


def strip_internal_action_scores(payload: dict[str, Any]) -> dict[str, Any]:
    actions = []
    for action in payload.get("recommended_actions") or []:
        if not isinstance(action, dict):
            continue
        clone = dict(action)
        for key in ("action_score", "coverage_score", "feasibility_score", "evidence_score"):
            clone.pop(key, None)
        actions.append(clone)
    out = dict(payload)
    out["recommended_actions"] = actions
    out["action_count"] = len(actions)
    return out
