"""
Tests for Recommended Actions input building, validation, ranking, and fallback.
"""
from django.test import SimpleTestCase

from apps.ai.prompts.recommended_actions import (
    RECOMMENDED_ACTIONS_PROMPT_VERSION,
    RECOMMENDED_ACTIONS_SYSTEM_PROMPT,
    build_recommended_actions_user_prompt,
)
from apps.ai.services.factory_analysis.normalizer import normalize_simulation_for_analysis
from apps.ai.services.recommended_actions.fallback import (
    build_deterministic_recommended_actions,
)
from apps.ai.services.recommended_actions.input_builder import (
    analysis_fingerprint,
    build_recommended_actions_input,
)
from apps.ai.services.recommended_actions.ranker import rank_recommended_actions
from apps.ai.services.recommended_actions.validator import (
    strip_internal_action_scores,
    validate_recommended_actions_payload,
)

SAMPLE_RESULTS = {
    "products": [
        {
            "id": "HUB1",
            "name": "HUB1",
            "demand": 100,
            "goodMade": 90,
            "goodShipped": 90,
            "started": 100,
            "scrap": 10,
            "wip": 40,
            "mct": 29.25,
            "mctQueue": 12.0,
            "mctWaitLabor": 3.0,
            "mctLotWait": 2.0,
            "mctSetup": 4.0,
            "mctRun": 8.25,
        }
    ],
    "equipment": [
        {
            "id": "eq1",
            "name": "VT_LATHE",
            "totalUtil": 89.55,
            "wip_queue": 12.0,
            "wip_total": 20.0,
            "wait_min": 5.0,
            "num_av": 1,
        }
    ],
    "labor": [
        {
            "id": "lab1",
            "name": "MACHINIST",
            "totalUtil": 55.0,
            "wip_queue": 2.0,
            "wip_total": 3.0,
            "count": 2,
        }
    ],
    "operations": [
        {
            "product": "HUB1",
            "product_id": "HUB1",
            "operation": "TURNING",
            "equipment": "VT_LATHE",
            "equip_id": "eq1",
            "labor": "MACHINIST",
            "labor_id": "lab1",
            "qpoper": 8.0,
            "w_equip": 4.0,
            "w_labor": 1.0,
            "w_lot": 0.5,
        }
    ],
    "warnings": ['Equipment "VT_LATHE" util (89.55%) > limit (85%)'],
    "overLimitResources": ["Equipment: VT_LATHE (89.55%)"],
    "calculatedAt": "2026-07-20T10:44:00+00:00",
}


def _normalized():
    return normalize_simulation_for_analysis(
        factory_id="f1",
        factory_name="Factory A",
        model_id="f1",
        model_name="Factory A",
        scenario_id="s1",
        scenario_name="Basecase",
        simulation_version="abc123",
        simulation_completed_at="2026-07-20T10:44:00+00:00",
        results=SAMPLE_RESULTS,
    )


PROBLEMS = [
    {
        "id": "problem_1",
        "rank": 1,
        "problem": "VT_LATHE Capacity Constraint",
        "summary": "VT_LATHE is creating excessive equipment waiting.",
        "root_cause": "High equipment loading.",
        "severity": "high",
        "category": "equipment_constraint",
        "area": "VT_LATHE",
        "detailed_reasoning": "Validated reasoning.",
        "why_top_four": "Largest delay.",
        "affected_products": ["HUB1"],
        "affected_operations": ["TURNING"],
        "affected_resources": ["VT_LATHE"],
        "supporting_metrics": [],
        "evidence_keys": [
            "equipment.utilization.VT_LATHE",
            "product_mct.HUB1.wait_equipment",
        ],
    },
    {
        "id": "problem_2",
        "rank": 2,
        "problem": "HUB1 Lot Waiting",
        "summary": "HUB1 waits for lot synchronization.",
        "root_cause": "Lot size delay.",
        "severity": "medium",
        "category": "lot_waiting",
        "area": "HUB1",
        "detailed_reasoning": "Validated.",
        "why_top_four": "Material wait_lot.",
        "affected_products": ["HUB1"],
        "affected_operations": ["TURNING"],
        "affected_resources": ["VT_LATHE"],
        "supporting_metrics": [],
        "evidence_keys": ["product_mct.HUB1.wait_lot"],
    },
]

