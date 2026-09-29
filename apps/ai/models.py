"""
Conversation persistence for Trooba Flow AI.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.rmct.models import RMCMModel, Scenario


class ChatSession(models.Model):
    """One AI conversation scoped to a user, factory model, and scenario."""

    STATUS_ACTIVE = "ACTIVE"
    STATUS_READ_ONLY = "READ_ONLY"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_READ_ONLY, "Read only"),
    ]

    LOCK_SESSION_TIMEOUT = "SESSION_TIMEOUT"
    LOCK_MODEL_CHANGED = "MODEL_CHANGED"
    LOCK_REASON_CHOICES = [
        (LOCK_SESSION_TIMEOUT, "Session timeout"),
        (LOCK_MODEL_CHANGED, "Model changed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_chat_sessions",
    )
    factory = models.ForeignKey(
        RMCMModel,
        on_delete=models.CASCADE,
        related_name="ai_chat_sessions",
    )
    scenario = models.ForeignKey(
        Scenario,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ai_chat_sessions",
    )
    conversation_summary = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        db_index=True,
    )
    lock_reason = models.CharField(
        max_length=32,
        choices=LOCK_REASON_CHOICES,
        null=True,
        blank=True,
        default=None,
    )
    # Fingerprint of saved factory + what-if state when the session started.
    model_version = models.CharField(max_length=255, blank=True, default="")
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "factory", "-updated_at"]),
            models.Index(fields=["user", "factory", "status", "-last_activity_at"]),
        ]

    @property
    def is_active(self) -> bool:
        return self.status == self.STATUS_ACTIVE

    @property
    def is_read_only(self) -> bool:
        return self.status == self.STATUS_READ_ONLY

    def __str__(self) -> str:
        return f"ChatSession {self.id} ({self.user_id} / {self.factory_id})"


class ChatMessage(models.Model):
    """A single turn in an AI chat session."""

    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [
        (ROLE_USER, "User"),
        (ROLE_ASSISTANT, "Assistant"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.role}: {self.message[:48]}"


class FactoryBriefing(models.Model):
    """
    Cached AI factory briefing for a factory scenario simulation result.

    One row per (factory, scenario). Regenerated when that scenario's
    result version changes. Independent of chat sessions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    factory = models.ForeignKey(
        RMCMModel,
        on_delete=models.CASCADE,
        related_name="ai_factory_briefings",
    )
    scenario = models.ForeignKey(
        "rmct.Scenario",
        on_delete=models.CASCADE,
        related_name="ai_factory_briefings",
    )
    briefing = models.TextField()
    paragraphs = models.JSONField(default=list)
    result_version = models.CharField(max_length=255, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    generated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["factory", "scenario"],
                name="ai_factorybriefing_factory_scenario_uniq",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"FactoryBriefing {self.factory_id}/{self.scenario_id} "
            f"({self.result_version[:16]})"
        )


class FactoryAnalysis(models.Model):
    """
    Cached AI factory analysis (Problems Identified + Business Impact).

    One row per (factory, scenario). Regenerated when that scenario's
    result_version or prompt_version changes. Independent of chat sessions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    factory = models.ForeignKey(
        RMCMModel,
        on_delete=models.CASCADE,
        related_name="ai_factory_analyses",
    )
    scenario = models.ForeignKey(
        "rmct.Scenario",
        on_delete=models.CASCADE,
        related_name="ai_factory_analyses",
    )
    analysis = models.JSONField(default=dict)
    result_version = models.CharField(max_length=255, blank=True, default="")
    prompt_version = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    source = models.CharField(max_length=64, blank=True, default="ai")
    generated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["factory", "scenario"],
                name="ai_factoryanalysis_factory_scenario_uniq",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"FactoryAnalysis {self.factory_id}/{self.scenario_id} "
            f"({self.result_version[:16]})"
        )


class RecommendedActions(models.Model):
    """
    Cached AI Recommended Actions for a factory scenario.

    Depends on a validated FactoryAnalysis (problems + impacts).
    One row per (factory, scenario). Regenerated when simulation version,
    parent analysis fingerprint, or recommendation prompt_version changes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    factory = models.ForeignKey(
        RMCMModel,
        on_delete=models.CASCADE,
        related_name="ai_recommended_actions",
    )
    scenario = models.ForeignKey(
        "rmct.Scenario",
        on_delete=models.CASCADE,
        related_name="ai_recommended_actions",
    )
    analysis = models.ForeignKey(
        FactoryAnalysis,
        on_delete=models.CASCADE,
        related_name="recommended_actions_rows",
        null=True,
        blank=True,
    )
    recommendations = models.JSONField(default=dict)
    result_version = models.CharField(max_length=255, blank=True, default="")
    analysis_fingerprint = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    source = models.CharField(max_length=64, blank=True, default="ai")
    generated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["factory", "scenario"],
                name="ai_recommendedactions_factory_scenario_uniq",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"RecommendedActions {self.factory_id}/{self.scenario_id} "
            f"({self.result_version[:16]})"
        )


class ManagersPlaybook(models.Model):
    """
    Cached AI Manager's Playbook for a factory scenario.

    Depends on validated FactoryAnalysis and RecommendedActions.
    One row per (factory, scenario). Regenerated when simulation version,
    parent analysis/recommendations fingerprint, or playbook prompt_version changes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    factory = models.ForeignKey(
        RMCMModel,
        on_delete=models.CASCADE,
        related_name="ai_managers_playbooks",
    )
    scenario = models.ForeignKey(
        "rmct.Scenario",
        on_delete=models.CASCADE,
        related_name="ai_managers_playbooks",
    )
    analysis = models.ForeignKey(
        FactoryAnalysis,
        on_delete=models.CASCADE,
        related_name="managers_playbook_rows",
        null=True,
        blank=True,
    )
    recommended_actions = models.ForeignKey(
        RecommendedActions,
        on_delete=models.CASCADE,
        related_name="managers_playbook_rows",
        null=True,
        blank=True,
    )
    playbook = models.JSONField(default=dict)
    result_version = models.CharField(max_length=255, blank=True, default="")
    playbook_fingerprint = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    source = models.CharField(max_length=64, blank=True, default="ai")
    generated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["factory", "scenario"],
                name="ai_managersplaybook_factory_scenario_uniq",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"ManagersPlaybook {self.factory_id}/{self.scenario_id} "
            f"({self.result_version[:16]})"
        )
