"""
ChatController — thin HTTP layer. Business logic lives in ChatService /
ChatSessionValidator. No inactivity or model-version checks here.
"""
from __future__ import annotations

import json
import uuid

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.ai.llm import get_llm_client
from apps.ai.models import ChatSession
from apps.ai.services import ChatService, ChatSessionLockedError, FactoryBriefingService
from apps.ai.services.factory_analysis_service import FactoryAnalysisService
from apps.ai.services.recommended_actions_service import RecommendedActionsService
from apps.ai.services.managers_playbook_service import ManagersPlaybookService
from apps.organizations.scoping import get_org_context
from apps.rmct.models import RMCMModel, Scenario


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _require_authenticated(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)
    return None


def _owned_factory_or_404(request, factory_id):
    ctx, err = get_org_context(request)
    if err:
        raise RMCMModel.DoesNotExist()
    return get_object_or_404(
        RMCMModel.objects.filter(organization_id=ctx.organization.id),
        id=factory_id,
    )


def _resolve_scenario(factory: RMCMModel, scenario_id: str | None) -> Scenario | None:
    if not scenario_id or scenario_id in ("basecase", "null", "undefined"):
        from apps.ai.services.factory_briefing_service import ensure_basecase_scenario

        return ensure_basecase_scenario(factory)
    return get_object_or_404(Scenario.objects.filter(model=factory), id=scenario_id)


