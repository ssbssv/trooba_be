"""
ChatSessionValidator — lock chats when saved factory / what-if data changes.

Inactivity timeouts are intentionally not used; a session stays open until
saved model or scenario data diverges from the fingerprint pinned at start.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.utils import timezone

from apps.ai.models import ChatSession
from apps.rmct.models import RMCMModel, Scenario, ScenarioChange, ScenarioResult

MODEL_CHANGED_MESSAGE = (
    "The factory or what-if data has changed since this conversation started. "
    "Please create a new conversation to discuss the latest saved data."
)

# Kept for older READ_ONLY rows that were locked by the removed inactivity path.
SESSION_TIMEOUT_MESSAGE = MODEL_CHANGED_MESSAGE

# Legacy settings / Option B — inactivity auto-replace is no longer used.
EXPIRY_BEHAVIOR_PROMPT_NEW = "prompt_new"
EXPIRY_BEHAVIOR_AUTO_NEW = "auto_new"


def resolve_factory_model_version(factory: RMCMModel) -> str:
    """
    Fingerprint of saved factory + what-if state.

    Bumps when basecase model metadata changes, scenarios/changes are saved,
    or calculated results are written — so chats pin to the data that was
    current when the conversation started.
    """
    h = hashlib.sha256()

    def _add(value: object) -> None:
        h.update(str(value if value is not None else "").encode("utf-8"))
        h.update(b"\0")

    _add(factory.id)
    _add(factory.updated_at.isoformat() if factory.updated_at else "")
    _add(factory.last_run_at.isoformat() if factory.last_run_at else "")
    _add(factory.run_status or "")

    scenarios = (
        Scenario.objects.filter(model=factory)
        .order_by("id")
        .values_list("id", "updated_at", "status", "is_basecase", "name")
    )
    for sid, updated, status, is_basecase, name in scenarios:
        _add(sid)
        _add(updated.isoformat() if updated else "")
        _add(status)
        _add(int(bool(is_basecase)))
        _add(name)

    changes = (
        ScenarioChange.objects.filter(scenario__model=factory)
        .order_by("id")
        .values_list(
            "id",
            "scenario_id",
            "data_type",
            "entity_id",
            "entity_name",
            "field_name",
            "basecase_value",
            "whatif_value",
            "updated_at",
        )
    )
    for row in changes:
        for cell in row:
            if hasattr(cell, "isoformat"):
                _add(cell.isoformat())
            else:
                _add(cell)

    results = (
        ScenarioResult.objects.filter(scenario__model=factory)
        .order_by("scenario_id")
        .values_list("scenario_id", "created_at")
    )
    for scenario_id, created_at in results:
        _add(scenario_id)
        _add(created_at.isoformat() if created_at else "")

    return h.hexdigest()


def expiry_behavior() -> str:
    """Compatibility shim — inactivity expiry is disabled."""
    return EXPIRY_BEHAVIOR_PROMPT_NEW


@dataclass
class SessionValidationResult:
    """Outcome of validating a chat session before an LLM turn."""

    ok: bool
    session: ChatSession
    session_expired: bool = False
    model_changed: bool = False
    message: str = ""
    replacement_session: ChatSession | None = None

    @property
    def should_auto_replace(self) -> bool:
        # Inactivity auto-replace removed; model-change always requires New Chat.
        return False


class ChatSessionValidator:
    """Validate model/what-if version. Lock outdated chats as READ_ONLY."""

    def validate(
        self,
        session: ChatSession,
        *,
        factory: RMCMModel,
    ) -> SessionValidationResult:
        if session.status == ChatSession.STATUS_READ_ONLY:
            return self._result_for_locked(session)

        return self._check_model_version(session, factory)

    def lock_session(
        self,
        session: ChatSession,
        *,
        reason: str,
    ) -> ChatSession:
        if (
            session.status == ChatSession.STATUS_READ_ONLY
            and session.lock_reason == reason
        ):
            return session

        session.status = ChatSession.STATUS_READ_ONLY
        session.lock_reason = reason
        session.save(update_fields=["status", "lock_reason", "updated_at"])
        return session

    def touch_activity(self, session: ChatSession) -> None:
        now = timezone.now()
        ChatSession.objects.filter(pk=session.pk).update(
            last_activity_at=now,
            updated_at=now,
        )
        session.last_activity_at = now
        session.updated_at = now

    def _check_model_version(
        self,
        session: ChatSession,
        factory: RMCMModel,
    ) -> SessionValidationResult:
        current = resolve_factory_model_version(factory)
        stored = (session.model_version or "").strip()
        if not stored:
            # Legacy / missing fingerprint: pin to current without locking.
            session.model_version = current
            session.save(update_fields=["model_version", "updated_at"])
            return SessionValidationResult(ok=True, session=session)

        if stored == current:
            return SessionValidationResult(ok=True, session=session)

        # Blank chats have no prior Q&A to protect — repin to the latest
        # fingerprint instead of forcing a New Chat prompt.
        if not session.messages.exists():
            session.model_version = current
            session.save(update_fields=["model_version", "updated_at"])
            return SessionValidationResult(ok=True, session=session)

        self.lock_session(session, reason=ChatSession.LOCK_MODEL_CHANGED)
        return SessionValidationResult(
            ok=False,
            session=session,
            model_changed=True,
            message=MODEL_CHANGED_MESSAGE,
        )

    def _result_for_locked(self, session: ChatSession) -> SessionValidationResult:
        if session.lock_reason == ChatSession.LOCK_MODEL_CHANGED:
            return SessionValidationResult(
                ok=False,
                session=session,
                model_changed=True,
                message=MODEL_CHANGED_MESSAGE,
            )
        # Legacy SESSION_TIMEOUT locks — same UX as data-changed.
        return SessionValidationResult(
            ok=False,
            session=session,
            model_changed=True,
            message=MODEL_CHANGED_MESSAGE,
        )
