"""
FactoryBriefingService — generate, cache and serve per-scenario factory briefings.

Independent of chat sessions. Sends one scenario's complete factory_input /
factory_output (what-if input is basecase with that scenario's changes applied).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ai.llm.base import LLMClient, LLMMessage
from apps.ai.llm.http import LLMProviderError
from apps.ai.models import FactoryBriefing
from apps.ai.prompts import (
    FACTORY_BRIEFING_SHORTEN_PROMPT,
    FACTORY_BRIEFING_SYSTEM_PROMPT,
)
from apps.ai.services.briefing_validation import (
    FALLBACK_BRIEFING,
    MAX_WORDS,
    count_words,
    needs_shorten,
    sanitize_briefing,
)
from apps.ai.services.context_builder import AIContextBuilder
from apps.rmct.models import RMCMModel, Scenario, ScenarioResult

logger = logging.getLogger(__name__)

STALE_MESSAGE = (
    "You've changed the factory data since this analysis was generated. "
    "This briefing is based on the last simulation run. "
    "Run the model again to get an updated analysis."
)

NEEDS_SIMULATION_MESSAGE = (
    "To get this AI analysis, please calculate this scenario in Run & Results."
)

GENERATE_WITH_EXISTING_MESSAGE = (
    "This scenario needs to be recalculated in Run & Results to get an updated "
    "AI analysis. You can also generate AI analysis with the existing results."
)

GENERATION_FAILED_MESSAGE = (
    "Something went wrong while generating the AI analysis. Please try again."
)


@dataclass
class FactoryBriefingResult:
    success: bool
    briefing: str
    paragraphs: list[str]
    generated_at: str | None
    calculated_at: str | None
    model: str
    cached: bool
    is_stale: bool
    stale_message: str
    needs_simulation: bool
    can_generate_with_existing: bool
    scenario_id: str | None
    error: str


def ensure_basecase_scenario(factory: RMCMModel) -> Scenario:
    """Return the factory basecase scenario, creating it if missing."""
    base = Scenario.objects.filter(model=factory, is_basecase=True).first()
    if base is not None:
        return base
    return Scenario.objects.create(
        id=uuid.uuid4(),
        model=factory,
        name="Basecase",
        description="",
        is_basecase=True,
        status="needs_recalc",
    )


def resolve_scenario_result_version(scenario: Scenario | None) -> str | None:
    """
    Fingerprint of the stored simulation result for a scenario.

    Changes only when results are recalculated — not when input data is edited
    without a new run.
    """
    if scenario is None:
        return None
    try:
        result = scenario.result
    except ScenarioResult.DoesNotExist:
        return None

    calculated_at = ""
    results = result.results
    if isinstance(results, dict):
        calculated_at = str(results.get("calculatedAt") or "")

    raw = "|".join(
        [
            str(scenario.id),
            result.created_at.isoformat() if result.created_at else "",
            calculated_at,
            str(len(str(results)) if results is not None else 0),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def resolve_basecase_result_version(factory: RMCMModel) -> str | None:
    """Fingerprint of the stored basecase simulation result."""
    basecase = Scenario.objects.filter(model=factory, is_basecase=True).first()
    return resolve_scenario_result_version(basecase)


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


class FactoryBriefingService:
    """Application service for the Trooba AI Factory Briefing card."""

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
    ) -> FactoryBriefingResult:
        target = scenario
        if target is None or getattr(target, "pk", None) is None:
            target = ensure_basecase_scenario(factory)

        scenario_id = str(target.id)
        calculated_at = _extract_calculated_at(target)

        result_version = resolve_scenario_result_version(target)
        if not result_version:
            return FactoryBriefingResult(
                success=False,
                briefing="",
                paragraphs=[],
                generated_at=None,
                calculated_at=None,
                model="",
                cached=False,
                is_stale=False,
                stale_message="",
                needs_simulation=True,
                can_generate_with_existing=False,
                scenario_id=scenario_id,
                error=NEEDS_SIMULATION_MESSAGE,
            )

        is_stale = _scenario_is_stale(factory, target)
        stale_message = STALE_MESSAGE if is_stale else ""

        cached = (
            FactoryBriefing.objects.filter(factory=factory, scenario=target)
            .order_by("-generated_at")
            .first()
        )
        if cached is not None and cached.result_version == result_version:
            return self._from_row(
                cached,
                cached=True,
                is_stale=is_stale,
                stale_message=stale_message,
                calculated_at=calculated_at,
                scenario_id=scenario_id,
            )

        # Old/stale results exist but no AI for this result version yet —
        # ask before generating, unless the user opted in.
        if is_stale and not force_generate:
            return FactoryBriefingResult(
                success=False,
                briefing="",
                paragraphs=[],
                generated_at=None,
                calculated_at=calculated_at,
                model="",
                cached=False,
                is_stale=True,
                stale_message="",
                needs_simulation=False,
                can_generate_with_existing=True,
                scenario_id=scenario_id,
                error=GENERATE_WITH_EXISTING_MESSAGE,
            )

        try:
            briefing_text, paragraphs, model_name = self._generate(
                factory, target
            )
        except Exception:
            logger.exception(
                "Factory briefing generation failed for factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            if cached is not None:
                return self._from_row(
                    cached,
                    cached=True,
                    is_stale=True,
                    stale_message=stale_message or STALE_MESSAGE,
                    success=True,
                    calculated_at=calculated_at,
                    scenario_id=scenario_id,
                )
            return FactoryBriefingResult(
                success=False,
                briefing="",
                paragraphs=[],
                generated_at=None,
                calculated_at=calculated_at,
                model=getattr(settings, "LLM_MODEL", "") or "",
                cached=False,
                is_stale=is_stale,
                stale_message=stale_message,
                needs_simulation=False,
                can_generate_with_existing=False,
                scenario_id=scenario_id,
                error=GENERATION_FAILED_MESSAGE,
            )

        try:
            row = self._persist(
                factory=factory,
                scenario=target,
                briefing=briefing_text,
                paragraphs=paragraphs,
                result_version=result_version,
                model_name=model_name,
            )
            return self._from_row(
                row,
                cached=False,
                is_stale=is_stale,
                stale_message=stale_message,
                calculated_at=calculated_at,
                scenario_id=scenario_id,
            )
        except Exception:
            # Generation succeeded — still return the briefing even if cache write fails.
            logger.exception(
                "Factory briefing persist failed for factory_id=%s scenario_id=%s",
                factory.id,
                scenario_id,
            )
            return FactoryBriefingResult(
                success=True,
                briefing=briefing_text,
                paragraphs=paragraphs,
                generated_at=timezone.now().isoformat(),
                calculated_at=calculated_at,
                model=model_name,
                cached=False,
                is_stale=is_stale,
                stale_message=stale_message,
                needs_simulation=False,
                can_generate_with_existing=False,
                scenario_id=scenario_id,
                error="",
            )

    def _generate(
        self,
        factory: RMCMModel,
        scenario: Scenario,
    ) -> tuple[str, list[str], str]:
        factory_input = self.context_builder.load_scenario_factory_input(
            factory, scenario
        )
        factory_output = self.context_builder.load_factory_output(
            factory, scenario
        )

        messages = [
            LLMMessage(role="system", content=FACTORY_BRIEFING_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content={
                    "factory_input": factory_input,
                    "factory_output": factory_output,
                },
            ),
        ]

        try:
            response = self.llm_client.chat(messages)
        except LLMProviderError:
            logger.exception(
                "LLM provider error while generating factory briefing factory_id=%s",
                factory.id,
            )
            raise

        raw = (response.content or "").strip()
        model_name = (response.model or getattr(settings, "LLM_MODEL", "") or "").strip()

        if not raw:
            raise ValueError("Empty briefing response from LLM")

        if needs_shorten(raw):
            raw = self._shorten(raw, model_name=model_name) or raw

        briefing, paragraphs = sanitize_briefing(raw)
        if not paragraphs:
            briefing, paragraphs = sanitize_briefing(FALLBACK_BRIEFING)

        if count_words(briefing) > MAX_WORDS:
            briefing, paragraphs = sanitize_briefing(briefing)

        return briefing, paragraphs, model_name

    def _shorten(self, text: str, *, model_name: str) -> str | None:
        messages = [
            LLMMessage(role="system", content=FACTORY_BRIEFING_SHORTEN_PROMPT),
            LLMMessage(role="user", content=text),
        ]
        try:
            response = self.llm_client.chat(messages)
            return (response.content or "").strip() or None
        except LLMProviderError:
            logger.warning(
                "Factory briefing shorten request failed model=%s",
                model_name or "unknown",
            )
            return None

    @staticmethod
    def _persist(
        *,
        factory: RMCMModel,
        scenario: Scenario,
        briefing: str,
        paragraphs: list[str],
        result_version: str,
        model_name: str,
    ) -> FactoryBriefing:
        scenario_pk = getattr(scenario, "pk", None)
        factory_pk = getattr(factory, "pk", None)
        if scenario_pk is None:
            raise ValueError("Cannot persist factory briefing without a scenario id")
        if factory_pk is None:
            raise ValueError("Cannot persist factory briefing without a factory id")

        now = timezone.now()
        with transaction.atomic():
            row, _created = FactoryBriefing.objects.update_or_create(
                factory_id=factory_pk,
                scenario_id=scenario_pk,
                defaults={
                    "briefing": briefing,
                    "paragraphs": paragraphs,
                    "result_version": result_version,
                    "model_name": model_name,
                    "generated_at": now,
                },
            )
        return row

    @staticmethod
    def _from_row(
        row: FactoryBriefing,
        *,
        cached: bool,
        is_stale: bool,
        stale_message: str,
        calculated_at: str | None,
        scenario_id: str | None,
        success: bool = True,
    ) -> FactoryBriefingResult:
        paragraphs = row.paragraphs if isinstance(row.paragraphs, list) else []
        paragraphs = [str(p).strip() for p in paragraphs if str(p).strip()]
        if not paragraphs and row.briefing:
            briefing, paragraphs = sanitize_briefing(row.briefing)
        else:
            briefing = row.briefing or "\n\n".join(paragraphs)

        return FactoryBriefingResult(
            success=success,
            briefing=briefing,
            paragraphs=paragraphs,
            generated_at=row.generated_at.isoformat() if row.generated_at else None,
            calculated_at=calculated_at,
            model=row.model_name or "",
            cached=cached,
            is_stale=is_stale,
            stale_message=stale_message,
            needs_simulation=False,
            can_generate_with_existing=False,
            scenario_id=scenario_id,
            error="" if success else GENERATION_FAILED_MESSAGE,
        )

    @staticmethod
    def to_payload(result: FactoryBriefingResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": result.success,
            "briefing": result.briefing,
            "paragraphs": result.paragraphs,
            "generated_at": result.generated_at,
            "calculated_at": result.calculated_at,
            "model": result.model,
            "cached": result.cached,
            "is_stale": result.is_stale,
            "stale_message": result.stale_message,
            "needs_simulation": result.needs_simulation,
            "can_generate_with_existing": result.can_generate_with_existing,
            "scenario_id": result.scenario_id,
        }
        if not result.success and result.error:
            payload["error"] = result.error
        return payload
