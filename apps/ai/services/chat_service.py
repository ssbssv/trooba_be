"""
ChatService — orchestrates context building, LLM calls, and conversation storage.

No tool calling in Version 1. Architecture leaves room for Version 2 tools
without changing this service's public contract.

Session lifecycle (saved factory / what-if data fingerprint) is validated via
ChatSessionValidator before any LLM request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Count
from django.utils import timezone

from apps.ai.llm.base import LLMClient, LLMMessage
from apps.ai.llm.http import LLMProviderError
from apps.ai.models import ChatMessage, ChatSession
from apps.ai.prompts import SYSTEM_PROMPT
from apps.ai.services.context_builder import AIContextBuilder
from apps.ai.services.conversation_memory import ConversationMemoryService
from apps.ai.services.session_validator import (
    ChatSessionValidator,
    SessionValidationResult,
    resolve_factory_model_version,
)
from apps.rmct.models import RMCMModel, Scenario


class ChatSessionLockedError(Exception):
    """Raised when a chat cannot accept new messages (saved data changed)."""

    def __init__(self, result: SessionValidationResult) -> None:
        super().__init__(result.message or "Chat session is read-only")
        self.result = result


@dataclass
class ChatTurnResult:
    session: ChatSession
    user_message: ChatMessage
    assistant_message: ChatMessage


class ChatService:
    """
    Application service for Trooba Flow AI chat.

    Dependencies are injected so providers and builders can be swapped in tests
    or future versions (e.g. tool-calling LLMClient).
    """

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        context_builder: AIContextBuilder | None = None,
        memory: ConversationMemoryService | None = None,
        session_validator: ChatSessionValidator | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.context_builder = context_builder or AIContextBuilder()
        self.memory = memory or ConversationMemoryService(llm_client)
        self.session_validator = session_validator or ChatSessionValidator()

    def _workspace_sessions(
        self,
        *,
        user: AbstractBaseUser,
        factory: RMCMModel,
        scenario: Scenario | None,
    ):
        qs = ChatSession.objects.filter(user=user, factory=factory)
        if scenario is None:
            return qs.filter(scenario__isnull=True)
        return qs.filter(scenario=scenario)

    def create_session(
        self,
        *,
        user: AbstractBaseUser,
        factory: RMCMModel,
        scenario: Scenario | None,
    ) -> ChatSession:
        """Start a fresh ACTIVE chat for the workspace context."""
        now = timezone.now()
        return ChatSession.objects.create(
            user=user,
            factory=factory,
            scenario=scenario,
            conversation_summary="",
            status=ChatSession.STATUS_ACTIVE,
            lock_reason=None,
            model_version=resolve_factory_model_version(factory),
            last_activity_at=now,
        )

    def start_fresh_session(
        self,
        *,
        user: AbstractBaseUser,
        factory: RMCMModel,
        scenario: Scenario | None,
    ) -> ChatSession:
        """
        Explicit New Chat.

        Reuse an existing empty session (0 messages) so history never fills with
        blank chats. Only create a new row when every prior session has messages.
        """
        qs = self._workspace_sessions(user=user, factory=factory, scenario=scenario)
        empty = (
            qs.annotate(msg_count=Count("messages"))
            .filter(msg_count=0)
            .order_by("-updated_at")
            .first()
        )
        now = timezone.now()
        version = resolve_factory_model_version(factory)

        if empty is not None:
            empty.status = ChatSession.STATUS_ACTIVE
            empty.lock_reason = None
            empty.model_version = version
            empty.conversation_summary = ""
            empty.last_activity_at = now
            empty.save(
                update_fields=[
                    "status",
                    "lock_reason",
                    "model_version",
                    "conversation_summary",
                    "last_activity_at",
                    "updated_at",
                ]
            )
            (
                qs.annotate(msg_count=Count("messages"))
                .filter(msg_count=0)
                .exclude(pk=empty.pk)
                .delete()
            )
            return empty

        return self.create_session(user=user, factory=factory, scenario=scenario)

    def get_or_create_session(
        self,
        *,
        user: AbstractBaseUser,
        factory: RMCMModel,
        scenario: Scenario | None,
        session_id: UUID | str | None = None,
        force_new: bool = False,
    ) -> ChatSession:
        if force_new:
            return self.start_fresh_session(
                user=user, factory=factory, scenario=scenario
            )

        if session_id:
            session = ChatSession.objects.filter(
                id=session_id,
                user=user,
                factory=factory,
            ).first()
            if session is not None:
                # Keep scenario aligned with the active workspace selection.
                if session.scenario_id != (scenario.id if scenario else None):
                    session.scenario = scenario
                    session.save(update_fields=["scenario", "updated_at"])
                return session

        # Prefer the most recent ACTIVE session for this user/factory/scenario.
        qs = self._workspace_sessions(user=user, factory=factory, scenario=scenario)

        existing = qs.filter(status=ChatSession.STATUS_ACTIVE).order_by(
            "-last_activity_at", "-updated_at"
        ).first()
        if existing:
            return existing

        # No ACTIVE chat — start a new one. Do not resume READ_ONLY history here;
        # ask() would immediately 409, and New Chat should get a writable session.
        return self.create_session(user=user, factory=factory, scenario=scenario)

    def ask(
        self,
        *,
        user: AbstractBaseUser,
        factory: RMCMModel,
        scenario: Scenario | None,
        question: str,
        session_id: UUID | str | None = None,
        force_new: bool = False,
    ) -> ChatTurnResult:
        question = (question or "").strip()
        if not question:
            raise ValueError("message is required")

        session = self.get_or_create_session(
            user=user,
            factory=factory,
            scenario=scenario,
            session_id=session_id,
            force_new=force_new,
        )

        validation = self.session_validator.validate(session, factory=factory)
        if not validation.ok:
            raise ChatSessionLockedError(validation)

        # Build context before persisting the new user turn so recent_messages
        # contains prior history only; current_question is sent separately.
        context = self.context_builder.build(
            factory=factory,
            scenario=scenario,
            session=session,
            current_question=question,
        )
        messages = self._build_llm_messages(context)

        user_row = self.memory.save_user_message(session, question)
        
        
        try:
            response = self.llm_client.chat(messages)
            assistant_text = (response.content or "").strip()
            if not assistant_text:
                assistant_text = "ai is out of service pls try again later"
        except LLMProviderError:
            assistant_text = "ai is out of service pls try again later"

        assistant_row = self.memory.save_assistant_message(session, assistant_text)

        # Summarize after the turn is persisted so thresholds see the new messages.
        try:
            self.memory.maybe_summarize(session)
        except LLMProviderError:
            # Summarization failure must not break the user-facing reply.
            pass

        session.refresh_from_db()
        return ChatTurnResult(
            session=session,
            user_message=user_row,
            assistant_message=assistant_row,
        )

    def list_messages(self, session: ChatSession) -> list[ChatMessage]:
        return list(session.messages.order_by("created_at"))

    @staticmethod
    def _build_llm_messages(context: dict[str, Any]) -> list[LLMMessage]:
        """
        Build provider-agnostic messages with structured JSON user content.

        messages = [
          { role: "system", content: SYSTEM_PROMPT },
          { role: "user", content: { factory_input, factory_output, ... } },
        ]
        """
        return [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content={
                    "factory_input": context["factory_input"],
                    "factory_output": context["factory_output"],
                    "whatif_scenarios": context["whatif_scenarios"],
                    "scenario": context["scenario"],
                    "conversation_summary": context["conversation_summary"],
                    "recent_messages": context["recent_messages"],
                    "current_question": context["current_question"],
                },
            ),
        ]

