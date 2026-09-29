"""
Full calculation engine — mirrors frontend calculationEngine.calculate().
Accepts model + scenario (JSON-serializable dicts), returns CalcResults-shaped dict.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def _sanitize(v: float) -> float:
    return v if (abs(v) != float("inf")) else 0.0


def _round1(x: float) -> float:
    return round(x * 10) / 10


def _round4(x: float) -> float:
    return round(x * 10000) / 10000


def apply_scenario(
    model: Dict[str, Any],
    scenario: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
    """Apply what-if scenario changes to a copy of the model."""

    if not scenario or not scenario.get("changes"):
        return copy.deepcopy(model)

    updated_model = copy.deepcopy(model)

    for key in (
        "labor",
        "equipment",
        "products",
        "operations",
        "routing",
    ):
        updated_model[key] = [
            dict(item)
            for item in updated_model.get(key, [])
        ]

    for change in scenario["changes"]:
        _apply_change(
            updated_model,
            change,
        )

    return updated_model

def _find_entity(items: list[dict], entity_id: str):
    for item in items:
        if item.get("id") == entity_id:
            return item
    return None

def _apply_product_change(
    products: list[dict],
    entity_id: str,
    field: str,
    what_if: Any,
) -> None:
    product = _find_entity(products, entity_id)

    if product is None:
        return

    if field == "included" and str(what_if).lower() == "false":
        product["demand"] = 0
        return

    product[field] = what_if

def _apply_routing_change(
    routing: list[dict],
    entity_id: str,
    field: str,
    what_if: Any,
) -> None:
    route = _find_entity(routing, entity_id)

    if route is None:
        return

    route[field] = float(what_if) if what_if is not None else 0

def _apply_change(model: dict, change: dict) -> None:
    data_type = change.get("dataType")
    entity_id = change.get("entityId")
    field = change.get("field")
    what_if = change.get("whatIfValue")

    if data_type == "Labor":
        labor = _find_entity(model["labor"], entity_id)
        if labor:
            labor[field] = what_if

    elif data_type == "Equipment":
        equipment = _find_entity(model["equipment"], entity_id)
        if equipment:
            equipment[field] = what_if

    elif data_type == "Product":
        _apply_product_change(
            model["products"],
            entity_id,
            field,
            what_if,
        )

    elif data_type == "Routing":
        _apply_routing_change(
            model["routing"],
            entity_id,
            field,
            what_if,
        )

    elif (
        data_type == "Product Inclusion"
        and what_if == "No"
    ):
        product = _find_entity(
            model["products"],
            entity_id,
        )

        if product:
            product["demand"] = 0



def compute_effective_demand(
    products: List[Dict], ibom: List[Dict]
) -> Dict[str, float]:
    """IBOM-driven demand: total demand per product including component demand from parents."""
    children: Dict[str, List[Dict[str, Any]]] = {}
    for entry in ibom:
        pid = entry.get("parent_product_id")
        if pid not in children:
            children[pid] = []
        children[pid].append({
            "componentId": entry.get("component_product_id"),
            "unitsPerAssy": float(entry.get("units_per_assy", 1)),
        })

    demand: Dict[str, float] = {}
    for p in products:
        d = float(p.get("demand", 0)) * float(p.get("demand_factor", 1))
        demand[p["id"]] = d

    visited = set()
    order: List[str] = []

    def visit(pid: str) -> None:
        if pid in visited:
            return
        visited.add(pid)
        for k in children.get(pid, []):
            visit(k["componentId"])
        order.append(pid)

    for p in products:
        visit(p["id"])
    order.reverse()

    for parent_id in order:
        parent_demand = demand.get(parent_id, 0)
        for k in children.get(parent_id, []):
            cid = k["componentId"]
            prev = demand.get(cid, 0)
            demand[cid] = prev + parent_demand * k["unitsPerAssy"]

    return demand


def full_calculate(model: Dict[str, Any], scenario: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Full RMT calculation: equipment/labor utilization, product MCT, WIP, queue times.
    Returns a dict matching frontend CalcResults: equipment, labor, products, warnings, errors, overLimitResources, calculatedAt.
    """
    from datetime import datetime

    m = apply_scenario(model, scenario)
    g = m.get("general", {})
    warnings: List[str] = []
    errors: List[str] = []

    conv1 = max(float(g.get("conv1", 480)), 0.001)
    conv2 = max(float(g.get("conv2", 210)), 0.001)
    ops_per_period = conv1 * conv2

    effective_demand = compute_effective_demand(
        m.get("products", []), m.get("ibom", [])
    )

    util_limit = float(g.get("util_limit", 85))
    var_equip = float(g.get("var_equip", 0)) / 100
    var_prod = float(g.get("var_prod", 0)) / 100

    # —— Equipment utilization ——
    equip_results: List[Dict[str, Any]] = []
    equip_util_map: Dict[str, float] = {}
    labor_by_id = {x["id"]: x for x in m.get("labor", [])}

    for eq in m.get("equipment", []):
        eq_id = eq.get("id", "")
        eq_name = eq.get("name", "")
        eq_count = int(eq.get("count", 0))
        is_delay = eq.get("equip_type") == "delay"
        count = 1 if is_delay else eq_count

        if count <= 0 and not is_delay:
            equip_results.append({
                "id": eq_id, "name": eq_name, "count": eq_count,
                "setupUtil": 0, "runUtil": 0, "repairUtil": 0, "waitLaborUtil": 0,
                "totalUtil": 0, "idle": 100, "laborGroup": "",
            })
            equip_util_map[eq_id] = 0.0
            continue

        overtime = 1 + float(eq.get("overtime_pct", 0)) / 100
        unavail = 1 - float(eq.get("unavail_pct", 0)) / 100
        avail_time = count * overtime * unavail * ops_per_period

        mttf = float(eq.get("mttf", 0))
        mttr = float(eq.get("mttr", 0))
        repair_fraction = mttr / (mttf + mttr) if (mttf > 0 and mttr > 0) else 0
        effective_avail = avail_time * (1 - repair_fraction)

        total_setup = 0.0
        total_run = 0.0

        for op in m.get("operations", []):
            if op.get("equip_id") != eq_id:
                continue
            product = next((p for p in m["products"] if p.get("id") == op.get("product_id")), None)
            if not product:
                continue
            demand = effective_demand.get(product["id"], 0) or 0
            if demand <= 0:
                continue

            lot_size = max(1, float(product.get("lot_size", 1)) * float(product.get("lot_factor", 1)))
            tbatch = float(product.get("tbatch_size", -1))
            tbatch_size = lot_size if tbatch == -1 else max(1, tbatch)
            num_tbatches = (lot_size + tbatch_size - 1) // tbatch_size if tbatch_size else 1
            assign_frac = float(op.get("pct_assigned", 0)) / 100
            num_lots = (demand / lot_size) * assign_frac

            prod_setup_factor = float(product.get("setup_factor", 1))
            setup_per_lot = (
                float(op.get("equip_setup_lot", 0))
                + float(op.get("equip_setup_piece", 0)) * lot_size
                + float(op.get("equip_setup_tbatch", 0)) * num_tbatches
            ) * float(eq.get("setup_factor", 1)) * prod_setup_factor
            run_per_lot = (
                float(op.get("equip_run_piece", 0)) * lot_size
                + float(op.get("equip_run_lot", 0))
                + float(op.get("equip_run_tbatch", 0)) * num_tbatches
            ) * float(eq.get("run_factor", 1))

            total_setup += num_lots * setup_per_lot
            total_run += num_lots * run_per_lot

        setup_util = (total_setup / effective_avail * 100) if effective_avail > 0 else 0
        run_util = (total_run / effective_avail * 100) if effective_avail > 0 else 0
        repair_util = repair_fraction * 100
        labor = labor_by_id.get(eq.get("labor_group_id") or "")
        labor_name = labor.get("name", "") if labor else ""

        equip_results.append({
            "id": eq_id, "name": eq_name, "count": eq_count,
            "setupUtil": _round1(setup_util), "runUtil": _round1(run_util),
            "repairUtil": _round1(repair_util), "waitLaborUtil": 0,
            "totalUtil": 0, "idle": 0, "laborGroup": labor_name,
        })
        equip_util_map[eq_id] = (setup_util + run_util + repair_util) / 100

    # —— Labor utilization ——
    labor_results: List[Dict[str, Any]] = []
    labor_util_map: Dict[str, float] = {}
    equipment_list = m.get("equipment", [])

    for lab in m.get("labor", []):
        lab_id = lab.get("id", "")
        lab_name = lab.get("name", "")
        lab_count = int(lab.get("count", 0))
        unavail_pct = float(lab.get("unavail_pct", 0))

        if lab_count <= 0:
            labor_results.append({
                "id": lab_id, "name": lab_name, "count": lab_count,
                "setupUtil": 0, "runUtil": 0, "unavailPct": unavail_pct,
                "totalUtil": unavail_pct, "idle": 100 - unavail_pct,
            })
            labor_util_map[lab_id] = 0.0
            continue

        overtime = 1 + float(lab.get("overtime_pct", 0)) / 100
        unavail_factor = 1 - unavail_pct / 100
        avail_time = lab_count * overtime * unavail_factor * ops_per_period

        total_setup = 0.0
        total_run = 0.0
        for op in m.get("operations", []):
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq or eq.get("labor_group_id") != lab_id:
                continue
            product = next((p for p in m["products"] if p.get("id") == op.get("product_id")), None)
            if not product:
                continue
            demand = effective_demand.get(product["id"], 0) or 0
            if demand <= 0:
                continue

            lot_size = max(1, float(product.get("lot_size", 1)) * float(product.get("lot_factor", 1)))
            tbatch = float(product.get("tbatch_size", -1))
            tbatch_size = lot_size if tbatch == -1 else max(1, tbatch)
            num_tbatches = (lot_size + tbatch_size - 1) // tbatch_size if tbatch_size else 1
            assign_frac = float(op.get("pct_assigned", 0)) / 100
            num_lots = (demand / lot_size) * assign_frac

            prod_setup_factor = float(product.get("setup_factor", 1))
            setup_per_lot = (
                float(op.get("labor_setup_lot", 0))
                + float(op.get("labor_setup_piece", 0)) * lot_size
                + float(op.get("labor_setup_tbatch", 0)) * num_tbatches
            ) * float(lab.get("setup_factor", 1)) * prod_setup_factor
            run_per_lot = (
                float(op.get("labor_run_piece", 0)) * lot_size
                + float(op.get("labor_run_lot", 0))
                + float(op.get("labor_run_tbatch", 0)) * num_tbatches
            ) * float(lab.get("run_factor", 1))

            total_setup += num_lots * setup_per_lot
            total_run += num_lots * run_per_lot

        setup_util = (total_setup / avail_time * 100) if avail_time > 0 else 0
        run_util = (total_run / avail_time * 100) if avail_time > 0 else 0
        work_util = setup_util + run_util
        labor_util_map[lab_id] = work_util / 100
        labor_results.append({
            "id": lab_id, "name": lab_name, "count": lab_count,
            "setupUtil": _round1(setup_util), "runUtil": _round1(run_util),
            "unavailPct": unavail_pct,
            "totalUtil": _round1(work_util + unavail_pct),
            "idle": _round1(max(0, 100 - work_util - unavail_pct)),
        })

    # —— Wait-for-labor on equipment ——
    for er in equip_results:
        eq = next((e for e in equipment_list if e.get("id") == er["id"]), None)
        if not eq or not eq.get("labor_group_id"):
            continue
        labor_util = labor_util_map.get(eq["labor_group_id"], 0) or 0
        safe_lu = min(labor_util, 0.98)
        base_util = er["setupUtil"] + er["runUtil"]
        wfl = (safe_lu * safe_lu / (1 - safe_lu)) * (base_util / 100) * 15 if safe_lu > 0 else 0
        er["waitLaborUtil"] = _round1(min(wfl, 30))
        er["totalUtil"] = _round1(er["setupUtil"] + er["runUtil"] + er["repairUtil"] + er["waitLaborUtil"])
        er["idle"] = _round1(max(0, 100 - er["totalUtil"]))
        equip_util_map[er["id"]] = er["totalUtil"] / 100

    # —— Product MCT and WIP ——
    product_results: List[Dict[str, Any]] = []
    operations_list = m.get("operations", [])
    routing_list = m.get("routing", [])

    for product in m.get("products", []):
        pid = product.get("id", "")
        pname = product.get("name", "")
        demand = effective_demand.get(pid, 0) or 0
        lot_size = max(1, float(product.get("lot_size", 1)) * float(product.get("lot_factor", 1)))
        tbatch = float(product.get("tbatch_size", -1))
        tbatch_size = lot_size if tbatch == -1 else max(1, tbatch)
        demand_end = float(product.get("demand", 0)) * float(product.get("demand_factor", 1))

        ops = [o for o in operations_list if o.get("product_id") == pid]

        if not ops or demand <= 0:
            product_results.append({
                "id": pid, "name": pname, "demand": demand, "lotSize": lot_size,
                "goodMade": round(demand), "goodShipped": round(demand_end),
                "started": round(demand), "scrap": 0, "wip": 0, "mct": 0,
                "mctLotWait": 0, "mctQueue": 0, "mctWaitLabor": 0, "mctSetup": 0, "mctRun": 0,
            })
            continue

        total_setup_mct = 0.0
        total_run_mct = 0.0
        total_queue_mct = 0.0
        total_lot_wait_mct = 0.0
        total_wait_labor_mct = 0.0
        total_scrap_fraction = 0.0

        for op in ops:
            eq = next((e for e in equipment_list if e.get("id") == op.get("equip_id")), None)
            if not eq:
                continue
            assign_frac = float(op.get("pct_assigned", 0)) / 100
            if assign_frac <= 0:
                continue

            num_tbatches = (lot_size + tbatch_size - 1) // tbatch_size if tbatch_size else 1
            prod_setup_factor = float(product.get("setup_factor", 1))
            setup_per_lot = (
                float(op.get("equip_setup_lot", 0))
                + float(op.get("equip_setup_piece", 0)) * lot_size
                + float(op.get("equip_setup_tbatch", 0)) * num_tbatches
            ) * float(eq.get("setup_factor", 1)) * prod_setup_factor
            run_per_lot = (
                float(op.get("equip_run_piece", 0)) * lot_size
                + float(op.get("equip_run_lot", 0))
                + float(op.get("equip_run_tbatch", 0)) * num_tbatches
            ) * float(eq.get("run_factor", 1))

            setup_time = setup_per_lot / lot_size
            run_time = run_per_lot / lot_size
            setup_mct = (setup_time / conv1) * assign_frac
            run_mct = (run_time / conv1) * assign_frac
            total_setup_mct += setup_mct
            total_run_mct += run_mct

            if product.get("gather_tbatches") and tbatch_size < lot_size:
                lot_wait = ((lot_size - tbatch_size) * run_time) / conv1 * assign_frac
                total_lot_wait_mct += lot_wait

            equip_util = equip_util_map.get(eq["id"], 0) or 0
            safe_util = min(equip_util, 0.99)
            if safe_util > 0 and eq.get("equip_type") != "delay":
                ca2 = var_prod * var_prod * float(product.get("var_factor", 1)) ** 2
                cs2 = var_equip * var_equip * float(eq.get("var_factor", 1)) ** 2
                mean_service = (setup_time + run_time) / conv1
                queue_time = ((ca2 + cs2) / 2) * (safe_util / (1 - safe_util)) * mean_service * assign_frac
                total_queue_mct += max(0, queue_time)

            if eq.get("labor_group_id"):
                labor_util = labor_util_map.get(eq["labor_group_id"], 0) or 0
                safe_lu = min(labor_util, 0.98)
                if safe_lu > 0:
                    labor_wait = (safe_lu * safe_lu / (1 - safe_lu)) * (run_time / conv1) * 0.5 * assign_frac
                    total_wait_labor_mct += max(0, labor_wait)

        for r in routing_list:
            if r.get("product_id") == pid and r.get("to_op_name") == "SCRAP":
                total_scrap_fraction += float(r.get("pct_routed", 0)) / 100
        scrap_rate = min(total_scrap_fraction, 0.5)

        # Started, GoodMade, Scrap, GoodShipped, WIP
        safe_rate = min(max(scrap_rate, 0), 0.99)
        started = round(demand / (1 - safe_rate)) if safe_rate > 0 else round(demand)
        good_made = round(started * (1 - safe_rate))
        scrap = max(0, round(started - good_made))
        good_shipped = round(min(good_made, demand_end))
        wip = max(0, round(started - good_shipped - scrap))

        total_mct = total_setup_mct + total_run_mct + total_queue_mct + total_lot_wait_mct + total_wait_labor_mct

        product_results.append({
            "id": pid, "name": pname, "demand": demand, "lotSize": lot_size,
            "goodMade": good_made, "goodShipped": good_shipped,
            "started": started, "scrap": scrap, "wip": wip,
            "mct": _round4(total_mct),
            "mctLotWait": _round4(total_lot_wait_mct),
            "mctQueue": _round4(total_queue_mct),
            "mctWaitLabor": _round4(total_wait_labor_mct),
            "mctSetup": _round4(total_setup_mct),
            "mctRun": _round4(total_run_mct),
        })

    # —— Over-limit warnings ——
    over_limit: List[str] = []
    for er in equip_results:
        if er["totalUtil"] > util_limit:
            over_limit.append(f"Equipment: {er['name']} ({er['totalUtil']}%)")
            warnings.append(f'Equipment group "{er["name"]}" utilization ({er["totalUtil"]}%) exceeds limit ({util_limit}%)')
    for lr in labor_results:
        if lr["totalUtil"] > util_limit:
            over_limit.append(f"Labor: {lr['name']} ({lr['totalUtil']}%)")
            warnings.append(f'Labor group "{lr["name"]}" utilization ({lr["totalUtil"]}%) exceeds limit ({util_limit}%)')

    if not m.get("operations"):
        errors.append("No operations defined. Add operations to products before running calculations.")

    # Sanitize
    for e in equip_results:
        e["setupUtil"] = _sanitize(e["setupUtil"])
        e["runUtil"] = _sanitize(e["runUtil"])
        e["repairUtil"] = _sanitize(e["repairUtil"])
        e["waitLaborUtil"] = _sanitize(e["waitLaborUtil"])
        e["totalUtil"] = _sanitize(e["totalUtil"])
        e["idle"] = _sanitize(e["idle"])
    for l in labor_results:
        l["setupUtil"] = _sanitize(l["setupUtil"])
        l["runUtil"] = _sanitize(l["runUtil"])
        l["totalUtil"] = _sanitize(l["totalUtil"])
        l["idle"] = _sanitize(l["idle"])
    for p in product_results:
        p["wip"] = _sanitize(p["wip"])
        p["mct"] = _sanitize(p["mct"])
        p["mctLotWait"] = _sanitize(p["mctLotWait"])
        p["mctQueue"] = _sanitize(p["mctQueue"])
        p["mctWaitLabor"] = _sanitize(p["mctWaitLabor"])
        p["mctSetup"] = _sanitize(p["mctSetup"])
        p["mctRun"] = _sanitize(p["mctRun"])

    return {
        "equipment": equip_results,
        "labor": labor_results,
        "products": product_results,
        "warnings": warnings,
        "errors": errors,
        "overLimitResources": over_limit,
        "calculatedAt": datetime.now(UTC).isoformat(),
    }
