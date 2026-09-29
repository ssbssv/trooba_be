import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel
from apps.operations.models import Operation
from apps.routing.models import Routing
from apps.ibom.models import BOM

from .models import Product
from apps.organizations.scoping import get_org_context
from apps.organizations.nested_rows import revive_soft_deleted, sync_row_organization


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


@csrf_exempt
@require_http_methods(['POST'])
def model_products_create(request, model_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    org = ctx.organization

    pid = data.get('id')
    prod_kwargs = {
        'organization': org,
        'model': m,
        'name': data.get('name', '').upper(),
        'end_demand': data.get('demand', 0),
        'lot_size': data.get('lot_size', 1),
        'transfer_batch': data.get('tbatch_size', -1),
        'department_area': data.get('dept_code') or '',
        'demand_factor': data.get('demand_factor', 1),
        'lot_factor': data.get('lot_factor', 1),
        'variability_factor': data.get('var_factor', 1),
        'make_to_stock': data.get('make_to_stock', False),
        'gather_transfer_batches': data.get('gather_tbatches', False),
        'prod1': data.get('prod1', 0),
        'prod2': data.get('prod2', 0),
        'prod3': data.get('prod3', 0),
        'prod4': data.get('prod4', 0),
        'comments': data.get('comments', ''),
    }

    if pid:
        prod_kwargs['id'] = pid

    from django.db import IntegrityError

    try:
        p = Product.objects.create(**prod_kwargs)
    except IntegrityError:
        # A row with the same (organization, model, name) already exists.
        # Look it up ignoring deleted_at so we can either return or "undelete" it.
        existing = Product.objects.filter(
            organization=org,
            model=m,
            name=prod_kwargs["name"],
        ).first()
        if not existing:
            # If we cannot find the existing row, re-raise so it surfaces during debugging.
            raise

        # If the row was soft-deleted, revive it and update fields from the payload.
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.end_demand = prod_kwargs["end_demand"]
            existing.lot_size = prod_kwargs["lot_size"]
            existing.transfer_batch = prod_kwargs["transfer_batch"]
            existing.department_area = prod_kwargs["department_area"]
            existing.demand_factor = prod_kwargs["demand_factor"]
            existing.lot_factor = prod_kwargs["lot_factor"]
            existing.variability_factor = prod_kwargs["variability_factor"]
            existing.make_to_stock = prod_kwargs["make_to_stock"]
            existing.gather_transfer_batches = prod_kwargs["gather_transfer_batches"]
            existing.prod1 = prod_kwargs["prod1"]
            existing.prod2 = prod_kwargs["prod2"]
            existing.prod3 = prod_kwargs["prod3"]
            existing.prod4 = prod_kwargs["prod4"]
            existing.comments = prod_kwargs["comments"]
            existing.save()

        return JsonResponse(
            {
                "id": str(existing.id),
                "detail": "Product with this name already exists for this model and organization.",
            },
            status=200,
        )

    return JsonResponse({'id': str(p.id)}, status=201)


@csrf_exempt
@require_http_methods(["PATCH"])
def model_products_update(request, model_id, product_id):
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

    product = _resolve_product(
        model,
        product_id,
        ctx.organization,
    )

    if product is None:
        return JsonResponse(
            {"error": "Product not found for this model"},
            status=404,
        )

    _apply_product_updates(
        product,
        data,
    )

    product.save()

    return JsonResponse({})

def _resolve_product(model, product_id, organization):
    product = Product.objects.filter(
        id=product_id,
        model=model,
    ).first()

    if product is None:
        return None

    sync_row_organization(product, organization)
    revive_soft_deleted(product)

    return product

def _apply_product_updates(product, data):
    field_map = {
        "name": "name",
        "demand": "end_demand",
        "lot_size": "lot_size",
        "tbatch_size": "transfer_batch",
        "demand_factor": "demand_factor",
        "lot_factor": "lot_factor",
        "var_factor": "variability_factor",
        "make_to_stock": "make_to_stock",
        "gather_tbatches": "gather_transfer_batches",
        "comments": "comments",
    }

    for request_field, model_field in field_map.items():
        if request_field in data:
            setattr(product, model_field, data[request_field])

    if "dept_code" in data:
        product.department_area = data["dept_code"] or ""

    for field in ("prod1", "prod2", "prod3", "prod4"):
        if field in data:
            setattr(product, field, data[field])



def _build_product_kwargs(data, organization, model):
    product_kwargs = {
        "organization": organization,
        "model": model,
        "name": data.get("name", "").upper(),
        "end_demand": data.get("demand", 0),
        "lot_size": data.get("lot_size", 1),
        "transfer_batch": data.get("tbatch_size", -1),
        "department_area": data.get("dept_code") or "",
        "demand_factor": data.get("demand_factor", 1),
        "lot_factor": data.get("lot_factor", 1),
        "variability_factor": data.get("var_factor", 1),
        "make_to_stock": data.get("make_to_stock", False),
        "gather_transfer_batches": data.get("gather_tbatches", False),
        "prod1": data.get("prod1", 0),
        "prod2": data.get("prod2", 0),
        "prod3": data.get("prod3", 0),
        "prod4": data.get("prod4", 0),
        "comments": data.get("comments", ""),
    }

    product_id = data.get("id")
    if product_id:
        product_kwargs["id"] = product_id

    return product_kwargs

def _restore_existing_product(org, model, product_kwargs):
    existing = Product.objects.filter(
        organization=org,
        model=model,
        name=product_kwargs["name"],
    ).first()

    if not existing:
        raise

    if existing.deleted_at is None:
        return existing

    existing.deleted_at = None

    fields = (
        "end_demand",
        "lot_size",
        "transfer_batch",
        "department_area",
        "demand_factor",
        "lot_factor",
        "variability_factor",
        "make_to_stock",
        "gather_transfer_batches",
        "prod1",
        "prod2",
        "prod3",
        "prod4",
        "comments",
    )

    for field in fields:
        setattr(existing, field, product_kwargs[field])

    existing.save()

    return existing


@csrf_exempt
@require_http_methods(['DELETE'])
def model_products_delete(request, model_id, product_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)
    p = Product.objects.filter(id=product_id, model=m).first()
    if p is None:
        return JsonResponse({"success": True}, status=200)
    sync_row_organization(p, ctx.organization)

    from django.utils import timezone

    now = timezone.now()
    p.deleted_at = now
    p.save()

    Operation.objects.filter(product_id=product_id, deleted_at__isnull=True).update(deleted_at=now)
    Routing.objects.filter(product_id=product_id, deleted_at__isnull=True).update(deleted_at=now)
    BOM.objects.filter(parent_product_id=product_id, deleted_at__isnull=True).update(deleted_at=now)

    return JsonResponse({"success": True}, status=200)


@csrf_exempt
@require_http_methods(['DELETE'])
def model_products_clear_ops_routing(request, model_id, product_id):
    ctx, err = get_org_context(request)
    if err:
        return err
    m = get_object_or_404(RMCMModel, id=model_id, organization_id=ctx.organization.id)

    from django.utils import timezone

    now = timezone.now()
    Operation.objects.filter(product_id=product_id, organization_id=ctx.organization.id, deleted_at__isnull=True).update(deleted_at=now)
    Routing.objects.filter(product_id=product_id, organization_id=ctx.organization.id, deleted_at__isnull=True).update(deleted_at=now)

    m.run_status = 'needs_recalc'
    m.save()

    return JsonResponse({"success": True}, status=200)
