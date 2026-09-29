"""
FactoryAnalysisService — one AI call for Problems Identified + Business Impact.

Cache / refresh rules mirror FactoryBriefingService:
- Cache hit when result_version + prompt_version match
- needs_simulation when no ScenarioResult
- stale + can_generate_with_existing when needs_recalc and not force_generate
- force_generate regenerates with existing results
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai.llm.base import LLMClient, LLMMessage
from apps.ai.llm.http import LLMProviderError
from apps.ai.models import FactoryAnalysis
from apps.ai.prompts.factory_analysis import (
    FACTORY_ANALYSIS_PROMPT_VERSION,
    FACTORY_ANALYSIS_SYSTEM_PROMPT,
    build_factory_analysis_user_prompt,
)
from apps.ai.services.context_builder import AIContextBuilder
from apps.ai.services.factory_analysis import (
    build_deterministic_analysis,
    extract_json_object,
    finalize_analysis_display,
    normalize_simulation_for_analysis,
    strip_internal_scores,
    validate_analysis_payload,
)
from apps.ai.services.factory_briefing_service import (
    GENERATE_WITH_EXISTING_MESSAGE,
    NEEDS_SIMULATION_MESSAGE,
    ensure_basecase_scenario,
    resolve_scenario_result_version,
)
from apps.rmct.models import RMCMModel, Scenario, ScenarioResult

logger = logging.getLogger(__name__)

GENERATION_FAILED_MESSAGE = (
    "Something went wrong while generating the AI analysis. Please try again."
)

ANALYSIS_STALE_MESSAGE = (
    "You've changed the factory data since this analysis was generated. "
    "This analysis is based on the last simulation run. "
    "Run the model again to get an updated analysis."
)


@dataclass
class FactoryAnalysisResult:
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
    can_generate_with_existing: bool
    analysis_status: str
    problem_count: int
    problems: list[dict[str, Any]] = field(default_factory=list)
    business_impacts: list[dict[str, Any]] = field(default_factory=list)
    overall_business_impact: dict[str, Any] | None = None
    source: str = "ai"
    model: str = ""
    prompt_version: str = FACTORY_ANALYSIS_PROMPT_VERSION
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
    # General data may store period labels; default to days for MCT display.
    return "days"


class FactoryAnalysisService:
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
    ) -> FactoryAnalysisResult:
        target = scenario
        if target is None or getattr(target, "pk", None) is None:
            target = ensure_basecase_scenario(factory)

        factory_id = str(factory.id)
        scenario_id = str(target.id)
        calculated_at = _extract_calculated_at(target)
        prompt_version = (
            getattr(settings, "FACTORY_ANALYSIS_PROMPT_VERSION", None)
            or FACTORY_ANALYSIS_PROMPT_VERSION
        )

        result_version = resolve_scenario_result_version(target)
        if not result_version:
            return FactoryAnalysisResult(
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
                can_generate_with_existing=False,
                analysis_status="limited_findings",
                problem_count=0,
                error=NEEDS_SIMULATION_MESSAGE,
            )

        is_stale = _scenario_is_stale(factory, target)
        stale_message = ANALYSIS_STALE_MESSAGE if is_stale else ""

        cached = (
            FactoryAnalysis.objects.filter(factory=factory, scenario=target)
            .order_by("-generated_at")
            .first()
        )
        if (
            cached is not None
            and cached.result_version == result_version
            and cached.prompt_version == prompt_version
            and not force_generate
        ):
            return self._from_row(
                cached,
                cache_hit=True,
                is_stale=is_stale,
                stale_message=stale_message,
                calculated_at=calculated_at,
            )

        if is_stale and not force_generate:
            return FactoryAnalysisResult(
                success=False,
                analysis_id=None,
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
                can_generate_with_existing=True,
                analysis_status="limited_findings",
                problem_count=0,
                error=GENERATE_WITH_EXISTING_MESSAGE,
            )

        try:
            analysis, model_name, source = self._generate(factory, target, result_version)
        except Exception:
            logger.exception(
                "Factory analysis generation failed factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            if cached is not None:
                return self._from_row(
                    cached,
                    cache_hit=True,
                    is_stale=True,
                    stale_message=stale_message or ANALYSIS_STALE_MESSAGE,
                    calculated_at=calculated_at,
                    success=True,
                )
            return FactoryAnalysisResult(
                success=False,
                analysis_id=None,
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
                can_generate_with_existing=False,
                analysis_status="limited_findings",
                problem_count=0,
                model=getattr(settings, "LLM_MODEL", "") or "",
                error=GENERATION_FAILED_MESSAGE,
            )

        try:
            row = self._persist(
                factory=factory,
                scenario=target,
                analysis=analysis,
                result_version=result_version,
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
            )
        except Exception:
            logger.exception(
                "Factory analysis persist failed factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            public = strip_internal_scores(analysis)
            return FactoryAnalysisResult(
                success=True,
                analysis_id=str(uuid.uuid4()),
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
                can_generate_with_existing=False,
                analysis_status=str(public.get("analysis_status") or "limited_findings"),
                problem_count=int(public.get("problem_count") or 0),
                problems=list(public.get("problems") or []),
                business_impacts=list(public.get("business_impacts") or []),
                overall_business_impact=public.get("overall_business_impact"),
                source=source,
                model=model_name,
                prompt_version=prompt_version,
            )

    def _generate(
        self,
        factory: RMCMModel,
        scenario: Scenario,
        result_version: str,
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
            scenario_name=str(scenario.name or ("Basecase" if scenario.is_basecase else scenario.id)),
            simulation_version=result_version,
            simulation_completed_at=calculated_at or None,
            results=factory_output,
            time_unit=_time_unit_from_factory(factory),
        )

        user_prompt = build_factory_analysis_user_prompt(
            factory_name=str(factory.name or factory.id),
            model_name=str(factory.name or factory.id),
            scenario_name=str(scenario.name or "Basecase"),
            simulation_version=result_version,
            simulation_completed_at=calculated_at or "",
            normalized_simulation_json=json.dumps(normalized, ensure_ascii=True, default=str),
        )

        messages = [
            LLMMessage(role="system", content=FACTORY_ANALYSIS_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        model_name = getattr(settings, "LLM_MODEL", "") or ""
        source = "ai"
        try:
            response = self.llm_client.chat(messages)
            model_name = (response.model or model_name or "").strip()
            raw_obj = extract_json_object(response.content or "")
            validated = validate_analysis_payload(raw_obj, normalized=normalized)
            # If AI returned problems but impacts missing, complete via fallback impacts only
            if validated["problem_count"] > 0 and len(validated.get("business_impacts") or []) < 5:
                fallback = build_deterministic_analysis(normalized)
                # Keep AI problems; fill missing impacts/overall from deterministic.
                if len(validated.get("business_impacts") or []) < 5:
                    validated["business_impacts"] = fallback.get("business_impacts") or []
                    # Re-bind related ids to current problems
                    ids = [p["id"] for p in validated["problems"]]
                    for impact in validated["business_impacts"]:
                        related = [x for x in impact.get("related_problem_ids") or [] if x in ids]
                        impact["related_problem_ids"] = related or ids[:1]
                if not validated.get("overall_business_impact"):
                    overall = fallback.get("overall_business_impact")
                    if isinstance(overall, dict):
                        overall["related_problem_ids"] = ids
                        validated["overall_business_impact"] = overall
            if validated["problem_count"] == 0:
                validated = build_deterministic_analysis(normalized)
                source = "deterministic_fallback"
        except (LLMProviderError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception(
                "Factory analysis AI path failed; using deterministic fallback factory_id=%s",
                factory.id,
            )
            validated = build_deterministic_analysis(normalized)
            source = "deterministic_fallback"
            model_name = model_name or "deterministic_fallback"

        if source == "deterministic_fallback":
            # already includes source key inside payload optionally
            validated = dict(validated)
            validated["source"] = "deterministic_fallback"

        validated = finalize_analysis_display(validated, normalized=normalized)
        return validated, model_name, source

    @staticmethod
    def _persist(
        *,
        factory: RMCMModel,
        scenario: Scenario,
        analysis: dict[str, Any],
        result_version: str,
        prompt_version: str,
        model_name: str,
        source: str,
    ) -> FactoryAnalysis:
        now = timezone.now()
        public = strip_internal_scores(analysis)
        payload = {
            **public,
            "source": source if source == "deterministic_fallback" else public.get("source", "ai"),
        }
        with transaction.atomic():
            row, _created = FactoryAnalysis.objects.update_or_create(
                factory_id=factory.pk,
                scenario_id=scenario.pk,
                defaults={
                    "analysis": payload,
                    "result_version": result_version,
                    "prompt_version": prompt_version,
                    "model_name": model_name,
                    "source": payload.get("source") or source,
                    "generated_at": now,
                },
            )
        return row

    @staticmethod
    def _from_row(
        row: FactoryAnalysis,
        *,
        cache_hit: bool,
        is_stale: bool,
        stale_message: str,
        calculated_at: str | None,
        success: bool = True,
    ) -> FactoryAnalysisResult:
        analysis = row.analysis if isinstance(row.analysis, dict) else {}
        problems = analysis.get("problems") if isinstance(analysis.get("problems"), list) else []
        # Ensure scores never leak
        clean_problems = []
        for p in problems:
            if isinstance(p, dict):
                clone = dict(p)
                clone.pop("problem_score", None)
                clean_problems.append(clone)

        impacts = (
            analysis.get("business_impacts")
            if isinstance(analysis.get("business_impacts"), list)
            else []
        )
        overall = analysis.get("overall_business_impact")
        if overall is not None and not isinstance(overall, dict):
            overall = None

        return FactoryAnalysisResult(
            success=success,
            analysis_id=str(row.id),
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
            can_generate_with_existing=False,
            analysis_status=str(analysis.get("analysis_status") or "limited_findings"),
            problem_count=int(analysis.get("problem_count") or len(clean_problems)),
            problems=clean_problems,
            business_impacts=[i for i in impacts if isinstance(i, dict)],
            overall_business_impact=overall,
            source=str(row.source or analysis.get("source") or "ai"),
            model=row.model_name or "",
            prompt_version=row.prompt_version or FACTORY_ANALYSIS_PROMPT_VERSION,
            error="" if success else GENERATION_FAILED_MESSAGE,
        )

    @staticmethod
    def to_payload(result: FactoryAnalysisResult) -> dict[str, Any]:
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
            "can_generate_with_existing": result.can_generate_with_existing,
            "analysis_status": result.analysis_status,
            "problem_count": result.problem_count,
            "problems": result.problems,
            "business_impacts": result.business_impacts,
            "overall_business_impact": result.overall_business_impact,
            "source": result.source,
            "model": result.model,
            "prompt_version": result.prompt_version,
        }
        if not result.success and result.error:
            payload["error"] = result.error
        return payload
