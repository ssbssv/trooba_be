"""Deterministic factory-analysis fallback when the AI provider fails."""
from __future__ import annotations

from typing import Any

from .ranker import rank_top_problems


def _metric(catalog: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    entry = catalog.get(key)
    if not entry:
        return None
    return {
        "metric_key": key,
        "label": entry.get("label") or key,
        "value": entry.get("value", 0),
        "formatted_value": entry.get("formatted_value") or str(entry.get("value", 0)),
        "unit": entry.get("unit") or "",
        "source": "simulation_result",
    }


def _side_box(catalog: dict[str, dict[str, Any]], key: str, *, prefix: str, description: str):
    entry = catalog.get(key) or {}
    return {
        "prefix": prefix,
        "subject": entry.get("label") or key,
        "value": entry.get("formatted_value") or "",
        "description": description,
        "metric_key": key,
    }


def build_deterministic_analysis(normalized: dict[str, Any]) -> dict[str, Any]:
    catalog = normalized.get("metric_catalog") or {}
    products = normalized.get("products") or []
    equipment = normalized.get("equipment") or []
    labor = normalized.get("labor") or []
    warnings = normalized.get("warnings") or []
    candidates: list[dict[str, Any]] = []

    # Highest MCT with dominant bad waiting component
    for product in products:
        if not isinstance(product, dict):
            continue
        mct = product.get("mct") or {}
        total = float(mct.get("total") or 0)
        if total <= 0:
            continue
        waits = {
            "wait_equipment": float(mct.get("wait_equipment") or 0),
            "wait_labor": float(mct.get("wait_labor") or 0),
            "wait_lot": float(mct.get("wait_lot") or 0),
        }
        dominant = max(waits, key=waits.get)
        if waits[dominant] <= 0:
            continue
        pid = product.get("product_id") or product.get("product_name")
        pname = (
            product.get("display_name")
            or product.get("product_name")
            or pid
        )
        # Never put a UUID into user-facing text.
        if isinstance(pname, str) and len(pname) == 36 and pname.count("-") == 4:
            # Last resort: skip narrative that can't be labeled cleanly
            continue
        if dominant == "wait_equipment":
            key = f"product_mct.{pname}.wait_equipment"
        elif dominant == "wait_labor":
            key = f"product_mct.{pname}.wait_labor"
        else:
            key = f"product_mct.{pname}.wait_lot"
        # Also try id-keyed catalog for older payloads
        metric = _metric(catalog, key) or _metric(
            catalog,
            key.replace(f".{pname}.", f".{pid}.") if pid else key,
        )
        total_metric = _metric(catalog, f"product_mct.{pname}.total") or _metric(
            catalog, f"product_mct.{pid}.total"
        )
        if not metric or not total_metric:
            continue
        label = {
            "wait_equipment": "equipment waiting",
            "wait_labor": "labor waiting",
            "wait_lot": "lot waiting",
        }[dominant]
        score = min(
            100.0,
            40.0 + (waits[dominant] / max(total, 1e-9)) * 40.0 + min(total, 20.0),
        )
        candidates.append(
            {
                "problem": f"{pname} elevated {label}",
                "summary": (
                    f"{pname} Manufacturing Cycle Time is elevated, driven mainly by {label}."
                ),
                "root_cause": (
                    f"Non-value-added {label} is the largest contributor to {pname} cycle time "
                    f"in the current simulation."
                ),
                "severity": "high" if waits[dominant] / max(total, 1e-9) >= 0.45 else "medium",
                "category": "high_mct",
                "area": str(pname),
                "problem_score": score,
                "detailed_reasoning": (
                    f"The simulation shows {pname} with Manufacturing Cycle Time of "
                    f"{total_metric['formatted_value']}. The dominant non-value-added component "
                    f"is {label} at {metric['formatted_value']}. Setup and run time are treated "
                    f"as productive unless a benchmark proves otherwise. This waiting pattern "
                    f"indicates flow delay for {pname} and related operations. The finding is "
                    f"based only on calculated MCT components and related resource pressure "
                    f"present in the result set."
                ),
                "why_top_four": f"{pname} has one of the strongest non-value-added waiting signals.",
                "affected_products": [str(pname)],
                "affected_operations": list(product.get("routing") or [])[:8],
                "affected_resources": list(
                    (product.get("related_equipment") or [])
                    + (product.get("related_labor") or [])
                )[:8],
                "supporting_metrics": [total_metric, metric],
                "side_box": _side_box(
                    catalog,
                    metric["metric_key"],
                    prefix="Primary pressure",
                    description=f"Dominant {label} inside {pname} Manufacturing Cycle Time.",
                ),
                "confidence": 0.72,
                "evidence_keys": [total_metric["metric_key"], metric["metric_key"]],
            }
        )

    # Equipment pressure: high util + queue/WIP/wait
    for eq in equipment:
        if not isinstance(eq, dict):
            continue
        name = eq.get("equipment_name") or eq.get("equipment_id")
        util = float(eq.get("utilization") or 0)
        queue = float(eq.get("queue") or 0)
        wip = float(eq.get("wip") or 0)
        if util < 75 and queue <= 0 and wip <= 0:
            continue
        if util < 85 and queue <= 0:
            continue
        util_key = f"equipment.utilization.{name}"
        queue_key = f"equipment.queue.{name}"
        util_m = _metric(catalog, util_key)
        queue_m = _metric(catalog, queue_key)
        if not util_m:
            continue
        score = min(100.0, util * 0.55 + min(queue, 50) * 0.6 + min(wip, 50) * 0.2)
        if score < 55:
            continue
        candidates.append(
            {
                "problem": f"{name} capacity pressure",
                "summary": (
                    f"{name} is operating at high utilization with queue/WIP pressure "
                    f"that can delay routed products."
                ),
                "root_cause": (
                    f"Equipment capacity pressure on {name} is elevating waiting and "
                    f"congestion for products routed through this resource."
                ),
                "severity": "high" if util >= 90 or queue > 5 else "medium",
                "category": "equipment_constraint",
                "area": str(name),
                "problem_score": score,
                "detailed_reasoning": (
                    f"{name} utilization is {util_m['formatted_value']}. "
                    f"{'Queue pressure is also present. ' if queue_m else ''}"
                    f"High utilization alone is not treated as a problem, but the combination "
                    f"with queue or WIP evidence indicates a capacity constraint that can "
                    f"increase Wait for Equipment and Manufacturing Cycle Time for affected "
                    f"products. This deterministic fallback used only simulation metrics."
                ),
                "why_top_four": f"{name} shows strong utilization-plus-queue evidence.",
                "affected_products": list(eq.get("affected_products") or [])[:10],
                "affected_operations": list(eq.get("operations") or [])[:10],
                "affected_resources": [str(name)],
                "supporting_metrics": [m for m in [util_m, queue_m] if m],
                "side_box": _side_box(
                    catalog,
                    util_key,
                    prefix="Primary pressure",
                    description="High utilization supported by queue or WIP pressure.",
                ),
                "confidence": 0.7,
                "evidence_keys": [k for k in [util_key, queue_key] if k in catalog],
            }
        )

    # Labor wait mismatch proxy via product wait_labor + labor util
    for lab in labor:
        if not isinstance(lab, dict):
            continue
        name = lab.get("labor_name") or lab.get("labor_id")
        util = float(lab.get("utilization") or 0)
        queue = float(lab.get("queue") or 0)
        # Find products with high wait_labor related to this labor
        related_wait = 0.0
        related_products = []
        for product in products:
            if not isinstance(product, dict):
                continue
            if name not in (product.get("related_labor") or []):
                continue
            wait_labor = float((product.get("mct") or {}).get("wait_labor") or 0)
            if wait_labor > related_wait:
                related_wait = wait_labor
            if wait_labor > 0:
                related_products.append(
                    str(
                        product.get("display_name")
                        or product.get("product_name")
                        or product.get("product_id")
                    )
                )
        if related_wait <= 0 and queue <= 0:
            continue
        if util >= 85 and related_wait <= 0 and queue <= 0:
            continue
        util_key = f"labor.utilization.{name}"
        util_m = _metric(catalog, util_key)
        if not util_m:
            continue
        score = min(100.0, related_wait * 8 + queue * 2 + (30 if util < 60 and related_wait > 0 else util * 0.2))
        if score < 50:
            continue
        candidates.append(
            {
                "problem": f"{name} labor waiting pressure",
                "summary": (
                    f"Products related to {name} show labor waiting, indicating availability "
                    f"or assignment pressure."
                ),
                "root_cause": (
                    f"Labor waiting for {name}-supported work suggests scheduling, assignment, "
                    f"or capacity mismatch rather than healthy spare capacity alone."
                ),
                "severity": "medium" if util < 90 else "high",
                "category": "labor_constraint",
                "area": str(name),
                "problem_score": score,
                "detailed_reasoning": (
                    f"{name} utilization is {util_m['formatted_value']}. Related products show "
                    f"Wait for Labor signals in the Manufacturing Cycle Time breakdown. "
                    f"Low or moderate utilization with waiting can still indicate mismatch. "
                    f"This fallback finding uses only labor and product waiting evidence."
                ),
                "why_top_four": f"{name} combines labor utilization context with waiting evidence.",
                "affected_products": related_products[:10],
                "affected_operations": list(lab.get("operations") or [])[:10],
                "affected_resources": [str(name)],
                "supporting_metrics": [util_m],
                "side_box": _side_box(
                    catalog,
                    util_key,
                    prefix="Labor signal",
                    description="Labor waiting evidence with utilization context.",
                ),
                "confidence": 0.65,
                "evidence_keys": [util_key],
            }
        )

    if warnings:
        warn_text = str(warnings[0])[:180]
        candidates.append(
            {
                "problem": "Simulation warning requires attention",
                "summary": "The calculation reported a warning that may affect result reliability.",
                "root_cause": warn_text,
                "severity": "low",
                "category": "model_warning",
                "area": "Model warnings",
                "problem_score": 48.0,
                "detailed_reasoning": (
                    f"The simulation produced warning evidence: {warn_text}. "
                    f"This is retained as a monitoring finding when stronger operational "
                    f"candidates are limited. No unsupported claims were added."
                ),
                "why_top_four": "Explicit simulation warnings remain operationally relevant.",
                "affected_products": [],
                "affected_operations": [],
                "affected_resources": [],
                "supporting_metrics": [],
                "side_box": {
                    "prefix": "Warning",
                    "subject": "Simulation warning",
                    "value": "Active",
                    "description": warn_text,
                    "metric_key": next(iter(catalog.keys()), "summary.overall_mct"),
                },
                "confidence": 0.55,
                "evidence_keys": [next(iter(catalog.keys()), "summary.overall_mct")],
            }
        )

    # Ensure side_box metric_key exists; drop invalid
    valid_candidates = []
    for c in candidates:
        key = (c.get("side_box") or {}).get("metric_key")
        if key in catalog or c.get("supporting_metrics"):
            if key not in catalog and c.get("supporting_metrics"):
                c["side_box"]["metric_key"] = c["supporting_metrics"][0]["metric_key"]
                entry = catalog.get(c["side_box"]["metric_key"], {})
                c["side_box"]["value"] = entry.get("formatted_value") or c["side_box"].get("value")
            if c["side_box"]["metric_key"] in catalog:
                valid_candidates.append(c)

    ranked = rank_top_problems(valid_candidates, limit=4)
    if not ranked:
        return {
            "analysis_status": "limited_findings",
            "problem_count": 0,
            "problems": [],
            "business_impacts": [],
            "overall_business_impact": None,
            "source": "deterministic_fallback",
        }

    problem_ids = [p["id"] for p in ranked]
    names = [p["problem"] for p in ranked]
    impacts = [
        {
            "id": "impact_1",
            "rank": 1,
            "title": "Manufacturing Cycle Time is likely to increase",
            "description": (
                "If the identified waiting and capacity pressures remain unresolved, "
                "affected products are likely to spend more time in non-value-added waiting, "
                "which could raise total Manufacturing Cycle Time."
            ),
            "severity": "high",
            "category": "cycle_time",
            "time_horizon": "near_term",
            "related_problem_ids": problem_ids[:2],
            "causal_chain": [
                "Current constraints persist",
                "Waiting time grows",
                "Manufacturing Cycle Time increases",
            ],
            "confidence": 0.7,
            "evidence_keys": ranked[0].get("evidence_keys") or [],
        },
        {
            "id": "impact_2",
            "rank": 2,
            "title": "Queues may continue to build",
            "description": (
                "Unresolved resource pressure may allow queues to keep forming at constrained "
                "operations, increasing congestion for routed products."
            ),
            "severity": "medium",
            "category": "queue_growth",
            "time_horizon": "near_term",
            "related_problem_ids": problem_ids[:3],
            "causal_chain": ["Resource pressure remains", "Arrivals exceed clearance", "Queues grow"],
            "confidence": 0.68,
            "evidence_keys": ranked[0].get("evidence_keys") or [],
        },
        {
            "id": "impact_3",
            "rank": 3,
            "title": "WIP is likely to accumulate",
            "description": (
                "As waiting and queue pressure persist, work-in-process may accumulate around "
                "constrained resources and inspection or downstream handoffs."
            ),
            "severity": "medium",
            "category": "wip",
            "time_horizon": "near_term",
            "related_problem_ids": problem_ids[:3],
            "causal_chain": ["Flow delays persist", "Inventory dwell increases", "WIP rises"],
            "confidence": 0.66,
            "evidence_keys": ranked[0].get("evidence_keys") or [],
        },
        {
            "id": "impact_4",
            "rank": 4,
            "title": "Capacity margin could shrink further",
            "description": (
                "Continuing to operate with the current constraints may leave less spare "
                "capacity, making the factory more sensitive to downtime or demand variation."
            ),
            "severity": "medium",
            "category": "capacity_risk",
            "time_horizon": "medium_term",
            "related_problem_ids": problem_ids,
            "causal_chain": [
                "Constraints remain",
                "Spare capacity declines",
                "Disruption sensitivity rises",
            ],
            "confidence": 0.64,
            "evidence_keys": ranked[0].get("evidence_keys") or [],
        },
        {
            "id": "impact_5",
            "rank": 5,
            "title": "Scheduling complexity is likely to increase",
            "description": (
                "Persistent bottlenecks and waiting patterns could make day-to-day scheduling "
                "and prioritization harder as variability interacts with constrained resources."
            ),
            "severity": "low",
            "category": "scheduling_complexity",
            "time_horizon": "medium_term",
            "related_problem_ids": problem_ids,
            "causal_chain": [
                "Flow instability continues",
                "Prioritization becomes harder",
                "Scheduling complexity rises",
            ],
            "confidence": 0.6,
            "evidence_keys": ranked[0].get("evidence_keys") or [],
        },
    ]

    overall = {
        "title": "Factory flow efficiency is likely to decline",
        "description": (
            "If management continues operating with the current unresolved issues — "
            f"including {', '.join(names[:3])} — non-value-added waiting is likely to persist "
            "across affected products and operations. Manufacturing Cycle Time may continue "
            "rising where equipment or labor waiting dominates, WIP and queues could build "
            "around constrained resources, and available capacity margin may shrink. Over time, "
            "the factory could become more sensitive to demand changes and downtime, with "
            "greater scheduling difficulty and less stable flow. These outcomes are likely "
            "risks based on the current simulation evidence, not guaranteed failures, and no "
            "financial claims are assumed."
        ),
        "severity": "high" if any(p["severity"] == "high" for p in ranked) else "medium",
        "time_horizon": "medium_term",
        "related_problem_ids": problem_ids,
        "confidence": 0.68,
    }

    return {
        "analysis_status": "complete" if len(ranked) >= 4 else "limited_findings",
        "problem_count": len(ranked),
        "problems": ranked,
        "business_impacts": impacts,
        "overall_business_impact": overall,
        "source": "deterministic_fallback",
    }