IMPACTS = [
    {
        "id": "impact_1",
        "rank": 1,
        "title": "Manufacturing Cycle Time is likely to increase",
        "description": "Waiting grows if unresolved.",
        "severity": "high",
        "category": "cycle_time",
        "time_horizon": "near_term",
        "related_problem_ids": ["problem_1"],
        "causal_chain": ["Pressure remains", "Waiting grows", "MCT increases"],
        "confidence": 0.9,
        "evidence_keys": ["equipment.utilization.VT_LATHE"],
    },
    {
        "id": "impact_2",
        "rank": 2,
        "title": "WIP is likely to rise",
        "description": "Queues may grow.",
        "severity": "medium",
        "category": "wip",
        "time_horizon": "near_term",
        "related_problem_ids": ["problem_2"],
        "causal_chain": ["Lot waiting", "WIP rises"],
        "confidence": 0.8,
        "evidence_keys": ["product_mct.HUB1.wait_lot"],
    },
]

OVERALL = {
    "title": "Factory flow efficiency is likely to decline",
    "description": "Non-value-added waiting may increase.",
    "severity": "high",
    "time_horizon": "medium_term",
    "related_problem_ids": ["problem_1", "problem_2"],
    "confidence": 0.9,
}


class InputBuilderTests(SimpleTestCase):
    def test_includes_required_sections(self):
        normalized = _normalized()
        payload = build_recommended_actions_input(
            analysis_id="a1",
            organization_id="org1",
            factory_id="f1",
            factory_name="Factory A",
            model_id="f1",
            model_name="Factory A",
            scenario_id="s1",
            scenario_name="Basecase",
            simulation_version="abc123",
            simulation_completed_at="2026-07-20T10:44:00+00:00",
            analysis_generated_at="2026-07-20T10:45:00+00:00",
            problems=PROBLEMS,
            business_impacts=IMPACTS,
            overall_business_impact=OVERALL,
            normalized=normalized,
        )
        for key in (
            "analysis_context",
            "factory_objectives",
            "validated_problems",
            "validated_business_impacts",
            "validated_overall_business_impact",
            "simulation_evidence",
            "available_action_context",
            "metric_catalog",
            "recommendation_constraints",
        ):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["validated_problems"]), 2)
        self.assertEqual(len(payload["validated_business_impacts"]), 2)
        self.assertTrue(payload["validated_overall_business_impact"])
        self.assertIn("equipment", payload["simulation_evidence"])
        self.assertIn("allowed_action_types", payload["available_action_context"])
        self.assertIn("equipment.utilization.VT_LATHE", payload["metric_catalog"])

    def test_fingerprint_changes_with_problems(self):
        a = analysis_fingerprint("a1", {"problems": PROBLEMS, "business_impacts": IMPACTS})
        b = analysis_fingerprint(
            "a1",
            {"problems": PROBLEMS[:1], "business_impacts": IMPACTS},
        )
        self.assertNotEqual(a, b)


class PromptTests(SimpleTestCase):
    def test_system_prompt_loaded(self):
        self.assertEqual(RECOMMENDED_ACTIONS_PROMPT_VERSION, "recommended-actions-v1")
        self.assertIn("Do not rediscover", RECOMMENDED_ACTIONS_SYSTEM_PROMPT)

    def test_user_prompt_contains_valid_json(self):
        normalized = _normalized()
        input_payload = build_recommended_actions_input(
            analysis_id="a1",
            organization_id="org1",
            factory_id="f1",
            factory_name="Factory A",
            model_id="f1",
            model_name="Factory A",
            scenario_id="s1",
            scenario_name="Basecase",
            simulation_version="abc123",
            simulation_completed_at=None,
            analysis_generated_at=None,
            problems=PROBLEMS,
            business_impacts=IMPACTS,
            overall_business_impact=OVERALL,
            normalized=normalized,
        )
        prompt = build_recommended_actions_user_prompt(
            factory_name="Factory A",
            model_name="Factory A",
            scenario_name="Basecase",
            simulation_version="abc123",
            analysis_id="a1",
            recommended_actions_input=input_payload,
        )
        self.assertIn("Input data:", prompt)
        self.assertIn('"validated_problems"', prompt)


