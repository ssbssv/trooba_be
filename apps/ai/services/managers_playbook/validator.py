"""Validate and sanitize Manager's Playbook AI output."""
from __future__ import annotations

import re
from typing import Any

TIME_HORIZONS = {"immediate", "near_term", "medium_term", "long_term"}
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
ACTION_INTENTS = {
    "stabilize_primary_constraint",
    "control_queue",
    "reduce_non_value_added_mct",
    "rebalance_labor",
    "review_lot_policy",
    "review_routing",
    "validate_scenario",
    "monitor_emerging_constraint",
    "prevent_wip_growth",
    "improve_inspection_flow",
    "defer_capital_purchase",
    "defer_hiring",
    "escalate_capacity_if_validated",
}

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
    "will reduce by",
    "will save",
)

CAPITAL_MARKERS = (
    "buy another machine",
    "purchase another machine",
    "buy a new machine",
    "purchase a new machine",
    "add another machine",
    "buy additional machine",
    "purchase additional capacity",
)

HIRING_MARKERS = (
    "hire additional",
    "hire more",
    "add headcount",
    "recruit additional",
    "staffing increase",
    "hire operators",
)

FIRST_PERSON_MARKERS = (
    "i would",
    "i wouldn't",
    "i will",
    "next, i",
    "then, i",
    "finally, i",
    "i also",
)


