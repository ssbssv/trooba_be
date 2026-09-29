"""
Apply a frontend-shaped model snapshot to relational tables (post-migration from JSON blobs).
All writes are scoped to the target model and organization to prevent cross-tenant data bleed.
"""
from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.generaldata.models import GeneralData
from apps.ibom.models import BOM
from apps.labor.models import Labor
from apps.equipment.models import EquipmentGroup
from apps.products.models import Product
from apps.operations.models import Operation
from apps.routing.models import Routing

from .models import RMCMModel


class SnapshotScopeError(ValueError):
    """Snapshot references entity IDs that do not belong to this model."""


def _model_organization(m: RMCMModel):
    owner = m.owner
    if owner is None or not hasattr(owner, "profile") or not getattr(owner.profile, "organization_id", None):
        return None
    return owner.profile.organization


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _assert_id_owned_by_model(model_cls, entity_label: str, row_id: uuid.UUID, m: RMCMModel) -> None:
    """Reject snapshot IDs that already belong to another model (multi-tenant safety)."""
    existing = model_cls.objects.filter(id=row_id).first()
    if existing is not None and getattr(existing, "model_id", None) != m.id:
        raise SnapshotScopeError(
            f"{entity_label} id {row_id} does not belong to this model"
        )


def validate_snapshot_scope(m: RMCMModel, snap: dict) -> None:
    snap = snap or {}

    _validate_model_ids(
        snap.get("labor") or [],
        Labor,
        "Labor",
        m,
    )

    _validate_model_ids(
        snap.get("equipment") or [],
        EquipmentGroup,
        "Equipment",
        m,
    )

    products = snap.get("products") or []

    _validate_model_ids(
        products,
        Product,
        "Product",
        m,
    )

    snapshot_product_ids = _snapshot_product_ids(products)

    _validate_operation_scope(
        m,
        snap.get("operations") or [],
        snapshot_product_ids,
    )

def _validate_model_ids(items, model_cls, entity_label, m):
    for item in items:
        row_id = _parse_uuid(item.get("id"))
        if row_id:
            _assert_id_owned_by_model(
                model_cls,
                entity_label,
                row_id,
                m,
            )

def _snapshot_product_ids(products):
    return {
        str(pid)
        for pid in (
            _parse_uuid(item.get("id"))
            for item in products
        )
        if pid
    }

def _validate_operation_scope(m, operations, snapshot_product_ids):
    for item in operations:
        operation_id = _parse_uuid(item.get("id"))
        if operation_id:
            operation = (
                Operation.objects
                .filter(id=operation_id)
                .select_related("product")
                .first()
            )

            if (
                operation is not None
                and operation.product.model_id != m.id
            ):
                raise SnapshotScopeError(
                    f"Operation id {operation_id} does not belong to this model"
                )

        product_id = _parse_uuid(item.get("product_id"))
        if not product_id:
            continue

        product = Product.objects.filter(id=product_id).first()

        if (
            product is not None
            and product.model_id != m.id
        ):
            raise SnapshotScopeError(
                f"Product id {product_id} does not belong to this model"
            )

        if (
            product is None
            and str(product_id) not in snapshot_product_ids
        ):
            raise SnapshotScopeError(
                f"Operation references unknown product id {product_id}"
            )



def _restore_general(m: RMCMModel, general: dict) -> None:
    if not isinstance(general, dict):
        return
    gd, _created = GeneralData.objects.get_or_create(model=m)
    mappings = {
        "model_title": "model_title",
        "author": "author",
        "comments": "comments",
        "ops_time_unit": "ops_time_unit",
        "mct_time_unit": "mct_time_unit",
        "prod_period_unit": "prod_period_unit",
        "conv1": "conv1",
        "conv2": "conv2",
        "util_limit": "util_limit",
        "var_equip": "var_equip",
        "var_labor": "var_labor",
        "var_prod": "var_prod",
        "gen1": "gen1",
        "gen2": "gen2",
        "gen3": "gen3",
        "gen4": "gen4",
        "output_view_mode": "output_view_mode",
    }
    for payload_key, field_name in mappings.items():
        if payload_key in general:
            value = general[payload_key]
            if payload_key == "output_view_mode" and value not in ("normal", "premium"):
                continue
            setattr(gd, field_name, value)
    gd.save()


def _soft_delete_extras(qs, keep_ids: set) -> None:
    now = timezone.now()
    if keep_ids:
        qs.exclude(id__in=keep_ids).update(deleted_at=now)
    else:
        qs.update(deleted_at=now)


