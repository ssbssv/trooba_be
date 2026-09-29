"""Build the authoritative AI input payload for Manager's Playbook."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from apps.ai.prompts.managers_playbook import MANAGERS_PLAYBOOK_PROMPT_VERSION
from apps.ai.services.recommended_actions.input_builder import (
    ALLOWED_ACTION_TYPES,
    _build_feasibility,
    _filter_relevant,
    _entity_set,
    _relevant_metric_catalog,
)

AVAILABLE_MANAGEMENT_LEVERS = [
    "Stabilize primary equipment or work-center constraint",
    "Control queue, WIP, or release into congested areas",
    "Reduce non-value-added MCT waiting on high-MCT products",
    "Use labor assignment and flexibility more effectively",
    "Review lot size or transfer-batch policy where editable",
    "Review routing or sequencing where alternatives exist",
    "Defer capital purchases until lower-cost options are tested",
    "Defer hiring until labor shortage is proven",
    "Re-run the scenario to validate directional improvement",
    "Monitor emerging constraints with clear escalation criteria",
]


def recommendations_fingerprint(recommendations_payload: dict[str, Any]) -> str:
    """Stable hash so playbook cache invalidates when Recommended Actions change."""
    blob = {
        "recommendation_status": recommendations_payload.get("recommendation_status"),
        "action_count": recommendations_payload.get("action_count"),
        "recommended_actions": recommendations_payload.get("recommended_actions") or [],
    }
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def playbook_cache_fingerprint(
    *,
    analysis_id: str,
    analysis_payload: dict[str, Any],
    recommendations_payload: dict[str, Any],
) -> str:
    """Combined fingerprint of parent analysis + recommended actions."""
    blob = {
        "analysis_id": analysis_id,
        "problems": analysis_payload.get("problems") or [],
        "business_impacts": analysis_payload.get("business_impacts") or [],
        "overall_business_impact": analysis_payload.get("overall_business_impact"),
        "recommendations": recommendations_payload.get("recommended_actions") or [],
        "recommendation_status": recommendations_payload.get("recommendation_status"),
    }
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def build_managers_playbook_input(
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
    recommended_actions: list[dict[str, Any]],
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
                "title": p.get("problem"),
                "summary": p.get("summary"),
                "root_cause": p.get("root_cause"),
                "severity": p.get("severity"),
                "category": p.get("category"),
                "area": p.get("area"),
                "detailed_reasoning": p.get("detailed_reasoning"),
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

    overall = {}
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

    validated_actions = []
    for action in recommended_actions:
        if not isinstance(action, dict):
            continue
        validated_actions.append(
            {
                "id": action.get("id"),
                "priority_rank": action.get("priority_rank"),
                "priority": action.get("priority"),
                "expected_impact_level": action.get("expected_impact_level"),
                "title": action.get("title"),
                "summary": action.get("summary"),
                "why": action.get("why"),
                "action_type": action.get("action_type"),
                "impact_area": action.get("impact_area"),
                "related_problem_ids": list(action.get("related_problem_ids") or []),
                "related_business_impact_ids": list(
                    action.get("related_business_impact_ids") or []
                ),
                "implementation_steps": list(action.get("implementation_steps") or []),
                "expected_impacts": list(
                    action.get("expected_impacts") or action.get("expected_kpi_impacts") or []
                ),
                "success_indicators": list(action.get("success_indicators") or []),
                "time_horizon": action.get("time_horizon"),
                "confidence": action.get("confidence"),
                "evidence_keys": list(action.get("evidence_keys") or []),
            }
        )

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
            "prompt_version": MANAGERS_PLAYBOOK_PROMPT_VERSION,
            "unit_system": unit_system,
        },
        "validated_problems": validated_problems,
        "validated_business_impacts": validated_impacts,
        "validated_overall_business_impact": overall,
        "validated_recommended_actions": validated_actions,
        "simulation_evidence": evidence,
        "management_context": {
            "primary_management_goal": (
                "Reduce non-value-added MCT and improve flow before major "
                "capital or staffing decisions."
            ),
            "preferred_decision_order": [
                "Stabilize current constraints",
                "Use existing capacity better",
                "Test lower-cost operational changes",
                "Validate through another simulation run",
                "Escalate to capital or staffing changes only when supported",
            ],
            "available_management_levers": list(AVAILABLE_MANAGEMENT_LEVERS),
            "allowed_action_types": list(ALLOWED_ACTION_TYPES),
            "action_feasibility": _build_feasibility(evidence),
        },
        "metric_catalog": _relevant_metric_catalog(catalog, problems, business_impacts),
        "playbook_constraints": {
            "overview_word_min": 25,
            "overview_word_max": 45,
            "required_action_count": 5,
            "allow_fewer_when_evidence_is_insufficient": True,
            "action_description_sentence_min": 1,
            "action_description_sentence_max": 2,
            "final_summary_word_min": 15,
            "final_summary_word_max": 30,
            "must_use_first_person_manager_voice": True,
            "must_reference_existing_analysis": True,
            "must_not_rediscover_problems": True,
            "must_not_invent_entities": True,
            "must_not_invent_costs": True,
            "must_not_invent_savings": True,
            "must_not_guarantee_results": True,
        },
    }
