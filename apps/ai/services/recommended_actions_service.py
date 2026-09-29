"""
RecommendedActionsService — Top 3 actions from validated Problems + Impacts.

Cache / refresh rules mirror FactoryAnalysisService / FactoryBriefingService.
Requires an existing FactoryAnalysis for the same factory + scenario.
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
from apps.ai.models import FactoryAnalysis, RecommendedActions
from apps.ai.prompts.recommended_actions import (
    RECOMMENDED_ACTIONS_PROMPT_VERSION,
    RECOMMENDED_ACTIONS_SYSTEM_PROMPT,
    build_recommended_actions_user_prompt,
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
from apps.ai.services.recommended_actions import (
    analysis_fingerprint,
    build_deterministic_recommended_actions,
    build_recommended_actions_input,
    rank_recommended_actions,
    strip_internal_action_scores,
    validate_recommended_actions_payload,
)
from apps.rmct.models import RMCMModel, Scenario, ScenarioResult

logger = logging.getLogger(__name__)

GENERATION_FAILED_MESSAGE = (
    "Something went wrong while generating recommended actions. Please try again."
)

NEEDS_ANALYSIS_MESSAGE = (
    "Recommended Actions need validated Problems and Business Impacts first. "
    "Open the Business Impact section to generate them, then return here."
)

STALE_MESSAGE = (
    "You've changed the factory data since these recommendations were generated. "
    "These actions are based on the last simulation run. "
    "Run the model again to get updated recommendations."
)


@dataclass
class RecommendedActionsResult:
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
    can_generate_with_existing: bool
    recommendation_status: str
    action_count: int
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)
    source: str = "ai"
    model: str = ""
    prompt_version: str = RECOMMENDED_ACTIONS_PROMPT_VERSION
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


class RecommendedActionsService:
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
    ) -> RecommendedActionsResult:
        target = scenario
        if target is None or getattr(target, "pk", None) is None:
            target = ensure_basecase_scenario(factory)

        factory_id = str(factory.id)
        scenario_id = str(target.id)
        calculated_at = _extract_calculated_at(target)
        prompt_version = (
            getattr(settings, "RECOMMENDED_ACTIONS_PROMPT_VERSION", None)
            or RECOMMENDED_ACTIONS_PROMPT_VERSION
        )

        result_version = resolve_scenario_result_version(target)
        if not result_version:
            return RecommendedActionsResult(
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
                can_generate_with_existing=False,
                recommendation_status="unavailable",
                action_count=0,
                error=NEEDS_SIMULATION_MESSAGE,
            )

        # Load parent FactoryAnalysis (problems + impacts)
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
            return RecommendedActionsResult(
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
                can_generate_with_existing=False,
                recommendation_status="unavailable",
                action_count=0,
                error=NEEDS_ANALYSIS_MESSAGE,
            )

        fingerprint = analysis_fingerprint(
            str(analysis_row.id),
            analysis_row.analysis if isinstance(analysis_row.analysis, dict) else {},
        )

        is_stale = _scenario_is_stale(factory, target)
        stale_message = STALE_MESSAGE if is_stale else ""

        cached = (
            RecommendedActions.objects.filter(factory=factory, scenario=target)
            .order_by("-generated_at")
            .first()
        )
        if (
            cached is not None
            and cached.result_version == result_version
            and cached.analysis_fingerprint == fingerprint
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
            return RecommendedActionsResult(
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
                can_generate_with_existing=True,
                recommendation_status="unavailable",
                action_count=0,
                error=GENERATE_WITH_EXISTING_MESSAGE,
            )

        try:
            payload, model_name, source = self._generate(
                factory=factory,
                scenario=target,
                analysis_row=analysis_row,
                result_version=result_version,
                organization_id=organization_id,
            )
        except Exception:
            logger.exception(
                "Recommended actions generation failed factory_id=%s scenario_id=%s",
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
            return RecommendedActionsResult(
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
                can_generate_with_existing=False,
                recommendation_status="unavailable",
                action_count=0,
                model=getattr(settings, "LLM_MODEL", "") or "",
                error=GENERATION_FAILED_MESSAGE,
            )

        try:
            row = self._persist(
                factory=factory,
                scenario=target,
                analysis_row=analysis_row,
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
                "Recommended actions persist failed factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            public = strip_internal_action_scores(payload)
            return RecommendedActionsResult(
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
                can_generate_with_existing=False,
                recommendation_status=str(
                    public.get("recommendation_status") or "limited_actions"
                ),
                action_count=int(public.get("action_count") or 0),
                recommended_actions=list(public.get("recommended_actions") or []),
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

        input_payload = build_recommended_actions_input(
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
            normalized=normalized,
        )

        user_prompt = build_recommended_actions_user_prompt(
            factory_name=str(factory.name or factory.id),
            model_name=str(factory.name or factory.id),
            scenario_name=str(
                scenario.name or ("Basecase" if scenario.is_basecase else scenario.id)
            ),
            simulation_version=result_version,
            analysis_id=str(analysis_row.id),
            recommended_actions_input=input_payload,
        )

        messages = [
            LLMMessage(role="system", content=RECOMMENDED_ACTIONS_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        model_name = getattr(settings, "LLM_MODEL", "") or ""
        source = "ai"
        try:
            response = self.llm_client.chat(messages)
            model_name = (response.model or model_name or "").strip()
            raw_obj = extract_json_object(response.content or "")
            validated = validate_recommended_actions_payload(
                raw_obj,
                input_payload=input_payload,
                analysis_id=str(analysis_row.id),
            )
            ranked = rank_recommended_actions(
                list(validated.get("recommended_actions") or []),
                limit=3,
            )
            validated["recommended_actions"] = ranked
            validated["action_count"] = len(ranked)
            if len(ranked) >= 3:
                validated["recommendation_status"] = "complete"
            elif ranked:
                validated["recommendation_status"] = "limited_actions"
            else:
                raise ValueError("No valid recommended actions from AI")
        except (LLMProviderError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception(
                "Recommended actions AI path failed; using deterministic fallback factory_id=%s",
                factory.id,
            )
            validated = build_deterministic_recommended_actions(
                analysis_id=str(analysis_row.id),
                input_payload=input_payload,
            )
            ranked = rank_recommended_actions(
                list(validated.get("recommended_actions") or []),
                limit=3,
            )
            validated["recommended_actions"] = ranked
            validated["action_count"] = len(ranked)
            validated["recommendation_status"] = (
                "complete" if len(ranked) >= 3 else "limited_actions"
            )
            source = "deterministic_fallback"
            model_name = model_name or "deterministic_fallback"

        validated = strip_internal_action_scores(validated)
        validated["source"] = source
        return validated, model_name, source

    @staticmethod
    def _persist(
        *,
        factory: RMCMModel,
        scenario: Scenario,
        analysis_row: FactoryAnalysis,
        payload: dict[str, Any],
        result_version: str,
        fingerprint: str,
        prompt_version: str,
        model_name: str,
        source: str,
    ) -> RecommendedActions:
        now = timezone.now()
        public = strip_internal_action_scores(payload)
        store = {
            **public,
            "source": source if source == "deterministic_fallback" else public.get("source", "ai"),
        }
        with transaction.atomic():
            row, _created = RecommendedActions.objects.update_or_create(
                factory_id=factory.pk,
                scenario_id=scenario.pk,
                defaults={
                    "analysis": analysis_row,
                    "recommendations": store,
                    "result_version": result_version,
                    "analysis_fingerprint": fingerprint,
                    "prompt_version": prompt_version,
                    "model_name": model_name,
                    "source": store.get("source") or source,
                    "generated_at": now,
                },
            )
        return row

    @staticmethod
    def _from_row(
        row: RecommendedActions,
        *,
        cache_hit: bool,
        is_stale: bool,
        stale_message: str,
        calculated_at: str | None,
        analysis_id: str,
        success: bool = True,
    ) -> RecommendedActionsResult:
        payload = row.recommendations if isinstance(row.recommendations, dict) else {}
        actions = (
            payload.get("recommended_actions")
            if isinstance(payload.get("recommended_actions"), list)
            else []
        )
        clean = []
        for action in actions:
            if isinstance(action, dict):
                clone = dict(action)
                for key in (
                    "action_score",
                    "coverage_score",
                    "feasibility_score",
                    "evidence_score",
                ):
                    clone.pop(key, None)
                clean.append(clone)

        return RecommendedActionsResult(
            success=success,
            analysis_id=analysis_id or (
                str(row.analysis_id) if row.analysis_id else None
            ),
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
            can_generate_with_existing=False,
            recommendation_status=str(
                payload.get("recommendation_status") or "limited_actions"
            ),
            action_count=int(payload.get("action_count") or len(clean)),
            recommended_actions=clean,
            source=str(row.source or payload.get("source") or "ai"),
            model=row.model_name or "",
            prompt_version=row.prompt_version or RECOMMENDED_ACTIONS_PROMPT_VERSION,
            error="" if success else GENERATION_FAILED_MESSAGE,
        )

    @staticmethod
    def to_payload(result: RecommendedActionsResult) -> dict[str, Any]:
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
            "can_generate_with_existing": result.can_generate_with_existing,
            "recommendation_status": result.recommendation_status,
            "action_count": result.action_count,
            "recommended_actions": result.recommended_actions,
            "source": result.source,
            "model": result.model,
            "prompt_version": result.prompt_version,
        }
        if not result.success and result.error:
            payload["error"] = result.error
        return payload
