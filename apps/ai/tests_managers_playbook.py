"""
Tests for Manager's Playbook input building, validation, prompts, and fallback.
"""
from django.test import SimpleTestCase

from apps.ai.prompts.managers_playbook import (
    MANAGERS_PLAYBOOK_PROMPT_VERSION,
    MANAGERS_PLAYBOOK_SYSTEM_PROMPT,
    build_managers_playbook_user_prompt,
)
from apps.ai.services.factory_analysis.normalizer import normalize_simulation_for_analysis
from apps.ai.services.managers_playbook.fallback import (
    build_deterministic_managers_playbook,
)
from apps.ai.services.managers_playbook.input_builder import (
    build_managers_playbook_input,
    playbook_cache_fingerprint,
)
from apps.ai.services.managers_playbook.validator import (
    validate_managers_playbook_payload,
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
        },
        {
            "id": "eq2",
            "name": "MILL",
            "totalUtil": 72.0,
            "wip_queue": 4.0,
            "wip_total": 6.0,
            "wait_min": 2.0,
            "num_av": 1,
        },
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

RECOMMENDATIONS = [
    {
        "id": "action_1",
        "priority_rank": 1,
        "priority": "high",
        "expected_impact_level": "high",
        "title": "Relieve VT_LATHE flow constraint",
        "summary": "Reduce avoidable occupancy around VT_LATHE.",
        "why": "Primary constraint.",
        "action_type": "reduce_setup_time",
        "impact_area": {"id": "VT_LATHE", "name": "VT_LATHE", "type": "work_center"},
        "related_problem_ids": ["problem_1"],
        "related_business_impact_ids": ["impact_1"],
        "implementation_steps": [],
        "expected_impacts": [],
        "success_indicators": [],
        "time_horizon": "near_term",
        "confidence": 0.9,
        "evidence_keys": ["equipment.utilization.VT_LATHE"],
    },
    {
        "id": "action_2",
        "priority_rank": 2,
        "priority": "medium",
        "expected_impact_level": "medium",
        "title": "Reduce HUB1 lot-waiting delay",
        "summary": "Review lot size for HUB1.",
        "why": "Lot waiting.",
        "action_type": "adjust_lot_size",
        "impact_area": {"id": "HUB1", "name": "HUB1", "type": "product"},
        "related_problem_ids": ["problem_2"],
        "related_business_impact_ids": ["impact_2"],
        "implementation_steps": [],
        "expected_impacts": [],
        "success_indicators": [],
        "time_horizon": "near_term",
        "confidence": 0.8,
        "evidence_keys": ["product_mct.HUB1.wait_lot"],
    },
    {
        "id": "action_3",
        "priority_rank": 3,
        "priority": "medium",
        "expected_impact_level": "medium",
        "title": "Balance labor assignment",
        "summary": "Use available labor more flexibly.",
        "why": "Labor flexibility.",
        "action_type": "change_labor_assignment",
        "impact_area": {"id": "MACHINIST", "name": "MACHINIST", "type": "labor"},
        "related_problem_ids": ["problem_1", "problem_2"],
        "related_business_impact_ids": ["impact_1"],
        "implementation_steps": [],
        "expected_impacts": [],
        "success_indicators": [],
        "time_horizon": "near_term",
        "confidence": 0.75,
        "evidence_keys": ["equipment.utilization.VT_LATHE"],
    },
]


def _input_payload():
    return build_managers_playbook_input(
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
        recommended_actions=RECOMMENDATIONS,
        normalized=_normalized(),
    )


class InputBuilderTests(SimpleTestCase):
    def test_includes_validated_sections(self):
        payload = _input_payload()
        for key in (
            "analysis_context",
            "validated_problems",
            "validated_business_impacts",
            "validated_overall_business_impact",
            "validated_recommended_actions",
            "simulation_evidence",
            "management_context",
            "metric_catalog",
            "playbook_constraints",
        ):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["validated_problems"]), 2)
        self.assertEqual(len(payload["validated_business_impacts"]), 2)
        self.assertTrue(payload["validated_overall_business_impact"])
        self.assertEqual(len(payload["validated_recommended_actions"]), 3)
        self.assertIn("equipment.utilization.VT_LATHE", payload["metric_catalog"])

    def test_fingerprint_changes_with_recommendations(self):
        fp1 = playbook_cache_fingerprint(
            analysis_id="a1",
            analysis_payload={"problems": PROBLEMS, "business_impacts": IMPACTS},
            recommendations_payload={"recommended_actions": RECOMMENDATIONS},
        )
        fp2 = playbook_cache_fingerprint(
            analysis_id="a1",
            analysis_payload={"problems": PROBLEMS, "business_impacts": IMPACTS},
            recommendations_payload={"recommended_actions": RECOMMENDATIONS[:1]},
        )
        self.assertNotEqual(fp1, fp2)