class ValidatorTests(SimpleTestCase):
    def setUp(self):
        normalized = _normalized()
        self.input_payload = build_recommended_actions_input(
            analysis_id="a1",
            organization_id="org1",
            factory_id="f1",
            factory_name="Factory A",
            model_id="f1",
            model_name="Factory A",
            scenario_id="s1",
            scenario_name="Basecase",
            simulation_version="abc123",
            simulation_completed_at=None,
            analysis_generated_at=None,
            problems=PROBLEMS,
            business_impacts=IMPACTS,
            overall_business_impact=OVERALL,
            normalized=normalized,
        )

    def test_valid_ai_response(self):
        raw = {
            "analysis_id": "a1",
            "recommendation_status": "complete",
            "action_count": 1,
            "recommended_actions": [
                {
                    "id": "action_1",
                    "priority_rank": 1,
                    "priority": "high",
                    "expected_impact_level": "high",
                    "title": "Relieve VT_LATHE flow constraint",
                    "summary": "Reduce pressure on VT_LATHE by reviewing setup losses.",
                    "why": "Linked to validated equipment waiting.",
                    "action_type": "reduce_setup_time",
                    "impact_area": {
                        "id": "VT_LATHE",
                        "name": "VT_LATHE",
                        "type": "work_center",
                    },
                    "related_problem_ids": ["problem_1"],
                    "related_business_impact_ids": ["impact_1"],
                    "implementation_steps": [
                        {"step": 1, "title": "Review setup", "description": "Check setup losses."},
                        {"step": 2, "title": "Re-run", "description": "Validate improvement."},
                    ],
                    "expected_impacts": [
                        {
                            "kpi": "MCT",
                            "direction": "decrease",
                            "display_label": "MCT",
                            "metric_key": "product_mct.HUB1.wait_equipment",
                        }
                    ],
                    "success_indicators": [
                        {
                            "label": "Wait",
                            "metric_key": "product_mct.HUB1.wait_equipment",
                            "desired_direction": "decrease",
                        }
                    ],
                    "time_horizon": "near_term",
                    "confidence": 0.9,
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                }
            ],
        }
        validated = validate_recommended_actions_payload(
            raw, input_payload=self.input_payload, analysis_id="a1"
        )
        self.assertEqual(validated["action_count"], 1)
        self.assertEqual(validated["recommended_actions"][0]["related_problem_ids"], ["problem_1"])

    def test_unknown_problem_id_replaced(self):
        raw = {
            "recommended_actions": [
                {
                    "title": "Balance inspection capacity and arrivals",
                    "summary": "Review labor assignment around inspection peaks.",
                    "why": "Queue pressure.",
                    "action_type": "improve_inspection_flow",
                    "impact_area": {"id": "VT_LATHE", "name": "VT_LATHE", "type": "work_center"},
                    "related_problem_ids": ["problem_999"],
                    "related_business_impact_ids": ["impact_999"],
                    "implementation_steps": [
                        {"step": 1, "title": "Review", "description": "Review evidence."},
                        {"step": 2, "title": "Test", "description": "Re-run scenario."},
                    ],
                    "expected_impacts": [],
                    "evidence_keys": ["invented.key"],
                    "priority": "medium",
                }
            ]
        }
        validated = validate_recommended_actions_payload(
            raw, input_payload=self.input_payload, analysis_id="a1"
        )
        action = validated["recommended_actions"][0]
        self.assertIn(action["related_problem_ids"][0], {"problem_1", "problem_2"})
        self.assertTrue(action["related_business_impact_ids"])
        self.assertNotIn("invented.key", action["evidence_keys"])

    def test_unsupported_capacity_addition_mapped(self):
        raw = {
            "recommended_actions": [
                {
                    "title": "Add another VT_LATHE unit",
                    "summary": "Increase capacity on VT_LATHE after reviewing lower-cost options.",
                    "why": "High utilization with waiting.",
                    "action_type": "increase_capacity",
                    "impact_area": {"id": "VT_LATHE", "name": "VT_LATHE", "type": "equipment"},
                    "related_problem_ids": ["problem_1"],
                    "related_business_impact_ids": ["impact_1"],
                    "implementation_steps": [
                        {"step": 1, "title": "Review", "description": "Review."},
                        {"step": 2, "title": "Test", "description": "Test."},
                    ],
                    "expected_impacts": [],
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                    "priority": "high",
                }
            ]
        }
        validated = validate_recommended_actions_payload(
            raw, input_payload=self.input_payload, analysis_id="a1"
        )
        self.assertEqual(
            validated["recommended_actions"][0]["action_type"],
            "rebalance_workload",
        )

    def test_duplicate_actions_removed(self):
        raw = {
            "recommended_actions": [
                {
                    "title": "Reduce VT_LATHE setup",
                    "summary": "Cut setup losses on VT_LATHE.",
                    "why": "Occupancy.",
                    "action_type": "reduce_setup_time",
                    "impact_area": {"id": "VT_LATHE", "name": "VT_LATHE", "type": "work_center"},
                    "related_problem_ids": ["problem_1"],
                    "related_business_impact_ids": ["impact_1"],
                    "implementation_steps": [
                        {"step": 1, "title": "Review", "description": "Review."},
                        {"step": 2, "title": "Test", "description": "Test."},
                    ],
                    "expected_impacts": [],
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                    "priority": "high",
                },
                {
                    "title": "Improve VT_LATHE changeovers",
                    "summary": "Lower changeover losses on VT_LATHE.",
                    "why": "Same root cause.",
                    "action_type": "reduce_setup_time",
                    "impact_area": {"id": "VT_LATHE", "name": "VT_LATHE", "type": "work_center"},
                    "related_problem_ids": ["problem_1"],
                    "related_business_impact_ids": ["impact_1"],
                    "implementation_steps": [
                        {"step": 1, "title": "Review", "description": "Review."},
                        {"step": 2, "title": "Test", "description": "Test."},
                    ],
                    "expected_impacts": [],
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                    "priority": "medium",
                },
            ]
        }
        validated = validate_recommended_actions_payload(
            raw, input_payload=self.input_payload, analysis_id="a1"
        )
        self.assertEqual(validated["action_count"], 1)

    def test_strip_internal_scores(self):
        payload = {
            "recommended_actions": [
                {
                    "id": "action_1",
                    "title": "A",
                    "action_score": 90,
                    "coverage_score": 80,
                }
            ]
        }
        cleaned = strip_internal_action_scores(payload)
        self.assertNotIn("action_score", cleaned["recommended_actions"][0])
        self.assertNotIn("coverage_score", cleaned["recommended_actions"][0])


