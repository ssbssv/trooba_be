"""
Simulation helpers and (optional) API endpoints for RMCT calculations.

Currently the main queuing calculations run in the frontend
(`frontend/src/lib/calculationEngine.ts`). This module provides matching
Python helpers that implement the core product-level formulas so they can
be reused from Django views or background tasks if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt

import json


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


@dataclass
class OperationInput:
    """Minimal per-operation data needed for MCT-style calculations."""

    cycle_time: float       # time per piece (or unit basis for batch)
    batch: float            # batch size for the operation
    utilization: float      # 0–1
    scrap_rate: float = 0.0 # 0–1, scrap at this operation


def calculate_started(demand: float, bom_qty: float | None = None, is_final_product: bool = False) -> float:
    """
    Started = Demand × BOM_Quantity
    If the part is a final product (no parent in BOM), Started = Demand.
    """
    if is_final_product or bom_qty is None:
        return float(demand)
    return float(demand) * float(bom_qty)


def calculate_good_made(started: float, scrap_rates: List[float]) -> float:
    """GoodMade = Started × Π (1 − ScrapRate_i)."""
    good = float(started)
    for s in scrap_rates:
        good *= (1.0 - float(s))
    return good


def calculate_scrap(started: float, good_made: float) -> float:
    """TotalScrap = Started − GoodMade."""
    return float(started) - float(good_made)


def calculate_good_shipped(good_made: float, demand: float) -> float:
    """GoodShipped = min(GoodMade, Demand)."""
    return float(min(good_made, demand))


def calculate_wip(started: float, shipped: float, scrap: float) -> float:
    """WIP = Started − GoodShipped − Scrap."""
    return float(started) - float(shipped) - float(scrap)


def calculate_mct(operations: List[OperationInput]) -> float:
    """
    MCT = Σ(ProcessTime_i) + Σ(QueueTime_i)

    ProcessTime_i = CycleTime_i × BatchSize
    QueueTime_i = (Utilization / (1 − Utilization)) × ProcessTime_i   for util < 1
    """
    total = 0.0
    for op in operations:
        process = op.cycle_time * op.batch
        util = op.utilization
        queue = (util / (1.0 - util)) * process if 0.0 <= util < 1.0 else 0.0
        total += process + queue
    return total


def calculate_utilization(demand: float, cycle_time: float, machines: int, available_time: float) -> float:
    """
    Utilization = Load / Capacity
    Load = Demand × CycleTime
    Capacity = Machines × AvailableTime
    """
    load = float(demand) * float(cycle_time)
    capacity = float(machines) * float(available_time)
    return load / capacity if capacity > 0 else 0.0


def calculate_product_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convenience helper for one product row.

    Expected payload shape:
    {
        "product_name": str,
        "demand": float,
        "bom_qty": float | null,
        "is_final_product": bool,
        "scrap_rates": [float, ...],    # decimals, e.g. 0.02 for 2%
        "operations": [
            { "cycle_time": float, "batch": float, "utilization": float, "scrap_rate": float },
            ...
        ]
    }
    """
    product_name = payload.get("product_name", "")
    demand = float(payload.get("demand", 0.0))
    bom_qty = payload.get("bom_qty")
    is_final = bool(payload.get("is_final_product", False))
    scrap_rates = [float(s) for s in payload.get("scrap_rates", [])]

    started = calculate_started(demand, bom_qty, is_final)
    good_made = calculate_good_made(started, scrap_rates)
    scrap = calculate_scrap(started, good_made)
    shipped = calculate_good_shipped(good_made, demand)
    wip = calculate_wip(started, shipped, scrap)

    ops = [
        OperationInput(
            cycle_time=float(o.get("cycle_time", 0.0)),
            batch=float(o.get("batch", 1.0)),
            utilization=float(o.get("utilization", 0.0)),
            scrap_rate=float(o.get("scrap_rate", 0.0)),
        )
        for o in payload.get("operations", [])
    ]
    mct = calculate_mct(ops)

    return {
        "product": product_name,
        "demand": demand,
        "started": started,
        "good_made": good_made,
        "scrap": scrap,
        "good_shipped": shipped,
        "wip": wip,
        "mct": mct,
    }


@csrf_exempt
@require_http_methods(["POST"])
def full_calculate_view(request):
    """
    POST /api/simulations/full-calculate
    Body: { "model": { ... }, "scenario": { ... } | null }
    Returns: { "results": { equipment, labor, products, warnings, errors, overLimitResources, calculatedAt } }
    """
    from .full_calculate import full_calculate as do_full_calculate

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    model = data.get("model")
    scenario = data.get("scenario")
    if not model:
        return JsonResponse({"error": "Missing 'model' in body"}, status=400)

    try:
        results = do_full_calculate(model, scenario)
        return JsonResponse({"results": results})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def simulate_rows(request):
    """
    Optional API endpoint:

    POST /api/simulations/rows
    {
      "rows": [ { ...see calculate_product_row payload... }, ... ]
    }

    Returns:
    { "results": [ { product, demand, started, good_made, good_shipped, scrap, wip, mct }, ... ] }
    """
    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    rows = data.get("rows", [])
    results = [calculate_product_row(row) for row in rows]
    return JsonResponse({"results": results})
