"""
Conversation memory — persist turns and summarize when thresholds are exceeded.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai.llm.base import LLMClient, LLMMessage
from apps.ai.models import ChatMessage, ChatSession
from apps.ai.prompts import SUMMARY_SYSTEM_PROMPT


class ConversationMemoryService:
    """Save messages and maintain ChatSession.conversation_summary."""

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client
        self.summary_threshold_messages = int(
            getattr(settings, "SUMMARY_THRESHOLD_MESSAGES", 20)
        )
        self.summary_threshold_tokens = int(
            getattr(settings, "SUMMARY_THRESHOLD_TOKENS", 6000)
        )
        self.max_recent_messages = int(getattr(settings, "MAX_RECENT_MESSAGES", 12))

    def save_user_message(self, session: ChatSession, message: str) -> ChatMessage:
        return self._save(session, ChatMessage.ROLE_USER, message)

    def save_assistant_message(self, session: ChatSession, message: str) -> ChatMessage:
        return self._save(session, ChatMessage.ROLE_ASSISTANT, message)

    def _save(self, session: ChatSession, role: str, message: str) -> ChatMessage:
        with transaction.atomic():
            row = ChatMessage.objects.create(
                session=session,
                role=role,
                message=message,
            )
            now = timezone.now()
            # User and assistant turns both count as activity (inactivity-only timeout).
            ChatSession.objects.filter(pk=session.pk).update(
                last_activity_at=now,
                updated_at=now,
            )
            session.last_activity_at = now
            session.updated_at = now
        return row

    def maybe_summarize(self, session: ChatSession) -> None:
        """
        When conversation exceeds configurable thresholds, generate a summary
        that preserves objective / products / bottlenecks / assumptions /
        recommendations / discussion.

        After summarization, LLM context uses Conversation Summary + Recent Messages.
        Older messages remain stored in the database.
        """
        messages = list(
            ChatMessage.objects.filter(session=session).order_by("created_at")
        )
        if len(messages) <= self.max_recent_messages:
            return

        transcript = self._format_transcript(messages)
        token_count = self.llm_client.estimate_tokens(transcript)
        over_message_threshold = len(messages) >= self.summary_threshold_messages
        over_token_threshold = token_count >= self.summary_threshold_tokens
        if not (over_message_threshold or over_token_threshold):
            return

        older = messages[: -self.max_recent_messages]
        older_text = self._format_transcript(older)
        existing = (session.conversation_summary or "").strip()

        summary_user_payload = {
            "existing_summary": existing,
            "messages_to_summarize": older_text,
            "instructions": (
                "Merge existing_summary with messages_to_summarize. "
                "Preserve current objective, products, bottlenecks, assumptions, "
                "recommendations, and discussion. Discard greetings and repeats."
            ),
        }

        response = self.llm_client.chat(
            [
                LLMMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
                LLMMessage(role="user", content=summary_user_payload),
            ],
            temperature=0.2,
            max_tokens=800,
        )
        session.conversation_summary = (response.content or "").strip()
        session.save(update_fields=["conversation_summary", "updated_at"])

    @staticmethod
    def _format_transcript(messages: list[ChatMessage]) -> str:
        return "\n".join(f"{m.role}: {m.message}" for m in messages)
