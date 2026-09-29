"""
ManagersPlaybookService — ordered manager actions from validated analysis + recommendations.

Requires existing FactoryAnalysis and RecommendedActions for the same factory + scenario.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai.llm.base import LLMClient, LLMMessage
from apps.ai.llm.http import LLMProviderError
from apps.ai.models import FactoryAnalysis, ManagersPlaybook, RecommendedActions
from apps.ai.prompts.managers_playbook import (
    MANAGERS_PLAYBOOK_PROMPT_VERSION,
    MANAGERS_PLAYBOOK_SYSTEM_PROMPT,
    build_managers_playbook_user_prompt,
)
from apps.ai.services.context_builder import AIContextBuilder
from apps.ai.services.factory_analysis import (
    extract_json_object,
    normalize_simulation_for_analysis,
)
from apps.ai.services.factory_briefing_service import (
    GENERATE_WITH_EXISTING_MESSAGE,
    NEEDS_SIMULATION_MESSAGE,
    ensure_basecase_scenario,
    resolve_scenario_result_version,
)
from apps.ai.services.managers_playbook import (
    build_deterministic_managers_playbook,
    build_managers_playbook_input,
    playbook_cache_fingerprint,
    validate_managers_playbook_payload,
)
from apps.rmct.models import RMCMModel, Scenario, ScenarioResult

logger = logging.getLogger(__name__)

GENERATION_FAILED_MESSAGE = (
    "Something went wrong while generating the Manager's Playbook. Please try again."
)

NEEDS_ANALYSIS_MESSAGE = (
    "Manager's Playbook needs validated Problems and Business Impacts first. "
    "Open the Business Impact section to generate them, then return here."
)

NEEDS_RECOMMENDATIONS_MESSAGE = (
    "Manager's Playbook needs Recommended Actions first. "
    "Open the Recommended Actions section to generate them, then return here."
)

STALE_MESSAGE = (
    "You've changed the factory data since this playbook was generated. "
    "These actions are based on the last simulation run. "
    "Run the model again to get an updated playbook."
)


@dataclass
class ManagersPlaybookResult:
    success: bool
    analysis_id: str | None
    factory_id: str
    model_id: str
    scenario_id: str | None
    simulation_version: str
    generated_at: str | None
    calculated_at: str | None
    cache_hit: bool
    is_stale: bool
    stale_message: str
    needs_simulation: bool
    needs_analysis: bool
    needs_recommendations: bool
    can_generate_with_existing: bool
    playbook_status: str
    overview: str
    action_count: int
    manager_actions: list[dict[str, Any]] = field(default_factory=list)
    final_summary: str = ""
    source: str = "ai"
    model: str = ""
    prompt_version: str = MANAGERS_PLAYBOOK_PROMPT_VERSION
    error: str = ""


def _extract_calculated_at(scenario: Scenario | None) -> str | None:
    if scenario is None:
        return None
    try:
        result = scenario.result
    except ScenarioResult.DoesNotExist:
        return None
    results = result.results
    if isinstance(results, dict):
        value = results.get("calculatedAt")
        if value:
            return str(value)
    if result.created_at:
        return result.created_at.isoformat()
    return None


def _scenario_is_stale(factory: RMCMModel, scenario: Scenario) -> bool:
    if scenario.is_basecase:
        return (factory.run_status or "") == "needs_recalc"
    return (scenario.status or "") == "needs_recalc"


def _time_unit_from_factory(factory: RMCMModel) -> str:
    return "days"


def _analysis_has_content(row: FactoryAnalysis) -> bool:
    payload = row.analysis if isinstance(row.analysis, dict) else {}
    problems = payload.get("problems") if isinstance(payload.get("problems"), list) else []
    impacts = (
        payload.get("business_impacts")
        if isinstance(payload.get("business_impacts"), list)
        else []
    )
    return len(problems) > 0 and len(impacts) > 0


def _recommendations_have_content(row: RecommendedActions) -> bool:
    payload = row.recommendations if isinstance(row.recommendations, dict) else {}
    actions = (
        payload.get("recommended_actions")
        if isinstance(payload.get("recommended_actions"), list)
        else []
    )
    return len(actions) > 0


class ManagersPlaybookService:
    def __init__(
        self,
        *,
        llm_client: LLMClient,
        context_builder: AIContextBuilder | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.context_builder = context_builder or AIContextBuilder()

    def get_or_generate(
        self,
        *,
        factory: RMCMModel,
        scenario: Scenario | None = None,
        force_generate: bool = False,
        analysis_id: str | None = None,
        organization_id: str | None = None,
    ) -> ManagersPlaybookResult:
        target = scenario
        if target is None or getattr(target, "pk", None) is None:
            target = ensure_basecase_scenario(factory)

        factory_id = str(factory.id)
        scenario_id = str(target.id)
        calculated_at = _extract_calculated_at(target)
        prompt_version = (
            getattr(settings, "MANAGERS_PLAYBOOK_PROMPT_VERSION", None)
            or MANAGERS_PLAYBOOK_PROMPT_VERSION
        )

        result_version = resolve_scenario_result_version(target)
        if not result_version:
            return ManagersPlaybookResult(
                success=False,
                analysis_id=None,
                factory_id=factory_id,
                model_id=factory_id,
                scenario_id=scenario_id,
                simulation_version="",
                generated_at=None,
                calculated_at=None,
                cache_hit=False,
                is_stale=False,
                stale_message="",
                needs_simulation=True,
                needs_analysis=False,
                needs_recommendations=False,
                can_generate_with_existing=False,
                playbook_status="unavailable",
                overview="",
                action_count=0,
                error=NEEDS_SIMULATION_MESSAGE,
            )

        analysis_qs = FactoryAnalysis.objects.filter(factory=factory, scenario=target)
        if analysis_id:
            analysis_row = analysis_qs.filter(id=analysis_id).first()
        else:
            analysis_row = analysis_qs.order_by("-generated_at").first()

        if (
            analysis_row is None
            or analysis_row.result_version != result_version
            or not _analysis_has_content(analysis_row)
        ):
            return ManagersPlaybookResult(
                success=False,
                analysis_id=str(analysis_row.id) if analysis_row else None,
                factory_id=factory_id,
                model_id=factory_id,
                scenario_id=scenario_id,
                simulation_version=result_version,
                generated_at=None,
                calculated_at=calculated_at,
                cache_hit=False,
                is_stale=False,
                stale_message="",
                needs_simulation=False,
                needs_analysis=True,
                needs_recommendations=False,
                can_generate_with_existing=False,
                playbook_status="unavailable",
                overview="",
                action_count=0,
                error=NEEDS_ANALYSIS_MESSAGE,
            )

        ra_row = (
            RecommendedActions.objects.filter(factory=factory, scenario=target)
            .order_by("-generated_at")
            .first()
        )
        if (
            ra_row is None
            or ra_row.result_version != result_version
            or not _recommendations_have_content(ra_row)
        ):
            return ManagersPlaybookResult(
                success=False,
                analysis_id=str(analysis_row.id),
                factory_id=factory_id,
                model_id=factory_id,
                scenario_id=scenario_id,
                simulation_version=result_version,
                generated_at=None,
                calculated_at=calculated_at,
                cache_hit=False,
                is_stale=False,
                stale_message="",
                needs_simulation=False,
                needs_analysis=False,
                needs_recommendations=True,
                can_generate_with_existing=False,
                playbook_status="unavailable",
                overview="",
                action_count=0,
                error=NEEDS_RECOMMENDATIONS_MESSAGE,
            )

        fingerprint = playbook_cache_fingerprint(
            analysis_id=str(analysis_row.id),
            analysis_payload=(
                analysis_row.analysis if isinstance(analysis_row.analysis, dict) else {}
            ),
            recommendations_payload=(
                ra_row.recommendations
                if isinstance(ra_row.recommendations, dict)
                else {}
            ),
        )

        is_stale = _scenario_is_stale(factory, target)
        stale_message = STALE_MESSAGE if is_stale else ""

        cached = (
            ManagersPlaybook.objects.filter(factory=factory, scenario=target)
            .order_by("-generated_at")
            .first()
        )
        if (
            cached is not None
            and cached.result_version == result_version
            and cached.playbook_fingerprint == fingerprint
            and cached.prompt_version == prompt_version
            and not force_generate
        ):
            return self._from_row(
                cached,
                cache_hit=True,
                is_stale=is_stale,
                stale_message=stale_message,
                calculated_at=calculated_at,
                analysis_id=str(analysis_row.id),
            )

        if is_stale and not force_generate:
            return ManagersPlaybookResult(
                success=False,
                analysis_id=str(analysis_row.id),
                factory_id=factory_id,
                model_id=factory_id,
                scenario_id=scenario_id,
                simulation_version=result_version,
                generated_at=None,
                calculated_at=calculated_at,
                cache_hit=False,
                is_stale=True,
                stale_message="",
                needs_simulation=False,
                needs_analysis=False,
                needs_recommendations=False,
                can_generate_with_existing=True,
                playbook_status="unavailable",
                overview="",
                action_count=0,
                error=GENERATE_WITH_EXISTING_MESSAGE,
            )

        try:
            payload, model_name, source = self._generate(
                factory=factory,
                scenario=target,
                analysis_row=analysis_row,
                recommendations_row=ra_row,
                result_version=result_version,
                organization_id=organization_id,
            )
        except Exception:
            logger.exception(
                "Managers playbook generation failed factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            if cached is not None:
                return self._from_row(
                    cached,
                    cache_hit=True,
                    is_stale=True,
                    stale_message=stale_message or STALE_MESSAGE,
                    calculated_at=calculated_at,
                    analysis_id=str(analysis_row.id),
                    success=True,
                )
            return ManagersPlaybookResult(
                success=False,
                analysis_id=str(analysis_row.id),
                factory_id=factory_id,
                model_id=factory_id,
                scenario_id=scenario_id,
                simulation_version=result_version,
                generated_at=None,
                calculated_at=calculated_at,
                cache_hit=False,
                is_stale=is_stale,
                stale_message=stale_message,
                needs_simulation=False,
                needs_analysis=False,
                needs_recommendations=False,
                can_generate_with_existing=False,
                playbook_status="unavailable",
                overview="",
                action_count=0,
                model=getattr(settings, "LLM_MODEL", "") or "",
                error=GENERATION_FAILED_MESSAGE,
            )

        try:
            row = self._persist(
                factory=factory,
                scenario=target,
                analysis_row=analysis_row,
                recommendations_row=ra_row,
                payload=payload,
                result_version=result_version,
                fingerprint=fingerprint,
                prompt_version=prompt_version,
                model_name=model_name,
                source=source,
            )
            return self._from_row(
                row,
                cache_hit=False,
                is_stale=is_stale,
                stale_message=stale_message,
                calculated_at=calculated_at,
                analysis_id=str(analysis_row.id),
            )
        except Exception:
            logger.exception(
                "Managers playbook persist failed factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            return ManagersPlaybookResult(
                success=True,
                analysis_id=str(analysis_row.id),
                factory_id=factory_id,
                model_id=factory_id,
                scenario_id=scenario_id,
                simulation_version=result_version,
                generated_at=timezone.now().isoformat(),
                calculated_at=calculated_at,
                cache_hit=False,
                is_stale=is_stale,
                stale_message=stale_message,
                needs_simulation=False,
                needs_analysis=False,
                needs_recommendations=False,
                can_generate_with_existing=False,
                playbook_status=str(payload.get("playbook_status") or "limited_actions"),
                overview=str(payload.get("overview") or ""),
                action_count=int(payload.get("action_count") or 0),
                manager_actions=list(payload.get("manager_actions") or []),
                final_summary=str(payload.get("final_summary") or ""),
                source=source,
                model=model_name,
                prompt_version=prompt_version,
            )

    def _generate(
        self,
        *,
        factory: RMCMModel,
        scenario: Scenario,
        analysis_row: FactoryAnalysis,
        recommendations_row: RecommendedActions,
        result_version: str,
        organization_id: str | None,
    ) -> tuple[dict[str, Any], str, str]:
        factory_output = self.context_builder.load_factory_output(factory, scenario)
        if not isinstance(factory_output, dict):
            raise ValueError("Simulation result payload missing")

        calculated_at = ""
        if factory_output.get("calculatedAt"):
            calculated_at = str(factory_output.get("calculatedAt"))

        normalized = normalize_simulation_for_analysis(
            factory_id=str(factory.id),
            factory_name=str(factory.name or factory.id),
            model_id=str(factory.id),
            model_name=str(factory.name or factory.id),
            scenario_id=str(scenario.id),
            scenario_name=str(
                scenario.name or ("Basecase" if scenario.is_basecase else scenario.id)
            ),
            simulation_version=result_version,
            simulation_completed_at=calculated_at or None,
            results=factory_output,
            time_unit=_time_unit_from_factory(factory),
        )

        analysis_payload = (
            analysis_row.analysis if isinstance(analysis_row.analysis, dict) else {}
        )
        problems = [
            p for p in (analysis_payload.get("problems") or []) if isinstance(p, dict)
        ]
        impacts = [
            i
            for i in (analysis_payload.get("business_impacts") or [])
            if isinstance(i, dict)
        ]
        overall = analysis_payload.get("overall_business_impact")
        if overall is not None and not isinstance(overall, dict):
            overall = None

        ra_payload = (
            recommendations_row.recommendations
            if isinstance(recommendations_row.recommendations, dict)
            else {}
        )
        recommended_actions = [
            a
            for a in (ra_payload.get("recommended_actions") or [])
            if isinstance(a, dict)
        ]

        input_payload = build_managers_playbook_input(
            analysis_id=str(analysis_row.id),
            organization_id=organization_id,
            factory_id=str(factory.id),
            factory_name=str(factory.name or factory.id),
            model_id=str(factory.id),
            model_name=str(factory.name or factory.id),
            scenario_id=str(scenario.id),
            scenario_name=str(
                scenario.name or ("Basecase" if scenario.is_basecase else scenario.id)
            ),
            simulation_version=result_version,
            simulation_completed_at=calculated_at or None,
            analysis_generated_at=(
                analysis_row.generated_at.isoformat() if analysis_row.generated_at else None
            ),
            problems=problems,
            business_impacts=impacts,
            overall_business_impact=overall,
            recommended_actions=recommended_actions,
            normalized=normalized,
        )

        user_prompt = build_managers_playbook_user_prompt(
            factory_name=str(factory.name or factory.id),
            model_name=str(factory.name or factory.id),
            scenario_name=str(
                scenario.name or ("Basecase" if scenario.is_basecase else scenario.id)
            ),
            simulation_version=result_version,
            analysis_id=str(analysis_row.id),
            managers_playbook_input=input_payload,
        )

        messages = [
            LLMMessage(role="system", content=MANAGERS_PLAYBOOK_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        model_name = getattr(settings, "LLM_MODEL", "") or ""
        source = "ai"
        try:
            response = self.llm_client.chat(messages)
            model_name = (response.model or model_name or "").strip()
            raw_obj = extract_json_object(response.content or "")
            validated = validate_managers_playbook_payload(
                raw_obj,
                input_payload=input_payload,
                analysis_id=str(analysis_row.id),
            )
            if not validated.get("manager_actions"):
                raise ValueError("No valid manager actions from AI")
            if len(validated.get("manager_actions") or []) < 3:
                # Prefer richer deterministic fallback when AI returns too little
                raise ValueError("Insufficient manager actions from AI")
        except (LLMProviderError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception(
                "Managers playbook AI path failed; using deterministic fallback factory_id=%s",
                factory.id,
            )
            validated = build_deterministic_managers_playbook(
                analysis_id=str(analysis_row.id),
                input_payload=input_payload,
            )
            # Re-validate fallback through the same sanitizer
            validated = validate_managers_playbook_payload(
                validated,
                input_payload=input_payload,
                analysis_id=str(analysis_row.id),
            )
            validated["playbook_status"] = (
                "complete"
                if len(validated.get("manager_actions") or []) >= 5
                else "limited_actions"
            )
            source = "deterministic_fallback"
            model_name = model_name or "deterministic_fallback"

        validated["source"] = source
        logger.info(
            "managers_playbook generated analysis_id=%s factory_id=%s scenario_id=%s "
            "simulation_version=%s prompt_version=%s model=%s source=%s action_count=%s",
            analysis_row.id,
            factory.id,
            scenario.id,
            result_version,
            MANAGERS_PLAYBOOK_PROMPT_VERSION,
            model_name,
            source,
            validated.get("action_count"),
        )
        return validated, model_name, source

    @staticmethod
    def _persist(
        *,
        factory: RMCMModel,
        scenario: Scenario,
        analysis_row: FactoryAnalysis,
        recommendations_row: RecommendedActions,
        payload: dict[str, Any],
        result_version: str,
        fingerprint: str,
        prompt_version: str,
        model_name: str,
        source: str,
    ) -> ManagersPlaybook:
        now = timezone.now()
        store = {
            **payload,
            "source": source if source == "deterministic_fallback" else payload.get("source", "ai"),
        }
        with transaction.atomic():
            row, _created = ManagersPlaybook.objects.update_or_create(
                factory_id=factory.pk,
                scenario_id=scenario.pk,
                defaults={
                    "analysis": analysis_row,
                    "recommended_actions": recommendations_row,
                    "playbook": store,
                    "result_version": result_version,
                    "playbook_fingerprint": fingerprint,
                    "prompt_version": prompt_version,
                    "model_name": model_name,
                    "source": store.get("source") or source,
                    "generated_at": now,
                },
            )
        return row

    @staticmethod
    def _from_row(
        row: ManagersPlaybook,
        *,
        cache_hit: bool,
        is_stale: bool,
        stale_message: str,
        calculated_at: str | None,
        analysis_id: str,
        success: bool = True,
    ) -> ManagersPlaybookResult:
        payload = row.playbook if isinstance(row.playbook, dict) else {}
        actions = (
            payload.get("manager_actions")
            if isinstance(payload.get("manager_actions"), list)
            else []
        )
        clean = [dict(a) for a in actions if isinstance(a, dict)]

        return ManagersPlaybookResult(
            success=success,
            analysis_id=analysis_id
            or (str(row.analysis_id) if row.analysis_id else None),
            factory_id=str(row.factory_id),
            model_id=str(row.factory_id),
            scenario_id=str(row.scenario_id),
            simulation_version=row.result_version or "",
            generated_at=row.generated_at.isoformat() if row.generated_at else None,
            calculated_at=calculated_at,
            cache_hit=cache_hit,
            is_stale=is_stale,
            stale_message=stale_message,
            needs_simulation=False,
            needs_analysis=False,
            needs_recommendations=False,
            can_generate_with_existing=False,
            playbook_status=str(payload.get("playbook_status") or "limited_actions"),
            overview=str(payload.get("overview") or ""),
            action_count=int(payload.get("action_count") or len(clean)),
            manager_actions=clean,
            final_summary=str(payload.get("final_summary") or ""),
            source=str(row.source or payload.get("source") or "ai"),
            model=row.model_name or "",
            prompt_version=row.prompt_version or MANAGERS_PLAYBOOK_PROMPT_VERSION,
            error="" if success else GENERATION_FAILED_MESSAGE,
        )

    @staticmethod
    def to_payload(result: ManagersPlaybookResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": result.success,
            "analysis_id": result.analysis_id,
            "factory_id": result.factory_id,
            "model_id": result.model_id,
            "scenario_id": result.scenario_id,
            "simulation_version": result.simulation_version,
            "generated_at": result.generated_at,
            "calculated_at": result.calculated_at,
            "cache_hit": result.cache_hit,
            "is_stale": result.is_stale,
            "stale_message": result.stale_message,
            "needs_simulation": result.needs_simulation,
            "needs_analysis": result.needs_analysis,
            "needs_recommendations": result.needs_recommendations,
            "can_generate_with_existing": result.can_generate_with_existing,
            "playbook_status": result.playbook_status,
            "overview": result.overview,
            "action_count": result.action_count,
            "manager_actions": result.manager_actions,
            "final_summary": result.final_summary,
            "source": result.source,
            "model": result.model,
            "prompt_version": result.prompt_version,
        }
        if not result.success and result.error:
            payload["error"] = result.error
        return payload
