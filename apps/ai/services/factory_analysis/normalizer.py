"""
Normalize ScenarioResult.results into a compact AI input payload + metric_catalog.

Implementation notes (Trooba Flow):
- Latest simulation result: ScenarioResult.results (JSON) for the selected Scenario.
- Factory selection: RMCMModel (organization-scoped); frontend modelId == factory_id.
- Model selection: same as factory (one active RMCMModel in the workspace).
- Scenario selection: Trooba AI picker (basecase | scenario uuid).
- Simulation version: resolve_scenario_result_version() hash of scenario + result timestamps.
- Org auth: get_org_context + filter RMCMModel by organization_id.
- AI config: shared LLM_PROVIDER / LLM_MODEL / API keys (same as Factory Briefing).
- UI page: /models/:modelId/trooba-ai — Problems Identified + Business Impact sections.
"""
from __future__ import annotations

import re
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _s(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_uuid(value: str) -> bool:
    text = (value or "").strip()
    if len(text) != 36:
        return False
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            text,
        )
    )


def _display_key(name: str, entity_id: str) -> str:
    """Prefer human-readable name for metric keys and narrative; never prefer UUID."""
    n = _s(name)
    i = _s(entity_id)
    if n and not _looks_like_uuid(n):
        return n
    if i and not _looks_like_uuid(i):
        return i
    return n or i


def _fmt(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value:.2f}%"
    if abs(value) >= 100:
        return f"{value:.1f}"
    if abs(value) >= 10:
        return f"{value:.2f}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _add_metric(
    catalog: dict[str, dict[str, Any]],
    key: str,
    *,
    value: float,
    unit: str,
    label: str,
) -> None:
    catalog[key] = {
        "value": value,
        "unit": unit,
        "label": label,
        "formatted_value": _fmt(value, unit),
    }


