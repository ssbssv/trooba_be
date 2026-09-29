from django.urls import path

from . import views

urlpatterns = [
    path("chat/", views.chat, name="ai-chat"),
    path("factory-briefing/", views.factory_briefing, name="ai-factory-briefing"),
    path("factory-analysis/", views.factory_analysis, name="ai-factory-analysis"),
    path(
        "factory-analysis/recommended-actions/",
        views.recommended_actions,
        name="ai-recommended-actions",
    ),
    path(
        "factory-analysis/managers-playbook/",
        views.managers_playbook,
        name="ai-managers-playbook",
    ),
    path("sessions/", views.session_for_factory, name="ai-sessions"),
    path("sessions/new/", views.start_new_session, name="ai-sessions-new"),
    path("sessions/history/", views.session_history, name="ai-sessions-history"),
    path("sessions/<uuid:session_id>/", views.session_detail, name="ai-session-detail"),
]