def _restore_labor(m: RMCMModel, org, items: list) -> None:
    keep: set[uuid.UUID] = set()
    for item in items:
        lid = _parse_uuid(item.get("id"))
        if lid:
            keep.add(lid)
    _soft_delete_extras(Labor.objects.filter(model=m), keep)

    for item in items:
        lid = _parse_uuid(item.get("id"))
        if not lid:
            continue
        fields = {
            "organization": org,
            "model": m,
            "name": (item.get("name") or "").upper(),
            "count": item.get("count", 1),
            "overtime_percent": item.get("overtime_pct", 0),
            "unavailability_percent": item.get("unavail_pct", 0),
            "department": item.get("dept_code") or None,
            "setup_factor": item.get("setup_factor", 1),
            "run_factor": item.get("run_factor", 1),
            "variable_factor": item.get("var_factor", 1),
            "prioritize": item.get("prioritize_use", False),
            "lab1": item.get("lab1", 0),
            "lab2": item.get("lab2", 0),
            "lab3": item.get("lab3", 0),
            "lab4": item.get("lab4", 0),
            "notes": item.get("comments", ""),
            "deleted_at": None,
        }
        row = Labor.objects.filter(id=lid, model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            _assert_id_owned_by_model(Labor, "Labor", lid, m)
            Labor.objects.create(id=lid, **fields)


def _restore_equipment(m: RMCMModel, org, items: list) -> None:
    keep = _equipment_keep_ids(items)

    _soft_delete_extras(
        EquipmentGroup.objects.filter(model=m),
        keep,
    )

    for item in items:
        equipment_id = _parse_uuid(item.get("id"))

        if not equipment_id:
            continue

        fields = _equipment_fields(
            m,
            org,
            item,
        )

        _save_equipment_row(
            m,
            equipment_id,
            fields,
        )

def _equipment_keep_ids(items):
    keep = set()

    for item in items:
        equipment_id = _parse_uuid(item.get("id"))
        if equipment_id:
            keep.add(equipment_id)

    return keep

def _equipment_labor_group(model, item):
    labor_group_id = _parse_uuid(item.get("labor_group_id"))

    if not labor_group_id:
        return None

    return Labor.objects.filter(
        id=labor_group_id,
        model=model,
    ).first()

def _equipment_fields(model, organization, item):
    equipment_type = (
        "Delay"
        if (item.get("equip_type") or "standard").lower() == "delay"
        else "Standard"
    )

    return {
        "organization": organization,
        "model": model,
        "name": (item.get("name") or "").upper(),
        "count": item.get("count", 1),
        "mttf_minutes": item.get("mttf", 0),
        "mttr_minutes": item.get("mttr", 0),
        "overtime_percent": item.get("overtime_pct", 0),
        "labor_group": _equipment_labor_group(model, item),
        "department_area": item.get("dept_code") or None,
        "out_of_area_equipment": item.get("out_of_area", False),
        "percent_time_unavailable": item.get("unavail_pct", 0),
        "setup_factor": item.get("setup_factor", 1),
        "run_factor": item.get("run_factor", 1),
        "variability_factor": item.get("var_factor", 1),
        "eq1": item.get("eq1", 0),
        "eq2": item.get("eq2", 0),
        "eq3": item.get("eq3", 0),
        "eq4": item.get("eq4", 0),
        "comments": item.get("comments", ""),
        "equipment_type": equipment_type,
        "deleted_at": None,
    }

def _save_equipment_row(model, equipment_id, fields):
    row = EquipmentGroup.objects.filter(
        id=equipment_id,
        model=model,
    ).first()

    if row:
        for key, value in fields.items():
            setattr(row, key, value)
        row.save()
        return

    _assert_id_owned_by_model(
        EquipmentGroup,
        "Equipment",
        equipment_id,
        model,
    )

    EquipmentGroup.objects.create(
        id=equipment_id,
        **fields,
    )
    


def _restore_products(m: RMCMModel, org, items: list) -> None:
    keep: set[uuid.UUID] = set()
    for item in items:
        pid = _parse_uuid(item.get("id"))
        if pid:
            keep.add(pid)
    _soft_delete_extras(Product.objects.filter(model=m), keep)

    for item in items:
        pid = _parse_uuid(item.get("id"))
        if not pid:
            continue
        fields = {
            "organization": org,
            "model": m,
            "name": (item.get("name") or "").upper(),
            "end_demand": item.get("demand", 0),
            "lot_size": item.get("lot_size", 1),
            "transfer_batch": item.get("tbatch_size", -1),
            "department_area": item.get("dept_code") or None,
            "demand_factor": item.get("demand_factor", 1),
            "lot_factor": item.get("lot_factor", 1),
            "variability_factor": item.get("var_factor", 1),
            "make_to_stock": item.get("make_to_stock", False),
            "gather_transfer_batches": item.get("gather_tbatches", False),
            "prod1": item.get("prod1", 0),
            "prod2": item.get("prod2", 0),
            "prod3": item.get("prod3", 0),
            "prod4": item.get("prod4", 0),
            "comments": item.get("comments", ""),
            "deleted_at": None,
        }
        row = Product.objects.filter(id=pid, model=m).first()
        if row:
            for key, val in fields.items():
                setattr(row, key, val)
            row.save()
        else:
            _assert_id_owned_by_model(Product, "Product", pid, m)
            Product.objects.create(id=pid, **fields)


def _restore_operations(m: RMCMModel, org, items: list) -> None:
    product_ids = Product.objects.filter(
        model=m,
    ).values_list("id", flat=True)

    keep = _operation_keep_ids(items)

    _soft_delete_extras(
        Operation.objects.filter(product_id__in=product_ids),
        keep,
    )

    for item in items:
        operation_id = _parse_uuid(item.get("id"))

        if not operation_id:
            continue

        product = _operation_product(
            m,
            item,
        )

        if product is None:
            continue

        fields = _operation_fields(
            m,
            org,
            product,
            item,
        )

        _save_operation_row(
            m,
            operation_id,
            fields,
        )

def _operation_keep_ids(items):
    keep = set()

    for item in items:
        operation_id = _parse_uuid(item.get("id"))
        if operation_id:
            keep.add(operation_id)

    return keep

def _operation_product(model, item):
    product_id = _parse_uuid(item.get("product_id"))

    if not product_id:
        return None

    return Product.objects.filter(
        id=product_id,
        model=model,
        deleted_at__isnull=True,
    ).first()

def _operation_equipment(model, item):
    equipment_id = _parse_uuid(item.get("equip_id"))

    if not equipment_id:
        return None

    return EquipmentGroup.objects.filter(
        id=equipment_id,
        model=model,
    ).first()

def _operation_fields(model, organization, product, item):
    return {
        "organization": organization,
        "product": product,
        "op_number": item.get("op_number", 1),
        "name": item.get("op_name", ""),
        "equipment_group": _operation_equipment(model, item),
        "percent_assign": item.get("pct_assigned", 100),
        "equipment_setup_per_lot": item.get("equip_setup_lot", 0),
        "equipment_setup_per_piece": item.get("equip_setup_piece", 0),
        "equipment_setup_per_tbatch": item.get("equip_setup_tbatch", 0),
        "equipment_run_per_piece": item.get("equip_run_piece", 0),
        "equipment_run_per_lot": item.get("equip_run_lot", 0),
        "equipment_run_per_tbatch": item.get("equip_run_tbatch", 0),
        "labor_setup_per_lot": item.get("labor_setup_lot", 0),
        "labor_setup_per_piece": item.get("labor_setup_piece", 0),
        "labor_setup_per_tbatch": item.get("labor_setup_tbatch", 0),
        "labor_run_per_piece": item.get("labor_run_piece", 0),
        "labor_run_per_lot": item.get("labor_run_lot", 0),
        "labor_run_per_tbatch": item.get("labor_run_tbatch", 0),
        "oper1": item.get("oper1", 0),
        "oper2": item.get("oper2", 0),
        "oper3": item.get("oper3", 0),
        "oper4": item.get("oper4", 0),
        "comments": "",
        "deleted_at": None,
    }

def _save_operation_row(model, operation_id, fields):
    row = Operation.objects.filter(
        id=operation_id,
        product__model=model,
    ).first()

    if row:
        for key, value in fields.items():
            setattr(row, key, value)
        row.save()
        return

    existing = Operation.objects.filter(id=operation_id).first()

    if existing is not None:
        raise SnapshotScopeError(
            f"Operation id {operation_id} does not belong to this model"
        )

    Operation.objects.create(
        id=operation_id,
        **fields,
    )



def _restore_routing(m: RMCMModel, org, items: list) -> None:
    product_ids = Product.objects.filter(
        model=m,
    ).values_list("id", flat=True)

    keep = _routing_keep_ids(items)

    _soft_delete_extras(
        Routing.objects.filter(product_id__in=product_ids),
        keep,
    )

    for item in items:
        routing_id = _parse_uuid(item.get("id"))

        if not routing_id:
            continue

        product = _routing_product(
            m,
            item,
        )

        if product is None:
            continue

        from_operation, to_operation = _routing_operations(
            product,
            item,
        )

        if from_operation is None or to_operation is None:
            continue

        fields = _routing_fields(
            org,
            product,
            from_operation,
            to_operation,
            item,
        )

        _save_routing_row(
            m,
            routing_id,
            fields,
        )

def _routing_keep_ids(items):
    keep = set()

    for item in items:
        routing_id = _parse_uuid(item.get("id"))
        if routing_id:
            keep.add(routing_id)

    return keep

def _routing_product(model, item):
    product_id = _parse_uuid(item.get("product_id"))

    if not product_id:
        return None

    return Product.objects.filter(
        id=product_id,
        model=model,
        deleted_at__isnull=True,
    ).first()

def _routing_operations(product, item):
    from_operation = Operation.objects.filter(
        product=product,
        name=item.get("from_op_name") or "",
        deleted_at__isnull=True,
    ).first()

    to_operation = Operation.objects.filter(
        product=product,
        name=item.get("to_op_name") or "",
        deleted_at__isnull=True,
    ).first()

    return from_operation, to_operation

def _routing_fields(org, product, from_operation, to_operation, item):
    return {
        "organization": org,
        "product": product,
        "from_operation": from_operation,
        "to_operation": to_operation,
        "probability": item.get("pct_routed", 1),
        "deleted_at": None,
    }

def _save_routing_row(model, routing_id, fields):
    row = Routing.objects.filter(
        id=routing_id,
        product__model=model,
    ).first()

    if row:
        for key, value in fields.items():
            setattr(row, key, value)
        row.save()
        return

    existing = Routing.objects.filter(id=routing_id).first()

    if existing is not None and existing.product.model_id != model.id:
        raise SnapshotScopeError(
            f"Routing id {routing_id} does not belong to this model"
        )

    Routing.objects.create(
        id=routing_id,
        **fields,
    )



def _restore_ibom(m: RMCMModel, org, items: list) -> None:
    parent_ids = Product.objects.filter(
        model=m,
    ).values_list("id", flat=True)

    keep = _ibom_keep_ids(items)

    _soft_delete_extras(
        BOM.objects.filter(parent_product_id__in=parent_ids),
        keep,
    )

    for item in items:
        bom_id = _parse_uuid(item.get("id"))

        if not bom_id:
            continue

        parent, component = _ibom_products(
            m,
            item,
        )

        if parent is None or component is None:
            continue

        fields = _ibom_fields(
            org,
            parent,
            component,
            item,
        )

        _save_ibom_row(
            m,
            bom_id,
            fields,
        )

def _ibom_keep_ids(items):
    keep = set()

    for item in items:
        bom_id = _parse_uuid(item.get("id"))
        if bom_id:
            keep.add(bom_id)

    return keep

def _ibom_products(model, item):
    parent_id = _parse_uuid(item.get("parent_product_id"))
    component_id = _parse_uuid(item.get("component_product_id"))

    if not parent_id or not component_id:
        return None, None

    parent = Product.objects.filter(
        id=parent_id,
        model=model,
        deleted_at__isnull=True,
    ).first()

    component = Product.objects.filter(
        id=component_id,
        model=model,
        deleted_at__isnull=True,
    ).first()

    return parent, component

def _ibom_fields(org, parent, component, item):
    return {
        "organization": org,
        "parent_product": parent,
        "component_product": component,
        "quantity_per_assembly": item.get("units_per_assy", 1),
        "deleted_at": None,
    }

def _save_ibom_row(model, bom_id, fields):
    row = BOM.objects.filter(
        id=bom_id,
        parent_product__model=model,
    ).first()

    if row:
        for key, value in fields.items():
            setattr(row, key, value)
        row.save()
        return

    existing = BOM.objects.filter(id=bom_id).first()

    if existing is not None and existing.parent_product.model_id != model.id:
        raise SnapshotScopeError(
            f"BOM id {bom_id} does not belong to this model"
        )

    BOM.objects.create(
        id=bom_id,
        **fields,
    )



@transaction.atomic
def apply_snapshot_to_model(m: RMCMModel, snap: dict) -> None:
    """Replace model nested data from a version snapshot dict."""
    snap = snap or {}
    org = _model_organization(m)
    if org is None:
        raise ValueError("Owner organization not configured for model")

    validate_snapshot_scope(m, snap)

    if isinstance(snap.get("param_names"), dict):
        m.param_names = snap["param_names"]
    if isinstance(snap.get("dept_codes"), dict):
        m.dept_codes = snap["dept_codes"]

    _restore_general(m, snap.get("general") or {})

    labor = snap.get("labor") if isinstance(snap.get("labor"), list) else []
    equipment = snap.get("equipment") if isinstance(snap.get("equipment"), list) else []
    products = snap.get("products") if isinstance(snap.get("products"), list) else []
    operations = snap.get("operations") if isinstance(snap.get("operations"), list) else []
    routing = snap.get("routing") if isinstance(snap.get("routing"), list) else []
    ibom = snap.get("ibom") if isinstance(snap.get("ibom"), list) else []

    _restore_labor(m, org, labor)
    _restore_equipment(m, org, equipment)
    _restore_products(m, org, products)
    _restore_operations(m, org, operations)
    _restore_routing(m, org, routing)
    _restore_ibom(m, org, ibom)

    m.run_status = "needs_recalc"
    m.save(update_fields=["param_names", "dept_codes", "run_status", "updated_at"])
