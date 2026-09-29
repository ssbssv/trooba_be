"""Deterministic Recommended Actions when the AI path fails."""
from __future__ import annotations

from typing import Any


def _first_metric(keys: list[str], catalog: dict[str, Any]) -> str | None:
    for key in keys:
        if key in catalog:
            return key
    return None


def _impact_ids_for_problems(
    problem_ids: list[str],
    impacts: list[dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    pid_set = set(problem_ids)
    for impact in impacts:
        if not isinstance(impact, dict):
            continue
        related = set(impact.get("related_problem_ids") or [])
        if related.intersection(pid_set) and impact.get("id"):
            out.append(str(impact["id"]))
    return out[:3]


def build_deterministic_recommended_actions(
    *,
    analysis_id: str,
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    problems = [
        p
        for p in (input_payload.get("validated_problems") or [])
        if isinstance(p, dict)
    ]
    impacts = [
        i
        for i in (input_payload.get("validated_business_impacts") or [])
        if isinstance(i, dict)
    ]
    catalog = input_payload.get("metric_catalog") or {}
    evidence = input_payload.get("simulation_evidence") or {}

    actions: list[dict[str, Any]] = []

    # Group problems by primary resource / area
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in problems:
        resources = p.get("affected_resources") or []
        key = str(resources[0]) if resources else str(p.get("area") or "factory")
        groups.setdefault(key, []).append(p)

    # Action 1: primary equipment / flow constraint
    eq_list = [e for e in (evidence.get("equipment") or []) if isinstance(e, dict)]
    primary_eq = None
    if eq_list:
        primary_eq = max(eq_list, key=lambda e: float(e.get("utilization") or 0))
    if primary_eq or groups:
        area_name = (
            str(
                (primary_eq or {}).get("display_name")
                or (primary_eq or {}).get("equipment_name")
                or next(iter(groups.keys()), "factory")
            )
        )
        related = []
        for key, plist in groups.items():
            if key == area_name or area_name in key or key in area_name:
                related.extend(plist)
        if not related:
            related = problems[:2]
        related_ids = [str(p["id"]) for p in related if p.get("id")]
        ekeys = []
        for p in related:
            for k in p.get("evidence_keys") or []:
                if k and str(k) in catalog:
                    ekeys.append(str(k))
        ekeys = list(dict.fromkeys(ekeys))[:4]
        mk_wait = _first_metric(
            [k for k in catalog if "wait_equipment" in k],
            catalog,
        )
        actions.append(
            {
                "id": "action_1",
                "priority_rank": 1,
                "priority": "high",
                "expected_impact_level": "high",
                "title": f"Relieve {area_name} flow constraint",
                "summary": (
                    f"Reduce avoidable occupancy and waiting around {area_name} "
                    "by reviewing setup losses and rebalancing eligible work."
                ),
                "why": (
                    f"{area_name} is linked to validated equipment waiting and elevated "
                    "Manufacturing Cycle Time for affected products."
                ),
                "action_type": "reduce_setup_time",
                "impact_area": {
                    "id": area_name,
                    "name": area_name,
                    "type": "work_center",
                },
                "related_problem_ids": related_ids,
                "related_business_impact_ids": _impact_ids_for_problems(related_ids, impacts),
                "implementation_steps": [
                    {
                        "step": 1,
                        "title": "Review setup and changeover losses",
                        "description": (
                            f"Compare setup contribution and queue behavior for {area_name} "
                            "operations using the supplied simulation evidence."
                        ),
                    },
                    {
                        "step": 2,
                        "title": "Rebalance eligible work",
                        "description": (
                            "Evaluate workload balance across eligible operations "
                            "without inventing unsupported alternative capacity."
                        ),
                    },
                    {
                        "step": 3,
                        "title": "Re-run the scenario",
                        "description": (
                            "Validate whether equipment waiting, queue, and affected "
                            "product MCT move in the desired direction."
                        ),
                    },
                ],
                "expected_impacts": [
                    {
                        "kpi": "Equipment Waiting",
                        "direction": "decrease",
                        "display_label": "Equipment Waiting",
                        "metric_key": mk_wait,
                    },
                    {
                        "kpi": "Manufacturing Cycle Time",
                        "direction": "decrease",
                        "display_label": "MCT",
                        "metric_key": _first_metric(
                            [k for k in catalog if k.endswith(".total")],
                            catalog,
                        ),
                    },
                    {
                        "kpi": "Flow Efficiency",
                        "direction": "improve",
                        "display_label": "Flow Efficiency",
                        "metric_key": None,
                    },
                ],
                "success_indicators": [
                    {
                        "label": catalog.get(k, {}).get("label", k) if isinstance(catalog.get(k), dict) else k,
                        "metric_key": k,
                        "desired_direction": "decrease",
                    }
                    for k in ekeys[:2]
                ],
                "time_horizon": "near_term",
                "confidence": 0.8,
                "evidence_keys": ekeys,
                "coverage_score": 90,
                "feasibility_score": 85,
                "evidence_score": 85,
            }
        )

    # Action 2: lot / product waiting
    products = [p for p in (evidence.get("products") or []) if isinstance(p, dict)]
    lot_product = None
    if products:
        lot_product = max(
            products,
            key=lambda p: float((p.get("mct") or {}).get("wait_lot") or 0),
        )
    if lot_product and float((lot_product.get("mct") or {}).get("wait_lot") or 0) > 0:
        pname = str(
            lot_product.get("display_name")
            or lot_product.get("product_name")
            or lot_product.get("product_id")
        )
        related = [
            p
            for p in problems
            if pname in (p.get("affected_products") or [])
            or "lot" in str(p.get("category") or "").lower()
            or "lot" in str(p.get("problem") or "").lower()
        ] or problems[1:2] or problems[:1]
        related_ids = [str(p["id"]) for p in related if p.get("id")]
        mk = _first_metric(
            [k for k in catalog if pname in k and "wait_lot" in k],
            catalog,
        )
        actions.append(
            {
                "id": "action_2",
                "priority_rank": 2,
                "priority": "medium",
                "expected_impact_level": "medium",
                "title": f"Reduce {pname} lot-waiting delay",
                "summary": (
                    f"Review lot size and transfer-batch settings for {pname} "
                    "to reduce non-value-added lot waiting where supported."
                ),
                "why": (
                    f"{pname} shows material Wait for Lot in the simulation evidence, "
                    "which contributes to Manufacturing Cycle Time."
                ),
                "action_type": "adjust_lot_size",
                "impact_area": {"id": pname, "name": pname, "type": "product"},
                "related_problem_ids": related_ids,
                "related_business_impact_ids": _impact_ids_for_problems(related_ids, impacts),
                "implementation_steps": [
                    {
                        "step": 1,
                        "title": "Review lot size",
                        "description": f"Compare current lot-waiting contribution for {pname}.",
                    },
                    {
                        "step": 2,
                        "title": "Test a smaller transfer batch",
                        "description": (
                            "Pilot a feasible transfer-batch adjustment and re-run the scenario."
                        ),
                    },
                ],
                "expected_impacts": [
                    {
                        "kpi": "Wait for Lot",
                        "direction": "decrease",
                        "display_label": "Wait for Lot",
                        "metric_key": mk,
                    },
                    {
                        "kpi": "MCT",
                        "direction": "decrease",
                        "display_label": "MCT",
                        "metric_key": None,
                    },
                    {
                        "kpi": "Flow Efficiency",
                        "direction": "improve",
                        "display_label": "Flow Efficiency",
                        "metric_key": None,
                    },
                ],
                "success_indicators": (
                    [
                        {
                            "label": catalog[mk]["label"] if isinstance(catalog.get(mk), dict) else "Wait for Lot",
                            "metric_key": mk,
                            "desired_direction": "decrease",
                        }
                    ]
                    if mk
                    else []
                ),
                "time_horizon": "near_term",
                "confidence": 0.75,
                "evidence_keys": [mk] if mk else [],
                "coverage_score": 70,
                "feasibility_score": 80,
                "evidence_score": 75,
            }
        )

    # Action 3: labor / inspection / queue congestion
    ops = [o for o in (evidence.get("operations") or []) if isinstance(o, dict)]
    congested = None
    if ops:
        congested = max(ops, key=lambda o: float(o.get("queue") or o.get("wip") or 0))
    labor_rows = [lab for lab in (evidence.get("labor") or []) if isinstance(lab, dict)]
    if congested or labor_rows:
        area_name = str(
            (congested or {}).get("operation_name")
            or (congested or {}).get("operation_id")
            or (
                (labor_rows[0].get("display_name") or labor_rows[0].get("labor_name"))
                if labor_rows
                else "Inspection"
            )
        )
        related = problems[-1:] if problems else []
        related_ids = [str(p["id"]) for p in related if p.get("id")] or (
            [str(problems[0]["id"])] if problems and problems[0].get("id") else []
        )
        actions.append(
            {
                "id": "action_3",
                "priority_rank": 3,
                "priority": "medium",
                "expected_impact_level": "medium",
                "title": f"Balance {area_name} capacity and arrivals",
                "summary": (
                    f"Review labor assignment and release sequencing around {area_name} "
                    "to reduce queue and WIP growth."
                ),
                "why": (
                    f"{area_name} shows elevated queue or labor waiting signals that can "
                    "compound the validated flow problems if left unaddressed."
                ),
                "action_type": "improve_inspection_flow"
                if "inspect" in area_name.lower()
                else "change_labor_assignment",
                "impact_area": {
                    "id": area_name,
                    "name": area_name,
                    "type": "department" if "inspect" in area_name.lower() else "labor",
                },
                "related_problem_ids": related_ids,
                "related_business_impact_ids": _impact_ids_for_problems(related_ids, impacts),
                "implementation_steps": [
                    {
                        "step": 1,
                        "title": "Review assignment timing",
                        "description": (
                            f"Check labor assignment and arrival patterns for {area_name}."
                        ),
                    },
                    {
                        "step": 2,
                        "title": "Sequence releases carefully",
                        "description": (
                            "Rebalance inflow against processing capacity, then re-run the scenario."
                        ),
                    },
                ],
                "expected_impacts": [
                    {
                        "kpi": "WIP",
                        "direction": "decrease",
                        "display_label": "WIP",
                        "metric_key": None,
                    },
                    {
                        "kpi": "Lead Time",
                        "direction": "decrease",
                        "display_label": "Lead Time",
                        "metric_key": None,
                    },
                    {
                        "kpi": "Utilization Balance",
                        "direction": "improve",
                        "display_label": "Utilization Balance",
                        "metric_key": None,
                    },
                ],
                "success_indicators": [],
                "time_horizon": "near_term",
                "confidence": 0.7,
                "evidence_keys": [],
                "coverage_score": 60,
                "feasibility_score": 75,
                "evidence_score": 65,
            }
        )

    # Ensure at least one monitor action if nothing else
    if not actions and problems:
        p = problems[0]
        pid = str(p.get("id") or "problem_1")
        area = str(p.get("area") or "factory")
        actions.append(
            {
                "id": "action_1",
                "priority_rank": 1,
                "priority": "high",
                "expected_impact_level": "medium",
                "title": f"Monitor {area} emerging constraint",
                "summary": (
                    f"Track the validated issue at {area} and re-run the scenario "
                    "after any operational change."
                ),
                "why": str(p.get("summary") or p.get("problem") or "Validated problem requires attention."),
                "action_type": "monitor_emerging_constraint",
                "impact_area": {"id": area, "name": area, "type": "factory"},
                "related_problem_ids": [pid],
                "related_business_impact_ids": _impact_ids_for_problems([pid], impacts),
                "implementation_steps": [
                    {
                        "step": 1,
                        "title": "Review the validated evidence",
                        "description": "Confirm the current problem signals in the simulation results.",
                    },
                    {
                        "step": 2,
                        "title": "Pilot a feasible change",
                        "description": "Apply a low-risk operational adjustment and re-run the scenario.",
                    },
                ],
                "expected_impacts": [
                    {
                        "kpi": "Flow Efficiency",
                        "direction": "improve",
                        "display_label": "Flow Efficiency",
                        "metric_key": None,
                    }
                ],
                "success_indicators": [],
                "time_horizon": "near_term",
                "confidence": 0.65,
                "evidence_keys": list(p.get("evidence_keys") or [])[:3],
                "coverage_score": 50,
                "feasibility_score": 80,
                "evidence_score": 60,
            }
        )

    status = "complete" if len(actions) >= 3 else "limited_actions"
    return {
        "analysis_id": analysis_id,
        "recommendation_status": status,
        "action_count": len(actions[:3]),
        "recommended_actions": actions[:3],
        "source": "deterministic_fallback",
    }