def normalize_simulation_for_analysis(
    *,
    factory_id: str,
    factory_name: str,
    model_id: str,
    model_name: str,
    scenario_id: str,
    scenario_name: str,
    simulation_version: str,
    simulation_completed_at: str | None,
    results: dict[str, Any],
    time_unit: str = "days",
) -> dict[str, Any]:
    """
    Build normalized payload from authoritative ScenarioResult.results.

    Field mapping from calc engine:
    - Wait for Equipment → product.mctQueue / w_equip
    - Wait for Labor → product.mctWaitLabor / w_labor
    - Wait for Lot → product.mctLotWait / w_lot
    - Setup / Run → mctSetup / mctRun
    - Equipment utilization → totalUtil
    - Queue / WIP → wip_queue / wip_total
    """
    products_raw = results.get("products") if isinstance(results.get("products"), list) else []
    equipment_raw = results.get("equipment") if isinstance(results.get("equipment"), list) else []
    labor_raw = results.get("labor") if isinstance(results.get("labor"), list) else []
    operations_raw = results.get("operations") if isinstance(results.get("operations"), list) else []
    warnings_raw = results.get("warnings") if isinstance(results.get("warnings"), list) else []
    over_limit = (
        results.get("overLimitResources")
        if isinstance(results.get("overLimitResources"), list)
        else []
    )

    metric_catalog: dict[str, dict[str, Any]] = {}

    products: list[dict[str, Any]] = []
    total_started = 0.0
    total_good = 0.0
    total_scrap = 0.0
    total_shipped = 0.0
    mct_sum = 0.0
    mct_n = 0
    wip_sum = 0.0

    for row in products_raw:
        if not isinstance(row, dict):
            continue
        pid = _s(row.get("id") or row.get("name"))
        if not pid:
            continue
        pname = _s(row.get("name")) or pid
        # Human-readable key for metrics / AI narrative (never expose UUID as the label).
        pkey = _display_key(pname, pid)
        started = _f(row.get("started"))
        good_made = _f(row.get("goodMade"))
        scrap = _f(row.get("scrap"))
        good_shipped = _f(row.get("goodShipped"))
        demand = _f(row.get("demand"))
        mct_total = _f(row.get("mct"))
        wait_eq = _f(row.get("mctQueue", row.get("w_equip")))
        wait_labor = _f(row.get("mctWaitLabor", row.get("w_labor")))
        wait_lot = _f(row.get("mctLotWait", row.get("w_lot")))
        setup = _f(row.get("mctSetup", row.get("w_setup")))
        run = _f(row.get("mctRun", row.get("w_run")))
        wip = _f(row.get("wip"))

        total_started += started
        total_good += good_made
        total_scrap += scrap
        total_shipped += good_shipped
        wip_sum += wip
        if mct_total > 0:
            mct_sum += mct_total
            mct_n += 1

        related_ops = [
            _s(op.get("operation"))
            for op in operations_raw
            if isinstance(op, dict) and _s(op.get("product_id")) == pid and _s(op.get("operation"))
        ]
        related_eq = sorted(
            {
                _display_key(_s(op.get("equipment")), _s(op.get("equip_id")))
                for op in operations_raw
                if isinstance(op, dict)
                and _s(op.get("product_id")) == pid
                and (_s(op.get("equipment")) or _s(op.get("equip_id")))
            }
        )
        related_lab = sorted(
            {
                _display_key(_s(op.get("labor")), _s(op.get("labor_id")))
                for op in operations_raw
                if isinstance(op, dict)
                and _s(op.get("product_id")) == pid
                and (_s(op.get("labor")) or _s(op.get("labor_id")))
            }
        )

        products.append(
            {
                "product_id": pid,
                "product_name": pname if not _looks_like_uuid(pname) else pkey,
                "display_name": pkey,
                "started": started,
                "good_made": good_made,
                "scrap": scrap,
                "good_shipped": good_shipped,
                "demand": demand,
                "mct": {
                    "total": mct_total,
                    "wait_equipment": wait_eq,
                    "wait_labor": wait_labor,
                    "wait_lot": wait_lot,
                    "setup": setup,
                    "run": run,
                },
                "wip": wip,
                "routing": related_ops,
                "related_equipment": related_eq,
                "related_labor": related_lab,
            }
        )

        _add_metric(
            metric_catalog,
            f"product_mct.{pkey}.total",
            value=mct_total,
            unit=time_unit,
            label=f"{pkey} Manufacturing Cycle Time",
        )
        _add_metric(
            metric_catalog,
            f"product_mct.{pkey}.wait_equipment",
            value=wait_eq,
            unit=time_unit,
            label=f"{pkey} Wait for Equipment",
        )
        _add_metric(
            metric_catalog,
            f"product_mct.{pkey}.wait_labor",
            value=wait_labor,
            unit=time_unit,
            label=f"{pkey} Wait for Labor",
        )
        _add_metric(
            metric_catalog,
            f"product_mct.{pkey}.wait_lot",
            value=wait_lot,
            unit=time_unit,
            label=f"{pkey} Wait for Lot",
        )
        _add_metric(
            metric_catalog,
            f"product_mct.{pkey}.setup",
            value=setup,
            unit=time_unit,
            label=f"{pkey} Setup Time",
        )
        _add_metric(
            metric_catalog,
            f"product_mct.{pkey}.run",
            value=run,
            unit=time_unit,
            label=f"{pkey} Run Time",
        )
        _add_metric(
            metric_catalog,
            f"product.wip.{pkey}",
            value=wip,
            unit="units",
            label=f"{pkey} WIP",
        )
        _add_metric(
            metric_catalog,
            f"product.scrap.{pkey}",
            value=scrap,
            unit="units",
            label=f"{pkey} Scrap",
        )
        if demand > 0:
            shortfall = max(0.0, demand - good_shipped)
            _add_metric(
                metric_catalog,
                f"product.shortfall.{pkey}",
                value=shortfall,
                unit="units",
                label=f"{pkey} Production Shortfall",
            )

    equipment: list[dict[str, Any]] = []
    for row in equipment_raw:
        if not isinstance(row, dict):
            continue
        eid = _s(row.get("id") or row.get("name"))
        if not eid:
            continue
        ename = _display_key(_s(row.get("name")), eid)
        util = _f(row.get("totalUtil"))
        queue = _f(row.get("wip_queue"))
        wip = _f(row.get("wip_total"))
        wait_time = _f(row.get("wait_min"))
        available = _f(row.get("num_av", row.get("count")), 1.0)

        ops = sorted(
            {
                _s(op.get("operation"))
                for op in operations_raw
                if isinstance(op, dict)
                and (_s(op.get("equip_id")) == eid or _s(op.get("equipment")) == ename)
                and _s(op.get("operation"))
            }
        )
        affected = sorted(
            {
                _display_key(_s(op.get("product")), _s(op.get("product_id")))
                for op in operations_raw
                if isinstance(op, dict)
                and (_s(op.get("equip_id")) == eid or _s(op.get("equipment")) == ename)
                and (_s(op.get("product")) or _s(op.get("product_id")))
            }
        )

        equipment.append(
            {
                "equipment_id": eid,
                "equipment_name": ename,
                "display_name": ename,
                "utilization": util,
                "configured_limit": None,
                "queue": queue,
                "wip": wip,
                "wait_time": wait_time,
                "operations": ops,
                "affected_products": affected,
                "available_units": available,
            }
        )
        _add_metric(
            metric_catalog,
            f"equipment.utilization.{ename}",
            value=util,
            unit="%",
            label=f"{ename} Utilization",
        )
        _add_metric(
            metric_catalog,
            f"equipment.queue.{ename}",
            value=queue,
            unit="units",
            label=f"{ename} Queue",
        )
        _add_metric(
            metric_catalog,
            f"equipment.wip.{ename}",
            value=wip,
            unit="units",
            label=f"{ename} WIP",
        )

    labor: list[dict[str, Any]] = []
    for row in labor_raw:
        if not isinstance(row, dict):
            continue
        lid = _s(row.get("id") or row.get("name"))
        if not lid:
            continue
        lname = _display_key(_s(row.get("name")), lid)
        util = _f(row.get("totalUtil"))
        queue = _f(row.get("wip_queue"))
        wip = _f(row.get("wip_total"))
        available = _f(row.get("count"), 1.0)

        ops = sorted(
            {
                _s(op.get("operation"))
                for op in operations_raw
                if isinstance(op, dict)
                and (_s(op.get("labor_id")) == lid or _s(op.get("labor")) == lname)
                and _s(op.get("operation"))
            }
        )
        affected = sorted(
            {
                _display_key(_s(op.get("product")), _s(op.get("product_id")))
                for op in operations_raw
                if isinstance(op, dict)
                and (_s(op.get("labor_id")) == lid or _s(op.get("labor")) == lname)
                and (_s(op.get("product")) or _s(op.get("product_id")))
            }
        )

        labor.append(
            {
                "labor_id": lid,
                "labor_name": lname,
                "display_name": lname,
                "utilization": util,
                "configured_limit": None,
                "queue": queue,
                "wip": wip,
                "wait_time": 0.0,
                "operations": ops,
                "affected_products": affected,
                "available_units": available,
            }
        )
        _add_metric(
            metric_catalog,
            f"labor.utilization.{lname}",
            value=util,
            unit="%",
            label=f"{lname} Utilization",
        )
        _add_metric(
            metric_catalog,
            f"labor.queue.{lname}",
            value=queue,
            unit="units",
            label=f"{lname} Queue",
        )
        _add_metric(
            metric_catalog,
            f"labor.wip.{lname}",
            value=wip,
            unit="units",
            label=f"{lname} WIP",
        )

    # Aggregate operations by operation name for queue/WIP style signals
    op_agg: dict[str, dict[str, Any]] = {}
    for row in operations_raw:
        if not isinstance(row, dict):
            continue
        oid = _s(row.get("operation"))
        if not oid:
            continue
        bucket = op_agg.setdefault(
            oid,
            {
                "operation_id": oid,
                "operation_name": oid,
                "queue": 0.0,
                "wip": 0.0,
                "wait_equipment": 0.0,
                "wait_labor": 0.0,
                "wait_lot": 0.0,
                "equipment": set(),
                "labor": set(),
                "affected_products": set(),
            },
        )
        bucket["wip"] += _f(row.get("qpoper"))
        bucket["wait_equipment"] += _f(row.get("w_equip"))
        bucket["wait_labor"] += _f(row.get("w_labor"))
        bucket["wait_lot"] += _f(row.get("w_lot"))
        if _s(row.get("equipment")) or _s(row.get("equip_id")):
            bucket["equipment"].add(
                _display_key(_s(row.get("equipment")), _s(row.get("equip_id")))
            )
        if _s(row.get("labor")) or _s(row.get("labor_id")):
            bucket["labor"].add(
                _display_key(_s(row.get("labor")), _s(row.get("labor_id")))
            )
        if _s(row.get("product")) or _s(row.get("product_id")):
            bucket["affected_products"].add(
                _display_key(_s(row.get("product")), _s(row.get("product_id")))
            )

    operations: list[dict[str, Any]] = []
    for oid, bucket in op_agg.items():
        ops_row = {
            "operation_id": oid,
            "operation_name": oid,
            "queue": round(bucket["wip"], 4),
            "wip": round(bucket["wip"], 4),
            "wait_equipment": round(bucket["wait_equipment"], 4),
            "wait_labor": round(bucket["wait_labor"], 4),
            "wait_lot": round(bucket["wait_lot"], 4),
            "equipment": sorted(bucket["equipment"]),
            "labor": sorted(bucket["labor"]),
            "affected_products": sorted(bucket["affected_products"]),
        }
        operations.append(ops_row)
        _add_metric(
            metric_catalog,
            f"operation.wip.{oid}",
            value=ops_row["wip"],
            unit="units",
            label=f"{oid} Operation WIP",
        )
        _add_metric(
            metric_catalog,
            f"operation.wait_equipment.{oid}",
            value=ops_row["wait_equipment"],
            unit=time_unit,
            label=f"{oid} Wait for Equipment",
        )

    target_shortfalls = []
    for p in products:
        demand = _f(p.get("demand"))
        shipped = _f(p.get("good_shipped"))
        if demand > 0 and shipped + 1e-9 < demand:
            target_shortfalls.append(
                {
                    "product_id": p["product_id"],
                    "product_name": p.get("display_name") or p.get("product_name") or p["product_id"],
                    "demand": demand,
                    "good_shipped": shipped,
                    "shortfall": max(0.0, demand - shipped),
                }
            )

    scrap_by_product = [
        {
            "product_id": p["product_id"],
            "product_name": p.get("display_name") or p.get("product_name") or p["product_id"],
            "scrap": _f(p.get("scrap")),
        }
        for p in products
        if _f(p.get("scrap")) > 0
    ]

    # Map raw UUIDs → display names so AI/fallback text never shows IDs to users.
    entity_aliases: dict[str, str] = {}
    for p in products:
        display = _s(p.get("display_name") or p.get("product_name"))
        pid = _s(p.get("product_id"))
        if display and pid and pid != display:
            entity_aliases[pid] = display
        if display:
            entity_aliases[display] = display
    for e in equipment:
        display = _s(e.get("display_name") or e.get("equipment_name"))
        eid = _s(e.get("equipment_id"))
        if display and eid and eid != display:
            entity_aliases[eid] = display
    for lab in labor:
        display = _s(lab.get("display_name") or lab.get("labor_name"))
        lid = _s(lab.get("labor_id"))
        if display and lid and lid != display:
            entity_aliases[lid] = display

    overall_mct = round(mct_sum / mct_n, 4) if mct_n else 0.0
    _add_metric(
        metric_catalog,
        "summary.overall_mct",
        value=overall_mct,
        unit=time_unit,
        label="Overall Manufacturing Cycle Time",
    )
    _add_metric(
        metric_catalog,
        "summary.overall_wip",
        value=wip_sum,
        unit="units",
        label="Overall WIP",
    )

    warnings = [str(w) for w in warnings_raw if str(w).strip()]
    for item in over_limit:
        text = str(item).strip()
        if text and text not in warnings:
            warnings.append(text)

    return {
        "analysis_context": {
            "factory_id": factory_id,
            "factory_name": factory_name,
            "model_id": model_id,
            "model_name": model_name,
            "scenario_id": scenario_id,
            "scenario_name": scenario_name,
            "simulation_version": simulation_version,
            "simulation_completed_at": simulation_completed_at or "",
            "unit_system": {
                "time": time_unit,
                "quantity": "units",
                "utilization": "%",
            },
        },
        "summary": {
            "started": total_started,
            "good_made": total_good,
            "scrap": total_scrap,
            "good_shipped": total_shipped,
            "overall_mct": overall_mct,
            "overall_wip": wip_sum,
        },
        "products": products,
        "equipment": equipment,
        "labor": labor,
        "operations": operations,
        "production": {
            "target_shortfalls": target_shortfalls,
            "throughput_constraints": [],
        },
        "quality": {
            "scrap_by_product": scrap_by_product,
            "scrap_by_operation": [],
            "rework": [],
        },
        "warnings": warnings,
        "entity_aliases": entity_aliases,
        "metric_catalog": metric_catalog,
    }