class RankerTests(SimpleTestCase):
    def test_ranks_and_limits_to_three(self):
        actions = [
            {"title": "C", "coverage_score": 40, "feasibility_score": 50, "evidence_score": 40, "priority": "low"},
            {"title": "A", "coverage_score": 95, "feasibility_score": 90, "evidence_score": 90, "priority": "high"},
            {"title": "B", "coverage_score": 70, "feasibility_score": 80, "evidence_score": 70, "priority": "medium"},
            {"title": "D", "coverage_score": 30, "feasibility_score": 40, "evidence_score": 30, "priority": "low"},
        ]
        ranked = rank_recommended_actions(actions, limit=3)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["title"], "A")
        self.assertEqual([a["priority_rank"] for a in ranked], [1, 2, 3])
        self.assertEqual(ranked[0]["id"], "action_1")


class FallbackTests(SimpleTestCase):
    def test_deterministic_fallback_returns_actions(self):
        normalized = _normalized()
        input_payload = build_recommended_actions_input(
            analysis_id="a1",
            organization_id="org1",
            factory_id="f1",
            factory_name="Factory A",
            model_id="f1",
            model_name="Factory A",
            scenario_id="s1",
            scenario_name="Basecase",
            simulation_version="abc123",
            simulation_completed_at=None,
            analysis_generated_at=None,
            problems=PROBLEMS,
            business_impacts=IMPACTS,
            overall_business_impact=OVERALL,
            normalized=normalized,
        )
        result = build_deterministic_recommended_actions(
            analysis_id="a1",
            input_payload=input_payload,
        )
        self.assertEqual(result["source"], "deterministic_fallback")
        self.assertGreaterEqual(result["action_count"], 1)
        self.assertLessEqual(result["action_count"], 3)
        for action in result["recommended_actions"]:
            self.assertTrue(action["related_problem_ids"])
            self.assertTrue(action["title"])
