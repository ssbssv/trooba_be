"""
AIContextBuilder — loads factory input/output, what-if deltas, and conversation context.

What-if input overrides are sent as compact change lists (not a full input overlay)
to keep token usage low while still covering every saved scenario.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import Prefetch

from apps.ai.models import ChatMessage, ChatSession
from apps.rmct.models import RMCMModel, Scenario, ScenarioChange, ScenarioResult


class AIContextBuilder:
    """Build the structured context payload sent to the LLM."""

    def __init__(self, *, max_recent_messages: int | None = None) -> None:
        self.max_recent_messages = (
            max_recent_messages
            if max_recent_messages is not None
            else int(getattr(settings, "MAX_RECENT_MESSAGES", 12))
        )

    def build(
        self,
        *,
        factory: RMCMModel,
        scenario: Scenario | None,
        session: ChatSession | None,
        current_question: str,
    ) -> dict[str, Any]:
        basecase = self._basecase_scenario(factory)
        return {
            "factory_input": self.load_factory_input(factory),
            "factory_output": self.load_factory_output(factory, basecase),
            "whatif_scenarios": self.load_whatif_scenarios(factory),
            "scenario": self.load_scenario(scenario),
            "conversation_summary": (
                (session.conversation_summary or "") if session else ""
            ),
            "recent_messages": self.load_recent_messages(session),
            "current_question": current_question,
        }

    def load_factory_input(self, factory: RMCMModel) -> dict[str, Any]:
        """
        Load the COMPLETE factory input JSON exactly as stored (basecase).
        Do not remove, summarize, or transform fields.
        """
        from apps.rmct.views import _build_snapshot_from_model, _model_to_payload

        snapshot = _build_snapshot_from_model(factory)
        meta = _model_to_payload(factory)
        return {
            **meta,
            **snapshot,
        }

    def load_scenario_factory_input(
        self,
        factory: RMCMModel,
        scenario: Scenario | None,
    ) -> dict[str, Any]:
        """
        Complete factory input as the user sees it for the selected scenario.

        Basecase → stored model snapshot.
        What-if → basecase snapshot with that scenario's changes applied.
        """
        base_input = self.load_factory_input(factory)
        if scenario is None or scenario.is_basecase:
            return base_input

        from apps.simulations.full_calculate import apply_scenario

        changes = [
            {
                "dataType": change.data_type,
                "entityId": change.entity_id,
                "field": change.field_name,
                "whatIfValue": change.whatif_value,
            }
            for change in scenario.changes.all().order_by("created_at", "id")
        ]
        overlay = apply_scenario(base_input, {"changes": changes})
        return overlay

    def load_factory_output(
        self,
        factory: RMCMModel,
        scenario: Scenario | None,
    ) -> dict[str, Any] | list[Any] | None:
        """Load calculated output JSON for the given scenario (basecase by default)."""
        target = scenario
        if target is None:
            target = self._basecase_scenario(factory)
        if target is None:
            return None
        try:
            result = target.result
        except ScenarioResult.DoesNotExist:
            return None
        return result.results

    def load_whatif_scenarios(self, factory: RMCMModel) -> list[dict[str, Any]]:
        """
        Saved what-if scenarios with compact input deltas + full calculated output.

        Deltas are small (only changed fields). Full outputs are included so the
        user can ask about any scenario without being limited to the active tab.
        """
        changes_qs = ScenarioChange.objects.order_by("created_at", "id")
        scenarios = (
            Scenario.objects.filter(model=factory, is_basecase=False)
            .prefetch_related(Prefetch("changes", queryset=changes_qs))
            .select_related("result")
            .order_by("name", "created_at")
        )
        payload: list[dict[str, Any]] = []
        for row in scenarios:
            try:
                output = row.result.results
            except ScenarioResult.DoesNotExist:
                output = None
            payload.append(
                {
                    "id": str(row.id),
                    "name": row.name,
                    "status": row.status or "",
                    "description": row.description or "",
                    "changes": [
                        {
                            "type": change.data_type,
                            "entity": change.entity_name or change.entity_id,
                            "field": change.field_name,
                            "base": change.basecase_value,
                            "whatif": change.whatif_value,
                        }
                        for change in row.changes.all()
                    ],
                    "output": output,
                }
            )
        return payload

    def load_scenario(self, scenario: Scenario | None) -> dict[str, Any]:
        if scenario is None:
            return {
                "id": None,
                "name": None,
                "is_current": True,
                "is_basecase": True,
                "description": "Basecase (current factory model)",
                "status": None,
            }
        return {
            "id": str(scenario.id),
            "name": scenario.name,
            "is_current": True,
            "is_basecase": bool(scenario.is_basecase),
            "description": scenario.description or "",
            "status": scenario.status or "",
        }

    def load_recent_messages(
        self,
        session: ChatSession | None,
    ) -> list[dict[str, Any]]:
        if session is None:
            return []
        qs = ChatMessage.objects.filter(session=session).order_by("-created_at")[
            : self.max_recent_messages
        ]
        rows = list(reversed(list(qs)))
        return [
            {
                "role": row.role,
                "message": row.message,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    @staticmethod
    def _basecase_scenario(factory: RMCMModel) -> Scenario | None:
        return Scenario.objects.filter(model=factory, is_basecase=True).first()