def _clean_str(value: Any, *, max_len: int = 2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    # Strip leftover markdown / brace markers
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\{([^{}]+)\}", r"\1", text)
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
        text = re.sub(r"\b\d+(?:\.\d+)?\s*%", "a directional amount", text)
        text = re.sub(
            r"\b(guarantee|guaranteed|will save|will reduce by)\b",
            "should help",
            text,
            flags=re.IGNORECASE,
        )
    return _clean_str(text)


def _fit_words(text: str, *, min_words: int, max_words: int, fallback: str) -> str:
    cleaned = _strip_financial(text)
    words = [w for w in re.split(r"\s+", cleaned.strip()) if w]
    if len(words) < min_words:
        extra = [w for w in re.split(r"\s+", fallback.strip()) if w]
        words = (words + extra)[:max_words]
        if len(words) < min_words:
            pad = [
                "I",
                "would",
                "stabilize",
                "current",
                "constraints,",
                "improve",
                "flow",
                "with",
                "existing",
                "resources,",
                "then",
                "validate",
                "through",
                "another",
                "scenario",
                "run",
                "before",
                "escalating",
                "to",
                "capital",
                "or",
                "staffing",
                "changes",
                "when",
                "supported",
                "by",
                "evidence.",
            ]
            words = (words + pad)[:max(min_words, min(len(words) + len(pad), max_words))]
    if len(words) > max_words:
        words = words[:max_words]
        # Keep a terminal period when truncated mid-sentence
        if words and not words[-1].endswith((".", "!", "?")):
            words[-1] = words[-1].rstrip(",;:") + "."
    return " ".join(words)


def _is_first_person(title: str) -> bool:
    lower = title.lower().strip()
    return any(lower.startswith(m) or f" {m}" in lower for m in FIRST_PERSON_MARKERS)


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
    for action in input_payload.get("validated_recommended_actions") or []:
        if not isinstance(action, dict):
            continue
        area = action.get("impact_area")
        if isinstance(area, dict):
            for k in ("id", "name"):
                if area.get(k):
                    names.add(str(area[k]))
    names.add("factory")
    return names


def _ids(items: list[Any], key: str = "id") -> set[str]:
    out: set[str] = set()
    for item in items:
        if isinstance(item, dict) and item.get(key):
            out.add(str(item[key]))
    return out


def _metric_keys(input_payload: dict[str, Any]) -> set[str]:
    catalog = input_payload.get("metric_catalog") or {}
    keys = set(str(k) for k in catalog.keys())
    for collection in (
        "validated_problems",
        "validated_business_impacts",
        "validated_recommended_actions",
    ):
        for item in input_payload.get(collection) or []:
            if isinstance(item, dict):
                for k in item.get("evidence_keys") or []:
                    if k:
                        keys.add(str(k))
    return keys


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


def _map_intent(raw: Any, title: str, description: str) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if text in ACTION_INTENTS:
        return text
    blob = f"{title} {description}".lower()
    mapping = (
        ("monitor", "monitor_emerging_constraint"),
        ("queue", "control_queue"),
        ("wip", "prevent_wip_growth"),
        ("inspect", "improve_inspection_flow"),
        ("lot", "review_lot_policy"),
        ("rout", "review_routing"),
        ("labor", "rebalance_labor"),
        ("hire", "defer_hiring"),
        ("machine", "defer_capital_purchase"),
        ("capacity", "defer_capital_purchase"),
        ("validate", "validate_scenario"),
        ("re-run", "validate_scenario"),
        ("rerun", "validate_scenario"),
        ("waiting", "reduce_non_value_added_mct"),
        ("mct", "reduce_non_value_added_mct"),
        ("stabil", "stabilize_primary_constraint"),
    )
    for key, intent in mapping:
        if key in blob:
            return intent
    return "stabilize_primary_constraint"


def _has_unsupported_capital(text: str, input_payload: dict[str, Any]) -> bool:
    lower = text.lower()
    if not any(m in lower for m in CAPITAL_MARKERS):
        return False
    flex = (
        (input_payload.get("management_context") or {}).get("action_feasibility") or {}
    ).get("equipment_flexibility") or []
    return not any(isinstance(f, dict) and f.get("can_add_units") for f in flex)


def _has_unsupported_hiring(text: str, input_payload: dict[str, Any]) -> bool:
    lower = text.lower()
    if not any(m in lower for m in HIRING_MARKERS):
        return False
    # Hiring is never treated as freely available unless labor shortage evidence exists
    labor = (input_payload.get("simulation_evidence") or {}).get("labor") or []
    for lab in labor:
        if isinstance(lab, dict):
            util = float(lab.get("utilization") or lab.get("totalUtil") or 0)
            if util >= 95:
                return False
    return True


def validate_managers_playbook_payload(
    raw: dict[str, Any],
    *,
    input_payload: dict[str, Any],
    analysis_id: str,
) -> dict[str, Any]:
    problem_ids = _ids(list(input_payload.get("validated_problems") or []))
    impact_ids = _ids(list(input_payload.get("validated_business_impacts") or []))
    recommendation_ids = _ids(
        list(input_payload.get("validated_recommended_actions") or [])
    )
    metric_keys = _metric_keys(input_payload)
    entities = _known_entities(input_payload)

    overview_fallback = (
        "Based on the current constraints, I would first stabilize the areas creating "
        "the most waiting, then verify results before major capacity or staffing decisions."
    )
    overview = _fit_words(
        str(raw.get("overview") or ""),
        min_words=25,
        max_words=45,
        fallback=overview_fallback,
    )

    summary_fallback = (
        "These steps should reduce avoidable waiting, improve flow stability, "
        "and support better capacity decisions."
    )
    final_summary = _fit_words(
        str(raw.get("final_summary") or ""),
        min_words=15,
        max_words=30,
        fallback=summary_fallback,
    )

    actions_raw = raw.get("manager_actions")
    if not isinstance(actions_raw, list):
        actions_raw = []

    validated: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in actions_raw:
        if not isinstance(item, dict):
            continue

        title = _strip_financial(_clean_str(item.get("title"), max_len=140))
        description = _strip_financial(_clean_str(item.get("description"), max_len=500))
        if not title or not description:
            continue
        if not _is_first_person(title):
            title = f"I would {title[0].lower() + title[1:]}" if title else title
            if not _is_first_person(title):
                title = f"I would focus next on {title.rstrip('.')}."

        # Keep 1–2 sentences
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", description) if s.strip()]
        if len(sentences) > 2:
            description = " ".join(sentences[:2])
        elif not sentences:
            continue

        blob = f"{title} {description}"
        if _has_unsupported_capital(blob, input_payload):
            # Rewrite away from purchase language
            title = re.sub(
                r"(buy|purchase|add)\s+(another|a new|additional)\s+machine[^.]*",
                "stabilize existing capacity first",
                title,
                flags=re.IGNORECASE,
            )
            description = (
                "I would test lower-cost operational changes on the current constraint "
                "before considering any additional machine."
            )
            title = "I would stabilize existing capacity before buying another machine."
        if _has_unsupported_hiring(blob, input_payload):
            title = "I would use available labor more flexibly before hiring."
            description = (
                "I would review assignment and cross-coverage options with existing crews "
                "before considering additional hiring."
            )

        related_problems = [
            str(x)
            for x in (item.get("related_problem_ids") or [])
            if str(x) in problem_ids
        ]
        if not related_problems and problem_ids:
            related_problems = [next(iter(problem_ids))]

        related_recommendations = [
            str(x)
            for x in (item.get("related_recommendation_ids") or [])
            if str(x) in recommendation_ids
        ]
        if not related_recommendations and recommendation_ids:
            related_recommendations = [next(iter(recommendation_ids))]

        related_impacts = [
            str(x)
            for x in (item.get("related_business_impact_ids") or [])
            if str(x) in impact_ids
        ]
        if not related_impacts and impact_ids:
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
            str(x) for x in (item.get("evidence_keys") or []) if str(x) in metric_keys
        ]
        if not evidence_keys:
            for p in input_payload.get("validated_problems") or []:
                if isinstance(p, dict) and str(p.get("id")) in related_problems:
                    for k in p.get("evidence_keys") or []:
                        if str(k) in metric_keys:
                            evidence_keys.append(str(k))
            evidence_keys = list(dict.fromkeys(evidence_keys))[:6]

        area_raw = item.get("impact_area") if isinstance(item.get("impact_area"), dict) else {}
        area_id = _clean_str(area_raw.get("id") or area_raw.get("name") or "factory", max_len=80)
        area_name = _clean_str(area_raw.get("name") or area_id, max_len=80)
        if area_id not in entities and area_name not in entities:
            for p in input_payload.get("validated_problems") or []:
                if isinstance(p, dict) and str(p.get("id")) in related_problems:
                    area_id = _clean_str(p.get("area") or area_id, max_len=80)
                    area_name = area_id
                    break
        area_type = _map_area_type(area_raw.get("type"), area_id, entities)
        intent = _map_intent(item.get("action_intent"), title, description)

        horizon = str(item.get("time_horizon") or "near_term").lower()
        if horizon not in TIME_HORIZONS:
            horizon = "near_term"

        dedupe_key = (area_id.lower(), intent)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        validated.append(
            {
                "id": f"manager_action_{len(validated) + 1}",
                "rank": len(validated) + 1,
                "title": title,
                "description": description,
                "action_intent": intent,
                "impact_area": {
                    "id": area_id,
                    "name": area_name,
                    "type": area_type,
                },
                "related_problem_ids": related_problems,
                "related_recommendation_ids": related_recommendations,
                "related_business_impact_ids": related_impacts,
                "time_horizon": horizon,
                "confidence": _clamp(item.get("confidence"), 0.0, 1.0, 0.8),
                "evidence_keys": evidence_keys,
            }
        )
        if len(validated) >= 5:
            break

    # Re-number ranks
    for idx, action in enumerate(validated, start=1):
        action["rank"] = idx
        action["id"] = f"manager_action_{idx}"

    status = str(raw.get("playbook_status") or "").lower()
    if status not in STATUSES:
        if len(validated) >= 5:
            status = "complete"
        elif validated:
            status = "limited_actions"
        else:
            status = "unavailable"
    elif len(validated) >= 5:
        status = "complete"
    elif validated and status == "complete":
        status = "limited_actions"

    return {
        "analysis_id": analysis_id,
        "playbook_status": status,
        "overview": overview,
        "action_count": len(validated),
        "manager_actions": validated,
        "final_summary": final_summary,
    }
