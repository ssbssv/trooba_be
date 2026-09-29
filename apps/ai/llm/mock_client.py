"""
Deterministic fallback client used when no provider API key is configured.

Keeps local/dev environments functional without inventing factory metrics.
"""
from __future__ import annotations

from typing import Any

from .base import LLMClient, LLMMessage, LLMResponse
from .formatting import content_as_text


class MockLLMClient(LLMClient):
    def chat(self, messages: list[LLMMessage], **kwargs: Any) -> LLMResponse:
        system_text = ""
        for message in messages:
            if message.role == "system":
                system_text = content_as_text(message.content).lower()
                break

        if "factory briefing" in system_text or "executive factory briefing" in system_text:
            content = (
                "I've reviewed the basecase simulation and the factory is generally able "
                "to support current demand, with a few flow risks worth watching. "
                "Overall {demand achievement} looks workable, but capacity headroom is limited "
                "on the tightest resources.\n\n"
                "The clearest constraint is around high-utilization equipment, where "
                "{queue} and {WIP} can build when arrivals spike. Labor looks more balanced "
                "and is less likely to be the primary limiter right now.\n\n"
                "Products with longer {manufacturing cycle time} are most exposed if waiting "
                "time grows at the constraint. Stabilizing the bottleneck and protecting flow "
                "there is the most practical next improvement before adding more demand.\n\n"
                "Note: No LLM API key is configured, so this is a local fallback briefing."
            )
            return LLMResponse(content=content, model="mock", usage={})

        if "factory operations consultant" in system_text or "problem_score" in system_text:
            # Deterministic JSON so local/dev still exercises the analysis path.
            # The service validates/rewrites metrics from the normalized catalog.
            content = (
                '{"analysis_status":"limited_findings","problem_count":0,'
                '"problems":[],"business_impacts":[],"overall_business_impact":null}'
            )
            return LLMResponse(content=content, model="mock", usage={})

        question = ""
        for message in reversed(messages):
            if message.role != "user":
                continue
            if isinstance(message.content, dict):
                question = str(message.content.get("current_question") or "")
            else:
                question = content_as_text(message.content)
            break

        lower = question.lower()
        hypothetical_markers = (
            "what if",
            "what-if",
            "if i add",
            "if i remove",
            "if i hire",
            "if demand",
            "if i add another shift",
            "another shift",
        )
        if any(marker in lower for marker in hypothetical_markers):
            content = (
                "This requires a new factory simulation.\n"
                "Please create a new Scenario and rerun the model.\n"
                "Once the scenario is calculated I can analyze the results."
            )
        else:
            content = (
                "I can analyze the supplied factory input and calculated results "
                "for this scenario. Ask about bottlenecks, WIP, lead time, "
                "utilization, throughput, or recommendations present in the results.\n\n"
                "Note: No LLM API key is configured, so this is a local fallback reply. "
                "Set LLM_PROVIDER and the matching API key in Django settings to enable "
                "full Trooba AI analysis."
            )
        return LLMResponse(content=content, model="mock", usage={})
