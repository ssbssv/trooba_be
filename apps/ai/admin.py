from django.contrib import admin

from .models import ChatMessage, ChatSession, FactoryAnalysis, FactoryBriefing


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("id", "role", "message", "created_at")
    can_delete = False


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "factory",
        "scenario",
        "status",
        "lock_reason",
        "last_activity_at",
        "updated_at",
        "created_at",
    )
    list_filter = ("status", "lock_reason", "created_at", "updated_at")
    search_fields = ("id", "user__username", "user__email", "factory__name", "model_version")
    readonly_fields = ("id", "created_at", "updated_at", "last_activity_at", "model_version")
    inlines = [ChatMessageInline]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "role", "created_at")
    list_filter = ("role", "created_at")
    search_fields = ("message", "session__id")
    readonly_fields = ("id", "created_at")


@admin.register(FactoryBriefing)
class FactoryBriefingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "factory",
        "scenario",
        "model_name",
        "result_version",
        "generated_at",
        "updated_at",
    )
    list_filter = ("generated_at", "updated_at")
    search_fields = (
        "id",
        "factory__name",
        "scenario__name",
        "result_version",
        "model_name",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "generated_at",
        "result_version",
        "briefing",
        "paragraphs",
        "model_name",
    )


@admin.register(FactoryAnalysis)
class FactoryAnalysisAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "factory",
        "scenario",
        "model_name",
        "source",
        "result_version",
        "prompt_version",
        "generated_at",
        "updated_at",
    )
    list_filter = ("source", "generated_at", "updated_at")
    search_fields = (
        "id",
        "factory__name",
        "scenario__name",
        "result_version",
        "prompt_version",
        "model_name",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "generated_at",
        "result_version",
        "prompt_version",
        "analysis",
        "model_name",
        "source",
    )

