"""Validate AI factory-analysis JSON and rewrite metrics from metric_catalog."""
from __future__ import annotations

import json
import re
from typing import Any

from .ranker import rank_top_problems

SEVERITIES = frozenset({"high", "medium", "low"})
TIME_HORIZONS = frozenset({"immediate", "near_term", "medium_term", "long_term"})

RECOMMENDATION_MARKERS = (
    "you should",
    "we recommend",
    "recommended action",
    "next step",
    "implement",
    "hire more",
    "add another machine",
    "buy ",
)

FINANCIAL_MARKERS = (
    "revenue",
    "$",
    "usd",
    "profit",
    "penalty",
    "lost sales",
    "customer churn",
)


def extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty AI response")

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Could not parse AI JSON response")


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _clean_str(value: Any, *, max_len: int = 2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _strip_bad_claims(text: str) -> str:
    lower = text.lower()
    if any(m in lower for m in FINANCIAL_MARKERS):
        # Soften unsupported financial phrasing without inventing numbers.
        text = re.sub(
            r"\$[\d,]+(?:\.\d+)?",
            "an unsupported financial amount",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _looks_like_recommendation(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in RECOMMENDATION_MARKERS)


def _rewrite_metric(
    metric: dict[str, Any],
    metric_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    key = str(metric.get("metric_key") or "").strip()
    if not key or key not in metric_catalog:
        return None
    source = metric_catalog[key]
    value = source.get("value", 0)
    unit = str(source.get("unit") or metric.get("unit") or "")
    label = str(source.get("label") or metric.get("label") or key)
    formatted = str(source.get("formatted_value") or f"{value}{unit}")
    return {
        "metric_key": key,
        "label": label,
        "value": value,
        "formatted_value": formatted,
        "unit": unit,
        "source": "simulation_result",
    }


def _valid_entity(name: str, allowed: set[str]) -> bool:
    if not name:
        return False
    if not allowed:
        return True
    return name in allowed or name.lower() in {a.lower() for a in allowed}


def _collect_entities(normalized: dict[str, Any]) -> dict[str, set[str]]:
    products: set[str] = set()
    operations: set[str] = set()
    resources: set[str] = set()
    for p in normalized.get("products") or []:
        if isinstance(p, dict):
            if p.get("product_id"):
                products.add(str(p["product_id"]))
            if p.get("product_name"):
                products.add(str(p["product_name"]))
    for e in normalized.get("equipment") or []:
        if isinstance(e, dict):
            if e.get("equipment_id"):
                resources.add(str(e["equipment_id"]))
            if e.get("equipment_name"):
                resources.add(str(e["equipment_name"]))
    for lab in normalized.get("labor") or []:
        if isinstance(lab, dict):
            if lab.get("labor_id"):
                resources.add(str(lab["labor_id"]))
            if lab.get("labor_name"):
                resources.add(str(lab["labor_name"]))
    for op in normalized.get("operations") or []:
        if isinstance(op, dict):
            if op.get("operation_id"):
                operations.add(str(op["operation_id"]))
            if op.get("operation_name"):
                operations.add(str(op["operation_name"]))
    return {"products": products, "operations": operations, "resources": resources}


def _validate_problem(
    raw: dict[str, Any],
    *,
    metric_catalog: dict[str, dict[str, Any]],
    entities: dict[str, set[str]],
) -> dict[str, Any] | None:
    problem = _clean_str(raw.get("problem"), max_len=80)
    summary = _clean_str(raw.get("summary"), max_len=220)
    root_cause = _clean_str(raw.get("root_cause"), max_len=140)
    detailed = _clean_str(raw.get("detailed_reasoning"), max_len=1800)
    why = _clean_str(raw.get("why_top_four"), max_len=300)
    area = _clean_str(raw.get("area"), max_len=120)
    category = _clean_str(raw.get("category"), max_len=64) or "operational_issue"

    if not problem or not summary or not root_cause or not detailed:
        return None
    if _looks_like_recommendation(detailed) or _looks_like_recommendation(summary):
        # Keep finding but strip recommendation tone softly by truncation note.
        detailed = re.sub(
            r"(?i)\b(you should|we recommend|recommended action)[^.]*\.?",
            "",
            detailed,
        ).strip()

    severity = str(raw.get("severity") or "medium").strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"

    supporting: list[dict[str, Any]] = []
    for metric in raw.get("supporting_metrics") or []:
        if not isinstance(metric, dict):
            continue
        rewritten = _rewrite_metric(metric, metric_catalog)
        if rewritten:
            supporting.append(rewritten)

    evidence_keys: list[str] = []
    for key in raw.get("evidence_keys") or []:
        k = str(key).strip()
        if k in metric_catalog and k not in evidence_keys:
            evidence_keys.append(k)

    side_raw = raw.get("side_box") if isinstance(raw.get("side_box"), dict) else {}
    side_key = str(side_raw.get("metric_key") or "").strip()
    if side_key not in metric_catalog:
        side_key = supporting[0]["metric_key"] if supporting else (
            evidence_keys[0] if evidence_keys else ""
        )
    if not side_key or side_key not in metric_catalog:
        return None

    cat_entry = metric_catalog[side_key]
    side_box = {
        "prefix": _clean_str(side_raw.get("prefix"), max_len=80) or "Primary pressure",
        "subject": _clean_str(side_raw.get("subject"), max_len=120)
        or str(cat_entry.get("label") or side_key),
        "value": str(cat_entry.get("formatted_value") or side_raw.get("value") or ""),
        "description": _clean_str(side_raw.get("description"), max_len=300)
        or str(cat_entry.get("label") or ""),
        "metric_key": side_key,
    }

    affected_products = [
        str(x)
        for x in (raw.get("affected_products") or [])
        if _valid_entity(str(x), entities["products"])
    ]
    affected_operations = [
        str(x)
        for x in (raw.get("affected_operations") or [])
        if _valid_entity(str(x), entities["operations"])
    ]
    affected_resources = [
        str(x)
        for x in (raw.get("affected_resources") or [])
        if _valid_entity(str(x), entities["resources"])
    ]

    return {
        "id": str(raw.get("id") or "problem"),
        "rank": int(raw.get("rank") or 0) or 0,
        "problem": problem,
        "summary": summary,
        "root_cause": root_cause,
        "severity": severity,
        "category": category,
        "area": area or (affected_resources[0] if affected_resources else "Factory"),
        "problem_score": _clamp(raw.get("problem_score"), 0, 100, 50),
        "detailed_reasoning": _strip_bad_claims(detailed),
        "why_top_four": why or "This issue has strong operational evidence in the simulation.",
        "affected_products": affected_products[:20],
        "affected_operations": affected_operations[:20],
        "affected_resources": affected_resources[:20],
        "supporting_metrics": supporting[:12],
        "side_box": side_box,
        "confidence": _clamp(raw.get("confidence"), 0, 1, 0.7),
        "evidence_keys": evidence_keys[:20],
    }


def _validate_impact(
    raw: dict[str, Any],
    *,
    problem_ids: set[str],
    metric_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    title = _clean_str(raw.get("title"), max_len=160)
    description = _strip_bad_claims(_clean_str(raw.get("description"), max_len=1200))
    if not title or not description:
        return None
    if _looks_like_recommendation(description):
        description = re.sub(
            r"(?i)\b(you should|we recommend|recommended action)[^.]*\.?",
            "",
            description,
        ).strip()
        if not description:
            return None

    severity = str(raw.get("severity") or "medium").strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"
    horizon = str(raw.get("time_horizon") or "near_term").strip().lower()
    if horizon not in TIME_HORIZONS:
        horizon = "near_term"

    related = [
        str(x)
        for x in (raw.get("related_problem_ids") or [])
        if str(x) in problem_ids
    ]
    if not related and problem_ids:
        related = [sorted(problem_ids)[0]]

    evidence_keys = [
        str(k)
        for k in (raw.get("evidence_keys") or [])
        if str(k) in metric_catalog
    ]
    chain = [
        _clean_str(step, max_len=120)
        for step in (raw.get("causal_chain") or [])
        if _clean_str(step, max_len=120)
    ][:6]

    return {
        "id": str(raw.get("id") or "impact"),
        "rank": int(raw.get("rank") or 0) or 0,
        "title": title,
        "description": description,
        "severity": severity,
        "category": _clean_str(raw.get("category"), max_len=64) or "operational_resilience",
        "time_horizon": horizon,
        "related_problem_ids": related,
        "causal_chain": chain,
        "confidence": _clamp(raw.get("confidence"), 0, 1, 0.7),
        "evidence_keys": evidence_keys[:20],
    }


UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _looks_like_uuid(value: str) -> bool:
    return bool(UUID_RE.fullmatch((value or "").strip()))


def _build_entity_aliases(normalized: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    raw = normalized.get("entity_aliases")
    if isinstance(raw, dict):
        for key, value in raw.items():
            k = str(key).strip()
            v = str(value).strip()
            if k and v and k != v:
                aliases[k] = v
    for p in normalized.get("products") or []:
        if not isinstance(p, dict):
            continue
        display = str(p.get("display_name") or p.get("product_name") or "").strip()
        pid = str(p.get("product_id") or "").strip()
        if display and not _looks_like_uuid(display) and pid and pid != display:
            aliases[pid] = display
    for e in normalized.get("equipment") or []:
        if not isinstance(e, dict):
            continue
        display = str(e.get("display_name") or e.get("equipment_name") or "").strip()
        eid = str(e.get("equipment_id") or "").strip()
        if display and not _looks_like_uuid(display) and eid and eid != display:
            aliases[eid] = display
    for lab in normalized.get("labor") or []:
        if not isinstance(lab, dict):
            continue
        display = str(lab.get("display_name") or lab.get("labor_name") or "").strip()
        lid = str(lab.get("labor_id") or "").strip()
        if display and not _looks_like_uuid(display) and lid and lid != display:
            aliases[lid] = display
    return aliases


def _replace_ids_in_text(text: str, aliases: dict[str, str]) -> str:
    if not text or not aliases:
        return text

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return aliases.get(raw, aliases.get(raw.lower(), raw))

    # Prefer exact alias keys first (longest first), then any remaining UUIDs.
    out = text
    for key in sorted(aliases.keys(), key=len, reverse=True):
        if key in out:
            out = out.replace(key, aliases[key])
    out = UUID_RE.sub(repl, out)
    # Drop any UUID that still has no friendly name.
    out = UUID_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" -–,")
    return out


def _sanitize_entity_list(values: list[Any], aliases: dict[str, str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = _replace_ids_in_text(str(value), aliases).strip()
        if not text or _looks_like_uuid(text):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _rewrite_human_facing_ids(analysis: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    problems = []
    for problem in analysis.get("problems") or []:
        if not isinstance(problem, dict):
            continue
        item = dict(problem)
        for field in (
            "problem",
            "summary",
            "root_cause",
            "area",
            "detailed_reasoning",
            "why_top_four",
        ):
            if field in item:
                item[field] = _replace_ids_in_text(str(item.get(field) or ""), aliases)
        item["affected_products"] = _sanitize_entity_list(
            list(item.get("affected_products") or []), aliases
        )
        item["affected_operations"] = _sanitize_entity_list(
            list(item.get("affected_operations") or []), aliases
        )
        item["affected_resources"] = _sanitize_entity_list(
            list(item.get("affected_resources") or []), aliases
        )
        side = item.get("side_box")
        if isinstance(side, dict):
            side_box = dict(side)
            for field in ("prefix", "subject", "value", "description"):
                if field in side_box:
                    side_box[field] = _replace_ids_in_text(
                        str(side_box.get(field) or ""), aliases
                    )
            item["side_box"] = side_box
        # Drop problems whose title collapsed to empty after UUID removal.
        if not str(item.get("problem") or "").strip():
            continue
        problems.append(item)

    impacts = []
    for impact in analysis.get("business_impacts") or []:
        if not isinstance(impact, dict):
            continue
        item = dict(impact)
        for field in ("title", "description"):
            if field in item:
                item[field] = _replace_ids_in_text(str(item.get(field) or ""), aliases)
        chain = []
        for step in item.get("causal_chain") or []:
            cleaned = _replace_ids_in_text(str(step), aliases)
            if cleaned:
                chain.append(cleaned)
        item["causal_chain"] = chain
        impacts.append(item)

    overall = analysis.get("overall_business_impact")
    if isinstance(overall, dict):
        overall = dict(overall)
        for field in ("title", "description"):
            if field in overall:
                overall[field] = _replace_ids_in_text(
                    str(overall.get(field) or ""), aliases
                )

    out = dict(analysis)
    out["problems"] = problems
    out["problem_count"] = len(problems)
    out["business_impacts"] = impacts if problems else []
    out["overall_business_impact"] = overall if problems else None
    if len(problems) == 0:
        out["analysis_status"] = "limited_findings"
    elif len(problems) < 4:
        out["analysis_status"] = "limited_findings"
    return out


def validate_analysis_payload(
    raw: dict[str, Any],
    *,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    metric_catalog = normalized.get("metric_catalog") or {}
    if not isinstance(metric_catalog, dict):
        metric_catalog = {}
    entities = _collect_entities(normalized)

    problems_in = raw.get("problems") if isinstance(raw.get("problems"), list) else []
    validated_problems: list[dict[str, Any]] = []
    for item in problems_in:
        if not isinstance(item, dict):
            continue
        problem = _validate_problem(
            item, metric_catalog=metric_catalog, entities=entities
        )
        if problem:
            validated_problems.append(problem)

    ranked = rank_top_problems(validated_problems, limit=4)
    problem_ids = {p["id"] for p in ranked}
    problem_count = len(ranked)

    impacts_in = (
        raw.get("business_impacts") if isinstance(raw.get("business_impacts"), list) else []
    )
    validated_impacts: list[dict[str, Any]] = []
    if problem_count > 0:
        for item in impacts_in:
            if not isinstance(item, dict):
                continue
            impact = _validate_impact(
                item, problem_ids=problem_ids, metric_catalog=metric_catalog
            )
            if impact:
                validated_impacts.append(impact)
        # Keep exactly five when possible; truncate extras.
        validated_impacts = validated_impacts[:5]
        for index, impact in enumerate(validated_impacts, start=1):
            impact["id"] = f"impact_{index}"
            impact["rank"] = index

    overall = None
    overall_raw = raw.get("overall_business_impact")
    if problem_count > 0 and isinstance(overall_raw, dict):
        title = _clean_str(overall_raw.get("title"), max_len=160)
        description = _strip_bad_claims(
            _clean_str(overall_raw.get("description"), max_len=1800)
        )
        if title and description and not _looks_like_recommendation(description):
            severity = str(overall_raw.get("severity") or "medium").strip().lower()
            if severity not in SEVERITIES:
                severity = "medium"
            horizon = str(overall_raw.get("time_horizon") or "medium_term").strip().lower()
            if horizon not in TIME_HORIZONS:
                horizon = "medium_term"
            related = [
                str(x)
                for x in (overall_raw.get("related_problem_ids") or [])
                if str(x) in problem_ids
            ] or sorted(problem_ids)
            overall = {
                "title": title,
                "description": description,
                "severity": severity,
                "time_horizon": horizon,
                "related_problem_ids": related,
                "confidence": _clamp(overall_raw.get("confidence"), 0, 1, 0.75),
            }

    status = str(raw.get("analysis_status") or "").strip().lower()
    if problem_count == 0:
        status = "limited_findings"
    elif problem_count < 4:
        status = "limited_findings"
    elif status not in {"complete", "limited_findings"}:
        status = "complete"

    payload = {
        "analysis_status": status,
        "problem_count": problem_count,
        "problems": ranked,
        "business_impacts": validated_impacts if problem_count else [],
        "overall_business_impact": overall if problem_count else None,
    }
    aliases = _build_entity_aliases(normalized)
    return _rewrite_human_facing_ids(payload, aliases)


def finalize_analysis_display(
    analysis: dict[str, Any],
    *,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    """Rewrite any leftover UUIDs in a validated or fallback analysis payload."""
    aliases = _build_entity_aliases(normalized)
    return _rewrite_human_facing_ids(dict(analysis), aliases)


def strip_internal_scores(analysis: dict[str, Any]) -> dict[str, Any]:
    """Remove problem_score before returning to clients."""
    problems = []
    for problem in analysis.get("problems") or []:
        if not isinstance(problem, dict):
            continue
        clone = dict(problem)
        clone.pop("problem_score", None)
        problems.append(clone)
    out = dict(analysis)
    out["problems"] = problems
    return out