class PromptTests(SimpleTestCase):
    def test_prompt_version(self):
        self.assertEqual(MANAGERS_PLAYBOOK_PROMPT_VERSION, "managers-playbook-v1")
        self.assertIn("Manager's Playbook", MANAGERS_PLAYBOOK_SYSTEM_PROMPT)

    def test_user_prompt_contains_valid_json(self):
        payload = _input_payload()
        prompt = build_managers_playbook_user_prompt(
            factory_name="Factory A",
            model_name="Factory A",
            scenario_name="Basecase",
            simulation_version="abc123",
            analysis_id="a1",
            managers_playbook_input=payload,
        )
        self.assertIn('"validated_problems"', prompt)
        self.assertIn('"validated_recommended_actions"', prompt)
        self.assertIn("Return valid JSON only", prompt)


class ValidatorTests(SimpleTestCase):
    def _valid_ai_response(self):
        return {
            "analysis_id": "a1",
            "playbook_status": "complete",
            "overview": (
                "Based on the current constraints, I would first stabilize the areas "
                "creating the most waiting, then verify the results before making major "
                "capacity or staffing decisions."
            ),
            "action_count": 5,
            "manager_actions": [
                {
                    "id": "manager_action_1",
                    "rank": 1,
                    "title": "I would stabilize VT_LATHE before adding capacity.",
                    "description": (
                        "I would first review setup losses on VT_LATHE. "
                        "I would test these changes before considering another machine."
                    ),
                    "action_intent": "stabilize_primary_constraint",
                    "impact_area": {
                        "id": "VT_LATHE",
                        "name": "VT_LATHE",
                        "type": "work_center",
                    },
                    "related_problem_ids": ["problem_1"],
                    "related_recommendation_ids": ["action_1"],
                    "related_business_impact_ids": ["impact_1"],
                    "time_horizon": "immediate",
                    "confidence": 0.94,
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                },
                {
                    "id": "manager_action_2",
                    "rank": 2,
                    "title": "Next, I would control the TURNING queue.",
                    "description": "I would tighten release into TURNING to limit WIP growth.",
                    "action_intent": "control_queue",
                    "impact_area": {"id": "TURNING", "name": "TURNING", "type": "operation"},
                    "related_problem_ids": ["problem_2"],
                    "related_recommendation_ids": ["action_2"],
                    "related_business_impact_ids": ["impact_2"],
                    "time_horizon": "immediate",
                    "confidence": 0.88,
                    "evidence_keys": ["product_mct.HUB1.wait_lot"],
                },
                {
                    "id": "manager_action_3",
                    "rank": 3,
                    "title": "Then, I would reduce HUB1's non-productive waiting.",
                    "description": "I would target Wait for Equipment and Wait for Lot on HUB1.",
                    "action_intent": "reduce_non_value_added_mct",
                    "impact_area": {"id": "HUB1", "name": "HUB1", "type": "product"},
                    "related_problem_ids": ["problem_1", "problem_2"],
                    "related_recommendation_ids": ["action_1", "action_2"],
                    "related_business_impact_ids": ["impact_1"],
                    "time_horizon": "near_term",
                    "confidence": 0.9,
                    "evidence_keys": ["product_mct.HUB1.wait_equipment"],
                },
                {
                    "id": "manager_action_4",
                    "rank": 4,
                    "title": "I would use available labor more flexibly.",
                    "description": "I would rebalance MACHINIST assignment around the constraint.",
                    "action_intent": "rebalance_labor",
                    "impact_area": {
                        "id": "MACHINIST",
                        "name": "MACHINIST",
                        "type": "labor",
                    },
                    "related_problem_ids": ["problem_1"],
                    "related_recommendation_ids": ["action_3"],
                    "related_business_impact_ids": ["impact_1"],
                    "time_horizon": "near_term",
                    "confidence": 0.8,
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                },
                {
                    "id": "manager_action_5",
                    "rank": 5,
                    "title": "Finally, I would monitor MILL as the next constraint.",
                    "description": "I would re-run the scenario and watch MILL utilization carefully.",
                    "action_intent": "validate_scenario",
                    "impact_area": {"id": "MILL", "name": "MILL", "type": "work_center"},
                    "related_problem_ids": ["problem_1"],
                    "related_recommendation_ids": ["action_1"],
                    "related_business_impact_ids": ["impact_1"],
                    "time_horizon": "near_term",
                    "confidence": 0.77,
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                },
            ],
            "final_summary": (
                "These five steps should reduce avoidable waiting, improve flow "
                "stability, and support better capacity decisions."
            ),
        }

    def test_valid_ai_response(self):
        payload = _input_payload()
        validated = validate_managers_playbook_payload(
            self._valid_ai_response(),
            input_payload=payload,
            analysis_id="a1",
        )
        self.assertEqual(validated["playbook_status"], "complete")
        self.assertEqual(validated["action_count"], 5)
        self.assertEqual(len(validated["manager_actions"]), 5)
        words = validated["overview"].split()
        self.assertGreaterEqual(len(words), 25)
        self.assertLessEqual(len(words), 45)
        summary_words = validated["final_summary"].split()
        self.assertGreaterEqual(len(summary_words), 15)
        self.assertLessEqual(len(summary_words), 30)
        for action in validated["manager_actions"]:
            self.assertTrue(action["title"].lower().startswith("i would") or "i would" in action["title"].lower() or action["title"].lower().startswith("next") or action["title"].lower().startswith("then") or action["title"].lower().startswith("finally"))

    def test_unknown_ids_are_filtered(self):
        payload = _input_payload()
        raw = self._valid_ai_response()
        raw["manager_actions"][0]["related_problem_ids"] = ["problem_99"]
        raw["manager_actions"][0]["related_recommendation_ids"] = ["action_99"]
        raw["manager_actions"][0]["related_business_impact_ids"] = ["impact_99"]
        raw["manager_actions"][0]["evidence_keys"] = ["fake.metric"]
        validated = validate_managers_playbook_payload(
            raw, input_payload=payload, analysis_id="a1"
        )
        action = validated["manager_actions"][0]
        self.assertIn(action["related_problem_ids"][0], {"problem_1", "problem_2"})
        self.assertIn(action["related_recommendation_ids"][0], {"action_1", "action_2", "action_3"})
        self.assertTrue(action["related_business_impact_ids"])
        self.assertNotIn("fake.metric", action["evidence_keys"])

    def test_duplicate_actions_deduped(self):
        payload = _input_payload()
        raw = self._valid_ai_response()
        raw["manager_actions"][1] = dict(raw["manager_actions"][0])
        raw["manager_actions"][1]["title"] = "I would stabilize VT_LATHE again."
        validated = validate_managers_playbook_payload(
            raw, input_payload=payload, analysis_id="a1"
        )
        self.assertLess(len(validated["manager_actions"]), 5)

    def test_unsupported_machine_purchase_rewritten(self):
        payload = _input_payload()
        raw = self._valid_ai_response()
        raw["manager_actions"][0]["title"] = "I would buy another machine for VT_LATHE."
        raw["manager_actions"][0]["description"] = (
            "I would purchase another machine immediately to add capacity."
        )
        validated = validate_managers_playbook_payload(
            raw, input_payload=payload, analysis_id="a1"
        )
        title = validated["manager_actions"][0]["title"].lower()
        self.assertNotIn("buy another machine for", title)
        self.assertIn("stabilize", title)

    def test_unsupported_hiring_rewritten(self):
        payload = _input_payload()
        raw = self._valid_ai_response()
        raw["manager_actions"][3]["title"] = "I would hire additional operators now."
        raw["manager_actions"][3]["description"] = (
            "I would hire more staff to clear the queue."
        )
        validated = validate_managers_playbook_payload(
            raw, input_payload=payload, analysis_id="a1"
        )
        title = validated["manager_actions"][3]["title"].lower()
        self.assertIn("flexibly", title)
        self.assertNotIn("hire additional", title)

    def test_cost_claim_stripped(self):
        payload = _input_payload()
        raw = self._valid_ai_response()
        raw["manager_actions"][0]["description"] = (
            "I would save $500000 with 20% ROI guaranteed."
        )
        validated = validate_managers_playbook_payload(
            raw, input_payload=payload, analysis_id="a1"
        )
        desc = validated["manager_actions"][0]["description"].lower()
        self.assertNotIn("$500000", desc)
        self.assertNotIn("guaranteed", desc)


class FallbackTests(SimpleTestCase):
    def test_deterministic_fallback(self):
        payload = _input_payload()
        result = build_deterministic_managers_playbook(
            analysis_id="a1",
            input_payload=payload,
        )
        self.assertEqual(result["source"], "deterministic_fallback")
        self.assertIn(result["playbook_status"], {"complete", "limited_actions"})
        self.assertGreaterEqual(len(result["manager_actions"]), 3)
        for action in result["manager_actions"]:
            self.assertTrue(
                action["title"].lower().startswith("i would")
                or "i would" in action["title"].lower()
                or action["title"].lower().startswith("next")
                or action["title"].lower().startswith("then")
                or action["title"].lower().startswith("finally")
            )
