"""Build the authoritative AI input payload for Recommended Actions."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from apps.ai.prompts.recommended_actions import RECOMMENDED_ACTIONS_PROMPT_VERSION

ALLOWED_ACTION_TYPES = [
    "reduce_setup_time",
    "rebalance_workload",
    "adjust_lot_size",
    "adjust_transfer_batch",
    "change_labor_assignment",
    "change_shift_coverage",
    "cross_train_labor",
    "use_alternative_resource",
    "review_routing",
    "increase_capacity",
    "improve_inspection_flow",
    "reduce_queue_release",
    "sequence_work_differently",
    "monitor_emerging_constraint",
    "reduce_scrap",
    "reduce_rework",
    "resolve_model_warning",
]


def analysis_fingerprint(analysis_row_id: str, analysis_payload: dict[str, Any]) -> str:
    """Stable hash of validated problems + impacts so cache invalidates when they change."""
    blob = {
        "analysis_id": analysis_row_id,
        "problems": analysis_payload.get("problems") or [],
        "business_impacts": analysis_payload.get("business_impacts") or [],
        "overall_business_impact": analysis_payload.get("overall_business_impact"),
        "analysis_status": analysis_payload.get("analysis_status"),
    }
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _entity_set(problems: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    products: set[str] = set()
    resources: set[str] = set()
    operations: set[str] = set()
    for p in problems:
        if not isinstance(p, dict):
            continue
        for x in p.get("affected_products") or []:
            if x:
                products.add(str(x))
        for x in p.get("affected_resources") or []:
            if x:
                resources.add(str(x))
        for x in p.get("affected_operations") or []:
            if x:
                operations.add(str(x))
    return products, resources, operations


def _filter_relevant(
    normalized: dict[str, Any],
    *,
    product_ids: set[str],
    resource_ids: set[str],
    operation_ids: set[str],
) -> dict[str, Any]:
    """Keep entities referenced by validated problems; fall back to full lists if empty."""
    products = [
        p
        for p in (normalized.get("products") or [])
        if isinstance(p, dict)
        and (
            not product_ids
            or str(p.get("product_id") or "") in product_ids
            or str(p.get("display_name") or "") in product_ids
            or str(p.get("product_name") or "") in product_ids
        )
    ]
    if not products:
        products = [p for p in (normalized.get("products") or []) if isinstance(p, dict)][:8]

    equipment = [
        e
        for e in (normalized.get("equipment") or [])
        if isinstance(e, dict)
        and (
            not resource_ids
            or str(e.get("equipment_id") or "") in resource_ids
            or str(e.get("display_name") or "") in resource_ids
            or str(e.get("equipment_name") or "") in resource_ids
        )
    ]
    if not equipment:
        equipment = [e for e in (normalized.get("equipment") or []) if isinstance(e, dict)][:8]

    labor = [
        lab
        for lab in (normalized.get("labor") or [])
        if isinstance(lab, dict)
        and (
            not resource_ids
            or str(lab.get("labor_id") or "") in resource_ids
            or str(lab.get("display_name") or "") in resource_ids
            or str(lab.get("labor_name") or "") in resource_ids
        )
    ]
    if not labor:
        labor = [lab for lab in (normalized.get("labor") or []) if isinstance(lab, dict)][:8]

    operations = [
        op
        for op in (normalized.get("operations") or [])
        if isinstance(op, dict)
        and (
            not operation_ids
            or str(op.get("operation_id") or "") in operation_ids
            or str(op.get("operation_name") or "") in operation_ids
        )
    ]
    if not operations:
        operations = [op for op in (normalized.get("operations") or []) if isinstance(op, dict)][:12]

    return {
        "summary": normalized.get("summary") or {},
        "products": products,
        "equipment": equipment,
        "labor": labor,
        "operations": operations,
        "routing": [],
        "production": normalized.get("production") or {
            "target_shortfalls": [],
            "throughput_constraints": [],
        },
        "quality": normalized.get("quality") or {
            "scrap_by_product": [],
            "scrap_by_operation": [],
            "rework": [],
        },
        "warnings": list(normalized.get("warnings") or [])[:20],
    }


def _build_feasibility(evidence: dict[str, Any]) -> dict[str, Any]:
    equipment_flex = []
    for e in evidence.get("equipment") or []:
        if not isinstance(e, dict):
            continue
        rid = str(e.get("display_name") or e.get("equipment_name") or e.get("equipment_id") or "")
        if not rid:
            continue
        equipment_flex.append(
            {
                "resource_id": rid,
                "can_add_units": False,
                "alternative_equipment": [],
                "shared_operations": list(e.get("operations") or []),
                "setup_data_available": True,
                "routing_alternatives_available": False,
            }
        )

    labor_flex = []
    for lab in evidence.get("labor") or []:
        if not isinstance(lab, dict):
            continue
        lid = str(lab.get("display_name") or lab.get("labor_name") or lab.get("labor_id") or "")
        if not lid:
            continue
        labor_flex.append(
            {
                "labor_id": lid,
                "cross_trained_labor": [],
                "shift_data_available": False,
                "assignment_data_available": True,
            }
        )

    product_rules = []
    for p in evidence.get("products") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("display_name") or p.get("product_name") or p.get("product_id") or "")
        if not pid:
            continue
        product_rules.append(
            {
                "product_id": pid,
                "lot_size_editable": True,
                "transfer_batch_editable": True,
                "routing_editable": True,
            }
        )

    return {
        "equipment_flexibility": equipment_flex,
        "labor_flexibility": labor_flex,
        "product_rules": product_rules,
        "allowed_action_types": list(ALLOWED_ACTION_TYPES),
    }


def _relevant_metric_catalog(
    catalog: dict[str, Any],
    problems: list[dict[str, Any]],
    impacts: list[dict[str, Any]],
) -> dict[str, Any]:
    keys: set[str] = set()
    for p in problems:
        if not isinstance(p, dict):
            continue
        for k in p.get("evidence_keys") or []:
            if k:
                keys.add(str(k))
        for m in p.get("supporting_metrics") or []:
            if isinstance(m, dict) and m.get("metric_key"):
                keys.add(str(m["metric_key"]))
    for impact in impacts:
        if not isinstance(impact, dict):
            continue
        for k in impact.get("evidence_keys") or []:
            if k:
                keys.add(str(k))

    out: dict[str, Any] = {}
    for key in keys:
        meta = catalog.get(key)
        if isinstance(meta, dict):
            out[key] = {
                "value": meta.get("value"),
                "unit": meta.get("unit") or "",
                "label": meta.get("label") or key,
            }
    # Always include a few summary metrics when present
    for key in ("summary.overall_mct", "summary.overall_wip"):
        meta = catalog.get(key)
        if isinstance(meta, dict):
            out[key] = {
                "value": meta.get("value"),
                "unit": meta.get("unit") or "",
                "label": meta.get("label") or key,
            }
    return out


def build_recommended_actions_input(
    *,
    analysis_id: str,
    organization_id: str | None,
    factory_id: str,
    factory_name: str,
    model_id: str,
    model_name: str,
    scenario_id: str,
    scenario_name: str,
    simulation_version: str,
    simulation_completed_at: str | None,
    analysis_generated_at: str | None,
    problems: list[dict[str, Any]],
    business_impacts: list[dict[str, Any]],
    overall_business_impact: dict[str, Any] | None,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    product_ids, resource_ids, operation_ids = _entity_set(problems)
    evidence = _filter_relevant(
        normalized,
        product_ids=product_ids,
        resource_ids=resource_ids,
        operation_ids=operation_ids,
    )
    catalog = (
        normalized.get("metric_catalog")
        if isinstance(normalized.get("metric_catalog"), dict)
        else {}
    )
    unit_system = (
        (normalized.get("analysis_context") or {}).get("unit_system")
        if isinstance(normalized.get("analysis_context"), dict)
        else {"time": "days", "quantity": "units", "utilization": "%"}
    )

    validated_problems = []
    for p in problems:
        if not isinstance(p, dict):
            continue
        validated_problems.append(
            {
                "id": p.get("id"),
                "rank": p.get("rank"),
                "problem": p.get("problem"),
                "summary": p.get("summary"),
                "root_cause": p.get("root_cause"),
                "severity": p.get("severity"),
                "category": p.get("category"),
                "area": p.get("area"),
                "problem_score": p.get("problem_score"),
                "detailed_reasoning": p.get("detailed_reasoning"),
                "why_top_four": p.get("why_top_four"),
                "affected_products": list(p.get("affected_products") or []),
                "affected_operations": list(p.get("affected_operations") or []),
                "affected_resources": list(p.get("affected_resources") or []),
                "supporting_metrics": list(p.get("supporting_metrics") or []),
                "evidence_keys": list(p.get("evidence_keys") or []),
            }
        )

    validated_impacts = []
    for impact in business_impacts:
        if not isinstance(impact, dict):
            continue
        validated_impacts.append(
            {
                "id": impact.get("id"),
                "rank": impact.get("rank"),
                "title": impact.get("title"),
                "description": impact.get("description"),
                "severity": impact.get("severity"),
                "category": impact.get("category"),
                "time_horizon": impact.get("time_horizon"),
                "related_problem_ids": list(impact.get("related_problem_ids") or []),
                "causal_chain": list(impact.get("causal_chain") or []),
                "confidence": impact.get("confidence"),
                "evidence_keys": list(impact.get("evidence_keys") or []),
            }
        )

    overall = None
    if isinstance(overall_business_impact, dict):
        overall = {
            "title": overall_business_impact.get("title"),
            "description": overall_business_impact.get("description"),
            "severity": overall_business_impact.get("severity"),
            "time_horizon": overall_business_impact.get("time_horizon"),
            "related_problem_ids": list(
                overall_business_impact.get("related_problem_ids") or []
            ),
            "confidence": overall_business_impact.get("confidence"),
        }

    return {
        "analysis_context": {
            "analysis_id": analysis_id,
            "organization_id": organization_id or "",
            "factory_id": factory_id,
            "factory_name": factory_name,
            "model_id": model_id,
            "model_name": model_name,
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "simulation_version": simulation_version,
            "simulation_completed_at": simulation_completed_at or "",
            "analysis_generated_at": analysis_generated_at or "",
            "prompt_version": RECOMMENDED_ACTIONS_PROMPT_VERSION,
            "unit_system": unit_system,
        },
        "factory_objectives": {
            "primary_focus": [
                "Reduce Manufacturing Cycle Time",
                "Reduce non-value-added waiting",
                "Improve equipment and labor utilization balance",
                "Reduce queue and WIP",
                "Improve production flow",
            ],
            "bad_mct_components": ["wait_equipment", "wait_labor", "wait_lot"],
            "productive_mct_components": ["setup", "run"],
        },
        "validated_problems": validated_problems,
        "validated_business_impacts": validated_impacts,
        "validated_overall_business_impact": overall or {},
        "simulation_evidence": evidence,
        "available_action_context": _build_feasibility(evidence),
        "metric_catalog": _relevant_metric_catalog(catalog, problems, business_impacts),
        "recommendation_constraints": {
            "required_action_count": 3,
            "allow_fewer_when_evidence_is_insufficient": True,
            "must_reference_problem_ids": True,
            "must_reference_metric_keys": True,
            "must_use_only_existing_entities": True,
            "must_not_invent_costs": True,
            "must_not_invent_savings": True,
            "must_not_guarantee_results": True,
            "must_not_recommend_unsupported_capacity_addition": True,
            "must_prefer_root_cause_actions": True,
            "must_merge_actions_with_same_root_cause": True,
        },
    }