def _message_payload(row) -> dict:
    return {
        "id": str(row.id),
        "role": row.role,
        "message": row.message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _session_payload(session: ChatSession) -> dict:
    return {
        "id": str(session.id),
        "factory_id": str(session.factory_id),
        "scenario_id": str(session.scenario_id) if session.scenario_id else None,
        "conversation_summary": session.conversation_summary or "",
        "status": session.status,
        "lock_reason": session.lock_reason,
        "model_version": session.model_version or "",
        "last_activity_at": (
            session.last_activity_at.isoformat() if session.last_activity_at else None
        ),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _session_history_item(session: ChatSession) -> dict:
    """Compact row for the previous-chats list."""
    first_user = (
        session.messages.filter(role="user").order_by("created_at").first()
    )
    if first_user and first_user.message.strip():
        title = first_user.message.strip().replace("\n", " ")
    elif (session.conversation_summary or "").strip():
        title = session.conversation_summary.strip().replace("\n", " ")
    else:
        title = "New conversation"
    if len(title) > 72:
        title = title[:69].rstrip() + "…"

    payload = _session_payload(session)
    payload["title"] = title
    payload["message_count"] = session.messages.count()
    return payload


def _locked_response(exc: ChatSessionLockedError) -> JsonResponse:
    result = exc.result
    payload: dict = {
        "session": _session_payload(result.session),
        "message": result.message,
    }
    if result.session_expired:
        payload["session_expired"] = True
    if result.model_changed:
        payload["model_changed"] = True
    return JsonResponse(payload, status=409)


def _build_chat_service() -> ChatService:
    return ChatService(llm_client=get_llm_client())


def _build_factory_briefing_service() -> FactoryBriefingService:
    return FactoryBriefingService(llm_client=get_llm_client())


def _build_factory_analysis_service() -> FactoryAnalysisService:
    return FactoryAnalysisService(llm_client=get_llm_client())


def _build_recommended_actions_service() -> RecommendedActionsService:
    return RecommendedActionsService(llm_client=get_llm_client())


def _build_managers_playbook_service() -> ManagersPlaybookService:
    return ManagersPlaybookService(llm_client=get_llm_client())


@csrf_exempt
@require_http_methods(["POST"])
def factory_briefing(request):
    """
    POST /api/ai/factory-briefing/

    Body:
      {
        "factory_id": "<uuid>",
        "scenario_id": "<uuid>|null|basecase",
        "force_generate": false
      }

    Returns a cached or freshly generated factory briefing for one scenario.
    What-if input is the basecase model with that scenario's changes applied.
    Independent of chat sessions — does not include other what-if scenarios.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    factory_id = data.get("factory_id") or data.get("model_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse({"error": "Factory not found"}, status=404)

    scenario_id = data.get("scenario_id")
    force_generate = bool(data.get("force_generate"))
    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Exception:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    service = _build_factory_briefing_service()
    result = service.get_or_generate(
        factory=factory,
        scenario=scenario,
        force_generate=force_generate,
    )
    payload = FactoryBriefingService.to_payload(result)

    if result.needs_simulation:
        return JsonResponse(payload, status=200)
    if not result.success:
        return JsonResponse(payload, status=200)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def factory_analysis(request):
    """
    POST /api/ai/factory-analysis/

    Body:
      {
        "factory_id": "<uuid>",
        "scenario_id": "<uuid>|null|basecase",
        "force_generate": false
      }

    Returns one cached or freshly generated analysis payload used by both
    Problems Identified and Business Impact (single AI call).
    Refresh / stale rules match Factory Briefing.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    factory_id = data.get("factory_id") or data.get("model_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse(
            {
                "detail": "You do not have access to this factory or model.",
                "code": "forbidden",
                "error": "Factory not found",
            },
            status=404,
        )

    scenario_id = data.get("scenario_id")
    force_generate = bool(
        data.get("force_generate") or data.get("force_refresh")
    )
    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Exception:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    service = _build_factory_analysis_service()
    result = service.get_or_generate(
        factory=factory,
        scenario=scenario,
        force_generate=force_generate,
    )
    payload = FactoryAnalysisService.to_payload(result)

    if result.needs_simulation:
        payload.setdefault("code", "simulation_result_not_found")
        payload.setdefault(
            "detail",
            "No completed simulation result is available for the selected model and scenario.",
        )
        return JsonResponse(payload, status=200)
    if not result.success:
        return JsonResponse(payload, status=200)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def recommended_actions(request):
    """
    POST /api/ai/factory-analysis/recommended-actions/

    Body:
      {
        "factory_id": "<uuid>",
        "scenario_id": "<uuid>|null|basecase",
        "analysis_id": "<uuid>|optional",
        "force_generate": false
      }

    Generates Top 3 Recommended Actions from the validated Problems + Impacts
    already stored for this factory/scenario. Does not rediscover problems.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    factory_id = data.get("factory_id") or data.get("model_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse(
            {
                "detail": "You do not have access to this factory or model.",
                "code": "forbidden",
                "error": "Factory not found",
            },
            status=404,
        )

    scenario_id = data.get("scenario_id")
    force_generate = bool(
        data.get("force_generate") or data.get("force_refresh")
    )
    analysis_id = data.get("analysis_id") or None
    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Exception:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    org_id = None
    ctx, _err = get_org_context(request)
    if ctx is not None:
        org_id = str(ctx.organization.id)

    service = _build_recommended_actions_service()
    result = service.get_or_generate(
        factory=factory,
        scenario=scenario,
        force_generate=force_generate,
        analysis_id=str(analysis_id) if analysis_id else None,
        organization_id=org_id,
    )
    payload = RecommendedActionsService.to_payload(result)

    if result.needs_simulation:
        payload.setdefault("code", "simulation_result_not_found")
        payload.setdefault(
            "detail",
            "No completed simulation result is available for the selected model and scenario.",
        )
        return JsonResponse(payload, status=200)
    if result.needs_analysis:
        payload.setdefault("code", "analysis_required")
        payload.setdefault(
            "detail",
            "Open Business Impact to generate Problems and Impacts first.",
        )
        return JsonResponse(payload, status=200)
    if not result.success:
        return JsonResponse(payload, status=200)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def managers_playbook(request):
    """
    POST /api/ai/factory-analysis/managers-playbook/

    Body:
      {
        "factory_id": "<uuid>",
        "scenario_id": "<uuid>|null|basecase",
        "analysis_id": "<uuid>|optional",
        "force_generate": false
      }

    Generates Manager's Playbook from validated Problems, Impacts, and
    Recommended Actions already stored for this factory/scenario.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    factory_id = data.get("factory_id") or data.get("model_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse(
            {
                "detail": "You do not have access to this factory or model.",
                "code": "forbidden",
                "error": "Factory not found",
            },
            status=404,
        )

    scenario_id = data.get("scenario_id")
    force_generate = bool(
        data.get("force_generate") or data.get("force_refresh")
    )
    analysis_id = data.get("analysis_id") or None
    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Exception:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    org_id = None
    ctx, _err = get_org_context(request)
    if ctx is not None:
        org_id = str(ctx.organization.id)

    service = _build_managers_playbook_service()
    result = service.get_or_generate(
        factory=factory,
        scenario=scenario,
        force_generate=force_generate,
        analysis_id=str(analysis_id) if analysis_id else None,
        organization_id=org_id,
    )
    payload = ManagersPlaybookService.to_payload(result)

    if result.needs_simulation:
        payload.setdefault("code", "simulation_result_not_found")
        payload.setdefault(
            "detail",
            "No completed simulation result is available for the selected model and scenario.",
        )
        return JsonResponse(payload, status=200)
    if result.needs_analysis:
        payload.setdefault("code", "analysis_required")
        payload.setdefault(
            "detail",
            "Open Business Impact to generate Problems and Impacts first.",
        )
        return JsonResponse(payload, status=200)
    if result.needs_recommendations:
        payload.setdefault("code", "recommendations_required")
        payload.setdefault(
            "detail",
            "Open Recommended Actions to generate them first.",
        )
        return JsonResponse(payload, status=200)
    if not result.success:
        return JsonResponse(payload, status=200)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def chat(request):
    """
    POST /api/ai/chat/

    Body:
      {
        "message": "...",
        "factory_id": "<uuid>",
        "scenario_id": "<uuid>|null|basecase",
        "session_id": "<uuid>|null",
        "force_new": false
      }

    Locked sessions (inactivity / model change) return HTTP 409 with:
      { session_expired|model_changed, message, session }
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    message = (data.get("message") or "").strip()
    factory_id = data.get("factory_id") or data.get("model_id")
    scenario_id = data.get("scenario_id")
    session_id = data.get("session_id")
    force_new = bool(data.get("force_new") or False)

    if not message:
        return JsonResponse({"error": "message is required"}, status=400)
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse({"error": "Factory not found"}, status=404)

    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Scenario.DoesNotExist:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    if session_id:
        try:
            uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid session_id"}, status=400)

    service = _build_chat_service()
    try:
        result = service.ask(
            user=request.user,
            factory=factory,
            scenario=scenario,
            question=message,
            session_id=None if force_new else session_id,
            force_new=force_new,
        )
    except ChatSessionLockedError as exc:
        return _locked_response(exc)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse(
        {
            "session": _session_payload(result.session),
            "user_message": _message_payload(result.user_message),
            "assistant_message": _message_payload(result.assistant_message),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def start_new_session(request):
    """
    POST /api/ai/sessions/new/

    Explicitly start a new ACTIVE chat (used by the New Chat button).
    Reuses an empty session when one exists so blank chats are not duplicated.
    Previous sessions with messages remain available in history.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    factory_id = data.get("factory_id") or data.get("model_id")
    scenario_id = data.get("scenario_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse({"error": "Factory not found"}, status=404)

    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Scenario.DoesNotExist:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    service = _build_chat_service()
    session = service.start_fresh_session(
        user=request.user,
        factory=factory,
        scenario=scenario,
    )
    return JsonResponse({"session": _session_payload(session), "messages": []})


@require_http_methods(["GET"])
def session_history(request):
    """
    GET /api/ai/sessions/history/?factory_id=&scenario_id=

    Lists previous chats for the workspace (newest activity first).
    Soft-validates ACTIVE sessions so expired / model-changed chats show as READ_ONLY.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    factory_id = request.GET.get("factory_id") or request.GET.get("model_id")
    scenario_id = request.GET.get("scenario_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse({"error": "Factory not found"}, status=404)

    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Scenario.DoesNotExist:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    from django.db.models import Count

    qs = ChatSession.objects.filter(user=request.user, factory=factory)
    if scenario is None:
        qs = qs.filter(scenario__isnull=True)
    else:
        qs = qs.filter(scenario=scenario)

    # Never list blank chats — they are workspace placeholders, not history.
    qs = qs.annotate(msg_count=Count("messages")).filter(msg_count__gt=0)

    from apps.ai.services.session_validator import ChatSessionValidator

    validator = ChatSessionValidator()
    sessions = list(qs.order_by("-last_activity_at", "-updated_at")[:40])
    items = []
    for session in sessions:
        if session.status == ChatSession.STATUS_ACTIVE:
            validator.validate(session, factory=factory)
            session.refresh_from_db()
        items.append(_session_history_item(session))

    return JsonResponse({"sessions": items})


@require_http_methods(["GET"])
def session_detail(request, session_id):
    """GET /api/ai/sessions/<session_id>/ — session + messages."""
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    ctx, err = get_org_context(request)
    if err:
        return JsonResponse({"error": "Organization required"}, status=400)

    session = get_object_or_404(
        ChatSession.objects.filter(
            user=request.user,
            factory__organization_id=ctx.organization.id,
        ),
        id=session_id,
    )

    from apps.ai.services.session_validator import ChatSessionValidator

    ChatSessionValidator().validate(session, factory=session.factory)
    session.refresh_from_db()

    messages = [_message_payload(m) for m in session.messages.order_by("created_at")]
    return JsonResponse({"session": _session_payload(session), "messages": messages})


@require_http_methods(["GET"])
def session_for_factory(request):
    """
    GET /api/ai/sessions/?factory_id=&scenario_id=

    Returns the latest session (and messages) for the workspace context, or null.
    Prefer the latest ACTIVE session; otherwise return the most recent (possibly
    READ_ONLY) so history remains visible.
    """
    auth_err = _require_authenticated(request)
    if auth_err:
        return auth_err

    factory_id = request.GET.get("factory_id") or request.GET.get("model_id")
    scenario_id = request.GET.get("scenario_id")
    if not factory_id:
        return JsonResponse({"error": "factory_id is required"}, status=400)

    try:
        factory = _owned_factory_or_404(request, factory_id)
    except RMCMModel.DoesNotExist:
        return JsonResponse({"error": "Factory not found"}, status=404)

    try:
        scenario = _resolve_scenario(factory, scenario_id)
    except Scenario.DoesNotExist:
        return JsonResponse({"error": "Scenario not found"}, status=404)

    qs = ChatSession.objects.filter(user=request.user, factory=factory)
    if scenario is None:
        qs = qs.filter(scenario__isnull=True)
    else:
        qs = qs.filter(scenario=scenario)

    session = (
        qs.filter(status=ChatSession.STATUS_ACTIVE)
        .order_by("-last_activity_at", "-updated_at")
        .first()
        or qs.order_by("-updated_at").first()
    )
    if not session:
        return JsonResponse({"session": None, "messages": []})

    # Soft-validate so the client learns about READ_ONLY without sending a message.
    from apps.ai.services.session_validator import ChatSessionValidator

    ChatSessionValidator().validate(session, factory=factory)
    session.refresh_from_db()

    messages = [_message_payload(m) for m in session.messages.order_by("created_at")]
    return JsonResponse({"session": _session_payload(session), "messages": messages})

