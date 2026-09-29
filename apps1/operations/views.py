import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import IntegrityError, transaction

from apps.rmct.models import RMCMModel
from apps.equipment.models import EquipmentGroup
from apps.labor.models import Labor
from apps.routing.models import Routing
from apps.products.models import Product

from .models import Operation
from apps.organizations.scoping import get_org_context
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None

@csrf_exempt
@require_http_methods(["POST"])
def model_operations_create(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err

    get_object_or_404(
        RMCMModel,
        id=model_id,
        organization_id=ctx.organization.id,
    )

    data = _parse_json(request)
    if data is None:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400,
        )

    organization = ctx.organization

    product = get_object_or_404(
        Product,
        id=data.get("product_id"),
        organization_id=organization.id,
    )

    equipment_group = None
    equip_id = data.get("equip_id")
    if equip_id:
        equipment_group = (
            EquipmentGroup.objects.filter(id=equip_id).first()
        )

    op_name = str(data.get("op_name", "") or "").strip()

    if op_name.upper() == "DOCK":
        existing_dock = (
            Operation.objects.filter(
                organization=organization,
                product=product,
                deleted_at__isnull=True,
                name__iexact="DOCK",
            ).first()
        )

        if existing_dock:
            if existing_dock.op_number != 0:
                existing_dock.op_number = 0
                existing_dock.save(update_fields=["op_number"])

            from apps.operations.normalize import (
                dedupe_routing_meta_operations_for_product,
            )

            dedupe_routing_meta_operations_for_product(product)

            return JsonResponse(
                {"id": str(existing_dock.id)},
                status=200,
            )

    operation_kwargs = _build_operation_kwargs(
        data,
        organization,
        product,
        equipment_group,
    )

    try:
        operation = Operation.objects.create(**operation_kwargs)

    except IntegrityError:
        operation = _restore_existing_operation(
            organization,
            product,
            operation_kwargs,
        )

        from apps.operations.normalize import (
            dedupe_routing_meta_operations_for_product,
        )

        dedupe_routing_meta_operations_for_product(product)

        return JsonResponse(
            {
                "id": str(operation.id),
                "detail": (
                    "Operation with this number already exists "
                    "for this product."
                ),
            },
            status=200,
        )

    from apps.operations.normalize import (
        dedupe_routing_meta_operations_for_product,
    )

    dedupe_routing_meta_operations_for_product(product)

    return JsonResponse(
        {"id": str(operation.id)},
        status=201,
    )

def _build_operation_kwargs(data, organization, product, equipment_group):
    op_name = str(data.get("op_name", "") or "").strip()

    operation_kwargs = {
        "organization": organization,
        "product": product,
        "op_number": (
            0
            if op_name.upper() == "DOCK"
            else data.get("op_number", 1)
        ),
        "name": op_name,
        "equipment_group": equipment_group,
        "percent_assign": data.get("pct_assigned", 100),
        "equipment_setup_per_lot": data.get("equip_setup_lot", 0),
        "equipment_run_per_piece": data.get("equip_run_piece", 0),
        "labor_setup_per_lot": data.get("labor_setup_lot", 0),
        "labor_run_per_piece": data.get("labor_run_piece", 0),
        "equipment_setup_per_piece": data.get("equip_setup_piece", 0),
        "equipment_setup_per_tbatch": data.get("equip_setup_tbatch", 0),
        "equipment_run_per_lot": data.get("equip_run_lot", 0),
        "equipment_run_per_tbatch": data.get("equip_run_tbatch", 0),
        "labor_setup_per_piece": data.get("labor_setup_piece", 0),
        "labor_setup_per_tbatch": data.get("labor_setup_tbatch", 0),
        "labor_run_per_lot": data.get("labor_run_lot", 0),
        "labor_run_per_tbatch": data.get("labor_run_tbatch", 0),
        "oper1": data.get("oper1", 0),
        "oper2": data.get("oper2", 0),
        "oper3": data.get("oper3", 0),
        "oper4": data.get("oper4", 0),
        "comments": "",
    }

    labor_group_id = data.get("labor_group_id")
    if labor_group_id:
        operation_kwargs["labor"] = (
            Labor.objects.filter(id=labor_group_id).first()
        )

    operation_id = data.get("id")
    if operation_id:
        operation_kwargs["id"] = operation_id

    return operation_kwargs

def _restore_existing_operation(
    organization,
    product,
    operation_kwargs,
):
    existing = Operation.objects.filter(
        organization=organization,
        product=product,
        op_number=operation_kwargs["op_number"],
    ).first()

    if not existing:
        raise

    if existing.deleted_at is None:
        return existing

    existing.deleted_at = None

    fields = (
        "name",
        "equipment_group",
        "labor",
        "percent_assign",
        "equipment_setup_per_lot",
        "equipment_run_per_piece",
        "labor_setup_per_lot",
        "labor_run_per_piece",
        "equipment_setup_per_piece",
        "equipment_setup_per_tbatch",
        "equipment_run_per_lot",
        "equipment_run_per_tbatch",
        "labor_setup_per_piece",
        "labor_setup_per_tbatch",
        "labor_run_per_lot",
        "labor_run_per_tbatch",
        "oper1",
        "oper2",
        "oper3",
        "oper4",
        "comments",
    )

    for field in fields:
        setattr(existing, field, operation_kwargs.get(field))

    existing.save()

    return existing

