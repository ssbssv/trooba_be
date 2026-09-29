"""Deterministic Manager's Playbook when the AI path fails."""
from __future__ import annotations

from typing import Any


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


def _rec_ids_for_problems(
    problem_ids: list[str],
    recommendations: list[dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    pid_set = set(problem_ids)
    for action in recommendations:
        if not isinstance(action, dict):
            continue
        related = set(action.get("related_problem_ids") or [])
        if related.intersection(pid_set) and action.get("id"):
            out.append(str(action["id"]))
    if not out and recommendations:
        for action in recommendations[:1]:
            if action.get("id"):
                out.append(str(action["id"]))
    return out[:2]


def _evidence_for(problems: list[dict[str, Any]], catalog: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for p in problems:
        for k in p.get("evidence_keys") or []:
            if k and (str(k) in catalog or True):
                keys.append(str(k))
    return list(dict.fromkeys(keys))[:4]


def build_deterministic_managers_playbook(
    *,
    analysis_id: str,
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    problems = [
        p for p in (input_payload.get("validated_problems") or []) if isinstance(p, dict)
    ]
    impacts = [
        i
        for i in (input_payload.get("validated_business_impacts") or [])
        if isinstance(i, dict)
    ]
    recommendations = [
        a
        for a in (input_payload.get("validated_recommended_actions") or [])
        if isinstance(a, dict)
    ]
    catalog = input_payload.get("metric_catalog") or {}
    evidence = input_payload.get("simulation_evidence") or {}

    actions: list[dict[str, Any]] = []

    # 1. Stabilize highest-ranked active constraint
    p1 = problems[0] if problems else None
    if p1:
        area = str(p1.get("area") or "the primary constraint")
        pids = [str(p1["id"])] if p1.get("id") else []
        actions.append(
            {
                "id": "manager_action_1",
                "rank": 1,
                "title": f"I would stabilize {area} before adding capacity.",
                "description": (
                    f"I would first review setup losses, sequencing, and eligible workload "
                    f"around {area} because it is linked to the highest-ranked validated constraint. "
                    "I would test these changes before considering major capacity decisions."
                ),
                "action_intent": "stabilize_primary_constraint",
                "impact_area": {"id": area, "name": area, "type": "work_center"},
                "related_problem_ids": pids,
                "related_recommendation_ids": _rec_ids_for_problems(pids, recommendations),
                "related_business_impact_ids": _impact_ids_for_problems(pids, impacts),
                "time_horizon": "immediate",
                "confidence": 0.85,
                "evidence_keys": _evidence_for([p1], catalog),
            }
        )

    # 2. Control largest queue / WIP
    ops = [o for o in (evidence.get("operations") or []) if isinstance(o, dict)]
    congested = None
    if ops:
        congested = max(ops, key=lambda o: float(o.get("queue") or o.get("wip") or 0))
    queue_area = str(
        (congested or {}).get("operation_name")
        or (congested or {}).get("operation_id")
        or (problems[1].get("area") if len(problems) > 1 else "the largest queue")
    )
    p_queue = problems[1] if len(problems) > 1 else (problems[0] if problems else None)
    if p_queue:
        pids = [str(p_queue["id"])] if p_queue.get("id") else []
        actions.append(
            {
                "id": "manager_action_2",
                "rank": 2,
                "title": f"Next, I would control the {queue_area} queue.",
                "description": (
                    f"I would tighten release and sequencing into {queue_area} to limit "
                    "further WIP growth while the primary constraint is being stabilized."
                ),
                "action_intent": "control_queue",
                "impact_area": {
                    "id": queue_area,
                    "name": queue_area,
                    "type": "department" if "inspect" in queue_area.lower() else "operation",
                },
                "related_problem_ids": pids,
                "related_recommendation_ids": _rec_ids_for_problems(pids, recommendations),
                "related_business_impact_ids": _impact_ids_for_problems(pids, impacts),
                "time_horizon": "immediate",
                "confidence": 0.8,
                "evidence_keys": _evidence_for([p_queue], catalog),
            }
        )

    # 3. Reduce dominant bad waiting on highest-MCT product
    products = [p for p in (evidence.get("products") or []) if isinstance(p, dict)]
    top_product = None
    if products:
        def _wait_sum(prod: dict[str, Any]) -> float:
            mct = prod.get("mct") if isinstance(prod.get("mct"), dict) else {}
            return float(
                mct.get("wait_equipment")
                or 0
            ) + float(mct.get("wait_labor") or 0) + float(mct.get("wait_lot") or 0)

        top_product = max(products, key=_wait_sum)
    if top_product:
        pname = str(
            top_product.get("display_name")
            or top_product.get("product_name")
            or top_product.get("product_id")
            or "the high-MCT product"
        )
    elif problems:
        affected = problems[0].get("affected_products") or []
        pname = str(affected[0]) if affected else "the high-MCT product"
    else:
        pname = "the high-MCT product"
    related_product_problems = [
        p
        for p in problems
        if pname in (p.get("affected_products") or [])
        or "mct" in str(p.get("category") or "").lower()
        or "wait" in str(p.get("problem") or "").lower()
    ] or (problems[:1] if problems else [])
    if related_product_problems:
        pids = [str(p["id"]) for p in related_product_problems if p.get("id")][:2]
        actions.append(
            {
                "id": "manager_action_3",
                "rank": 3,
                "title": f"Then, I would reduce {pname}'s non-productive waiting.",
                "description": (
                    f"I would focus on Wait for Equipment, Wait for Labor, and Wait for Lot "
                    f"on {pname}, because those components dominate non-value-added MCT."
                ),
                "action_intent": "reduce_non_value_added_mct",
                "impact_area": {"id": pname, "name": pname, "type": "product"},
                "related_problem_ids": pids,
                "related_recommendation_ids": _rec_ids_for_problems(pids, recommendations),
                "related_business_impact_ids": _impact_ids_for_problems(pids, impacts),
                "time_horizon": "near_term",
                "confidence": 0.82,
                "evidence_keys": _evidence_for(related_product_problems, catalog),
            }
        )

    # 4. Labor / routing / lot flexibility from recommendations
    labor_rows = [lab for lab in (evidence.get("labor") or []) if isinstance(lab, dict)]
    labor_name = str(
        (labor_rows[0].get("display_name") or labor_rows[0].get("labor_name"))
        if labor_rows
        else "available labor"
    )
    rec = recommendations[min(1, len(recommendations) - 1)] if recommendations else None
    p_flex = problems[-1] if problems else None
    if p_flex or rec:
        pids = (
            [str(x) for x in (rec.get("related_problem_ids") or []) if x]
            if rec
            else ([str(p_flex["id"])] if p_flex and p_flex.get("id") else [])
        )
        if not pids and p_flex and p_flex.get("id"):
            pids = [str(p_flex["id"])]
        actions.append(
            {
                "id": "manager_action_4",
                "rank": 4,
                "title": f"I would use {labor_name} more flexibly.",
                "description": (
                    "I would improve assignment, sequencing, or lot policy using existing "
                    "resources before escalating to capital or staffing changes."
                ),
                "action_intent": "rebalance_labor",
                "impact_area": {
                    "id": labor_name if labor_rows else "factory",
                    "name": labor_name if labor_rows else "Factory",
                    "type": "labor" if labor_rows else "factory",
                },
                "related_problem_ids": pids,
                "related_recommendation_ids": (
                    [str(rec["id"])] if rec and rec.get("id") else _rec_ids_for_problems(pids, recommendations)
                ),
                "related_business_impact_ids": _impact_ids_for_problems(pids, impacts),
                "time_horizon": "near_term",
                "confidence": 0.75,
                "evidence_keys": _evidence_for(
                    [p for p in problems if str(p.get("id")) in pids] or problems[:1],
                    catalog,
                ),
            }
        )

    # 5. Validate / monitor emerging constraint
    emerging = None
    eq_list = [e for e in (evidence.get("equipment") or []) if isinstance(e, dict)]
    if len(eq_list) >= 2:
        ranked = sorted(eq_list, key=lambda e: float(e.get("utilization") or 0), reverse=True)
        emerging = ranked[1]
    emerge_name = str(
        (emerging or {}).get("display_name")
        or (emerging or {}).get("equipment_name")
        or (problems[-1].get("area") if problems else "the next constraint")
    )
    p_mon = problems[-1] if problems else None
    if p_mon or recommendations:
        pids = [str(p_mon["id"])] if p_mon and p_mon.get("id") else (
            [str(problems[0]["id"])] if problems and problems[0].get("id") else []
        )
        actions.append(
            {
                "id": "manager_action_5",
                "rank": 5,
                "title": f"Finally, I would monitor {emerge_name} and re-run the scenario.",
                "description": (
                    f"I would watch {emerge_name} as the next emerging constraint and "
                    "re-run the scenario after the earlier changes to confirm directional improvement."
                ),
                "action_intent": "validate_scenario",
                "impact_area": {
                    "id": emerge_name,
                    "name": emerge_name,
                    "type": "work_center",
                },
                "related_problem_ids": pids,
                "related_recommendation_ids": _rec_ids_for_problems(pids, recommendations),
                "related_business_impact_ids": _impact_ids_for_problems(pids, impacts),
                "time_horizon": "near_term",
                "confidence": 0.78,
                "evidence_keys": _evidence_for(
                    [p for p in problems if str(p.get("id")) in pids] or problems[:1],
                    catalog,
                ),
            }
        )

    # Deduplicate by intent+area while keeping order
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for action in actions:
        key = (
            str(action.get("action_intent") or ""),
            str((action.get("impact_area") or {}).get("id") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(action)

    for idx, action in enumerate(unique[:5], start=1):
        action["rank"] = idx
        action["id"] = f"manager_action_{idx}"

    status = "complete" if len(unique) >= 5 else "limited_actions"
    overview = (
        "Based on the validated constraints, I would stabilize the primary bottleneck first, "
        "control nearby queues, then validate flow improvements before any capital or hiring decision."
    )
    final_summary = (
        "These steps should reduce avoidable waiting, improve flow stability, "
        "and support better capacity decisions."
    )
    return {
        "analysis_id": analysis_id,
        "playbook_status": status,
        "overview": overview,
        "action_count": len(unique[:5]),
        "manager_actions": unique[:5],
        "final_summary": final_summary,
        "source": "deterministic_fallback",
    }