@csrf_exempt
@require_http_methods(["PATCH"])
def model_operations_update(request, model_id, op_id):
    ctx, err = get_org_context(request)
    if err:
        return err

    model = get_object_or_404(
        RMCMModel,
        id=model_id,
        organization_id=ctx.organization.id,
    )

    data = _parse_json(request)
    if data is None:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400,
        )

    operation = _find_operation(
        model,
        op_id,
        data,
    )

    if operation is None:
        return JsonResponse(
            {"error": "Operation not found"},
            status=404,
        )

    sync_row_organization(operation, ctx.organization)
    revive_soft_deleted(operation)

    _update_operation_fields(operation, data)
    _update_operation_relations(operation, data)

    response = _swap_operation_number_if_needed(
        operation,
        data,
        ctx.organization.id,
    )

    if response:
        return response

    try:
        operation.save()
    except IntegrityError as exc:
        return JsonResponse(
            {
                "error": "integrity_error",
                "detail": str(exc),
            },
            status=400,
        )

    return JsonResponse({})

def _find_operation(model, op_id, data):
    operation = Operation.objects.filter(
        id=op_id,
        product__model=model,
    ).first()

    if operation is not None:
        return operation

    product_id = data.get("product_id")
    op_number = data.get("op_number")

    if product_id is None or op_number is None:
        return None

    return Operation.objects.filter(
        product_id=product_id,
        product__model=model,
        op_number=op_number,
    ).first()

def _update_operation_fields(operation, data):
    field_map = {
        "op_name": "name",
        "op_number": "op_number",
        "pct_assigned": "percent_assign",
        "equip_setup_lot": "equipment_setup_per_lot",
        "equip_run_piece": "equipment_run_per_piece",
        "labor_setup_lot": "labor_setup_per_lot",
        "labor_run_piece": "labor_run_per_piece",
        "equip_setup_piece": "equipment_setup_per_piece",
        "equip_setup_tbatch": "equipment_setup_per_tbatch",
        "equip_run_lot": "equipment_run_per_lot",
        "equip_run_tbatch": "equipment_run_per_tbatch",
        "labor_setup_piece": "labor_setup_per_piece",
        "labor_setup_tbatch": "labor_setup_per_tbatch",
        "labor_run_lot": "labor_run_per_lot",
        "labor_run_tbatch": "labor_run_per_tbatch",
        "oper1": "oper1",
        "oper2": "oper2",
        "oper3": "oper3",
        "oper4": "oper4",
    }

    for request_field, model_field in field_map.items():
        if request_field in data:
            setattr(operation, model_field, data[request_field])

def _update_operation_relations(operation, data):
    if "equip_id" in data:
        equip_id = data.get("equip_id")
        operation.equipment_group = (
            EquipmentGroup.objects.filter(id=equip_id).first()
            if equip_id
            else None
        )

    if "labor_group_id" in data:
        labor_group_id = data.get("labor_group_id")
        operation.labor = (
            Labor.objects.filter(id=labor_group_id).first()
            if labor_group_id
            else None
        )

def _swap_operation_number_if_needed(operation, data, organization_id):
    new_op_number = data.get("op_number")

    if new_op_number is None:
        return None

    conflicting = (
        Operation.objects.filter(
            product=operation.product,
            op_number=new_op_number,
            organization_id=organization_id,
            deleted_at__isnull=True,
        )
        .exclude(id=operation.id)
        .first()
    )

    if conflicting is None:
        return None

    old_op_number = operation.op_number

    try:
        with transaction.atomic():
            Operation.objects.filter(
                id=conflicting.id
            ).update(op_number=-1)

            operation.save()

            Operation.objects.filter(
                id=conflicting.id
            ).update(op_number=old_op_number)

    except IntegrityError as exc:
        return JsonResponse(
            {
                "error": "integrity_error",
                "detail": str(exc),
            },
            status=400,
        )

    return JsonResponse(
        {
            "swapped_with": str(conflicting.id),
        }
    )


@csrf_exempt
@require_http_methods(['DELETE'])
def model_operations_delete(request, model_id, op_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    op = Operation.objects.filter(id=op_id, product__model=m).first()
    if op is None:
        return JsonResponse({"success": True}, status=200)

    from django.utils import timezone

    now = timezone.now()
    op.deleted_at = now
    op.save()

    Routing.objects.filter(from_operation=op, deleted_at__isnull=True).update(deleted_at=now)

    return JsonResponse({"success": True}, status=200)
