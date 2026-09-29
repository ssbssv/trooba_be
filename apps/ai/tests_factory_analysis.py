"""
Tests for Factory Analysis normalization, validation, ranking, and fallback.
"""
from django.test import SimpleTestCase

from apps.ai.services.factory_analysis.fallback import build_deterministic_analysis
from apps.ai.services.factory_analysis.normalizer import normalize_simulation_for_analysis
from apps.ai.services.factory_analysis.ranker import rank_top_problems
from apps.ai.services.factory_analysis.validator import (
    extract_json_object,
    strip_internal_scores,
    validate_analysis_payload,
)
from apps.ai.prompts.factory_analysis import FACTORY_ANALYSIS_PROMPT_VERSION
from apps.ai.services.factory_analysis_service import FactoryAnalysisService
from unittest.mock import MagicMock, patch


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
        },
        {
            "id": "SHAFT1",
            "name": "SHAFT1",
            "demand": 50,
            "goodMade": 50,
            "goodShipped": 50,
            "started": 50,
            "scrap": 0,
            "wip": 10,
            "mct": 10.0,
            "mctQueue": 1.0,
            "mctWaitLabor": 0.5,
            "mctLotWait": 0.5,
            "mctSetup": 2.0,
            "mctRun": 6.0,
        },
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


class NormalizerTests(SimpleTestCase):
    def test_builds_metric_catalog_and_mct_components(self):
        normalized = normalize_simulation_for_analysis(
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
        catalog = normalized["metric_catalog"]
        self.assertIn("product_mct.HUB1.total", catalog)
        self.assertIn("product_mct.HUB1.wait_equipment", catalog)
        self.assertIn("equipment.utilization.VT_LATHE", catalog)
        self.assertEqual(catalog["equipment.utilization.VT_LATHE"]["value"], 89.55)
        hub = next(p for p in normalized["products"] if p["product_id"] == "HUB1")
        self.assertEqual(hub["mct"]["wait_equipment"], 12.0)
        self.assertEqual(hub["mct"]["wait_labor"], 3.0)
        self.assertEqual(hub["mct"]["wait_lot"], 2.0)


class RankerTests(SimpleTestCase):
    def test_ranks_by_score_not_severity_and_limits_to_four(self):
        problems = [
            {"problem": "A", "severity": "high", "problem_score": 60, "area": "A", "category": "a", "root_cause": "a", "affected_resources": ["A"], "affected_products": [], "affected_operations": []},
            {"problem": "B", "severity": "low", "problem_score": 95, "area": "B", "category": "b", "root_cause": "b", "affected_resources": ["B"], "affected_products": [], "affected_operations": []},
            {"problem": "C", "severity": "medium", "problem_score": 80, "area": "C", "category": "c", "root_cause": "c", "affected_resources": ["C"], "affected_products": [], "affected_operations": []},
            {"problem": "D", "severity": "high", "problem_score": 70, "area": "D", "category": "d", "root_cause": "d", "affected_resources": ["D"], "affected_products": [], "affected_operations": []},
            {"problem": "E", "severity": "high", "problem_score": 50, "area": "E", "category": "e", "root_cause": "e", "affected_resources": ["E"], "affected_products": [], "affected_operations": []},
        ]
        ranked = rank_top_problems(problems, limit=4)
        self.assertEqual(len(ranked), 4)
        self.assertEqual(ranked[0]["problem"], "B")
        self.assertEqual(ranked[0]["severity"], "low")
        self.assertEqual(ranked[0]["id"], "problem_1")
        self.assertEqual([p["rank"] for p in ranked], [1, 2, 3, 4])


class ValidatorTests(SimpleTestCase):
    def setUp(self):
        self.normalized = normalize_simulation_for_analysis(
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

    def test_extract_json_from_fence(self):
        raw = '```json\n{"analysis_status":"complete","problems":[]}\n```'
        parsed = extract_json_object(raw)
        self.assertEqual(parsed["analysis_status"], "complete")

    def test_metric_values_rewritten_from_catalog(self):
        raw = {
            "analysis_status": "complete",
            "problems": [
                {
                    "id": "problem_1",
                    "rank": 1,
                    "problem": "VT_LATHE capacity pressure",
                    "summary": "VT_LATHE is constrained.",
                    "root_cause": "High utilization with queue pressure.",
                    "severity": "high",
                    "category": "equipment_constraint",
                    "area": "VT_LATHE",
                    "problem_score": 92,
                    "detailed_reasoning": (
                        "VT_LATHE shows elevated utilization with queue evidence in the "
                        "simulation. This creates Wait for Equipment pressure for routed "
                        "products such as HUB1. The finding is based on utilization and "
                        "queue metrics together, not utilization alone. Operational impact "
                        "is meaningful because multiple flow indicators align around the "
                        "same resource. Setup and run time are not treated as the problem "
                        "here. The evidence supports ranking this among the top issues."
                    ),
                    "why_top_four": "Strongest capacity-plus-queue signal.",
                    "affected_products": ["HUB1"],
                    "affected_operations": ["TURNING"],
                    "affected_resources": ["VT_LATHE"],
                    "supporting_metrics": [
                        {
                            "metric_key": "equipment.utilization.VT_LATHE",
                            "label": "wrong",
                            "value": 999,
                            "formatted_value": "999%",
                            "unit": "%",
                            "source": "simulation_result",
                        }
                    ],
                    "side_box": {
                        "prefix": "Primary pressure",
                        "subject": "VT_LATHE utilization",
                        "value": "999%",
                        "description": "High utilization with queue pressure.",
                        "metric_key": "equipment.utilization.VT_LATHE",
                    },
                    "confidence": 0.9,
                    "evidence_keys": ["equipment.utilization.VT_LATHE", "fake.key"],
                }
            ],
            "business_impacts": [
                {
                    "id": "impact_1",
                    "rank": 1,
                    "title": "Manufacturing Cycle Time is likely to increase",
                    "description": "Waiting may grow if the constraint remains unresolved.",
                    "severity": "high",
                    "category": "cycle_time",
                    "time_horizon": "near_term",
                    "related_problem_ids": ["problem_1"],
                    "causal_chain": ["Pressure remains", "Waiting grows", "MCT rises"],
                    "confidence": 0.8,
                    "evidence_keys": ["equipment.utilization.VT_LATHE"],
                }
            ]
            * 5,
            "overall_business_impact": {
                "title": "Factory flow efficiency is likely to decline",
                "description": (
                    "If current constraints remain unresolved, waiting is likely to persist "
                    "and Manufacturing Cycle Time may continue rising for affected products."
                ),
                "severity": "high",
                "time_horizon": "medium_term",
                "related_problem_ids": ["problem_1"],
                "confidence": 0.85,
            },
        }
        validated = validate_analysis_payload(raw, normalized=self.normalized)
        self.assertEqual(validated["problem_count"], 1)
        metric = validated["problems"][0]["supporting_metrics"][0]
        self.assertEqual(metric["value"], 89.55)
        self.assertEqual(metric["formatted_value"], "89.55%")
        self.assertNotIn("fake.key", validated["problems"][0]["evidence_keys"])
        self.assertEqual(validated["problems"][0]["side_box"]["value"], "89.55%")
        public = strip_internal_scores(validated)
        self.assertNotIn("problem_score", public["problems"][0])

    def test_impacts_cannot_reference_discarded_problems(self):
        raw = {
            "analysis_status": "complete",
            "problems": [],
            "business_impacts": [
                {
                    "title": "Something may worsen",
                    "description": "Likely future consequence.",
                    "related_problem_ids": ["problem_99"],
                    "severity": "medium",
                    "time_horizon": "near_term",
                }
            ],
            "overall_business_impact": None,
        }
        validated = validate_analysis_payload(raw, normalized=self.normalized)
        self.assertEqual(validated["problem_count"], 0)
        self.assertEqual(validated["business_impacts"], [])


class FallbackTests(SimpleTestCase):
    def test_fallback_returns_up_to_four_and_five_impacts(self):
        normalized = normalize_simulation_for_analysis(
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
        analysis = build_deterministic_analysis(normalized)
        self.assertEqual(analysis["source"], "deterministic_fallback")
        self.assertLessEqual(analysis["problem_count"], 4)
        self.assertGreaterEqual(analysis["problem_count"], 1)
        self.assertEqual(len(analysis["business_impacts"]), 5)
        ids = {p["id"] for p in analysis["problems"]}
        for impact in analysis["business_impacts"]:
            self.assertTrue(set(impact["related_problem_ids"]).issubset(ids))
        self.assertIsNotNone(analysis["overall_business_impact"])


class FactoryAnalysisServiceCacheTests(SimpleTestCase):
    def setUp(self):
        self.llm = MagicMock()
        self.service = FactoryAnalysisService(llm_client=self.llm)
        self.factory = MagicMock()
        self.factory.id = "factory-1"
        self.factory.name = "Factory A"
        self.factory.run_status = "current"
        self.scenario = MagicMock()
        self.scenario.id = "scenario-base"
        self.scenario.name = "Basecase"
        self.scenario.is_basecase = True
        self.scenario.status = "calculated"
        self.scenario.pk = "scenario-base"

    @patch(
        "apps.ai.services.factory_analysis_service.resolve_scenario_result_version",
        return_value=None,
    )
    def test_needs_simulation_when_no_results(self, _mock_version):
        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertFalse(result.success)
        self.assertTrue(result.needs_simulation)
        self.llm.chat.assert_not_called()

    @patch("apps.ai.services.factory_analysis_service.FactoryAnalysis.objects")
    @patch(
        "apps.ai.services.factory_analysis_service.resolve_scenario_result_version",
        return_value="result-v1",
    )
    def test_returns_cached_analysis_when_versions_match(self, _mock_version, mock_objects):
        row = MagicMock()
        row.id = "analysis-1"
        row.factory_id = "factory-1"
        row.scenario_id = "scenario-base"
        row.result_version = "result-v1"
        row.prompt_version = FACTORY_ANALYSIS_PROMPT_VERSION
        row.model_name = "gpt-test"
        row.source = "ai"
        row.generated_at = MagicMock()
        row.generated_at.isoformat.return_value = "2026-07-20T10:00:00+00:00"
        row.analysis = {
            "analysis_status": "complete",
            "problem_count": 1,
            "problems": [{"id": "problem_1", "problem": "Test", "problem_score": 90}],
            "business_impacts": [],
            "overall_business_impact": None,
        }
        mock_objects.filter.return_value.order_by.return_value.first.return_value = row

        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertTrue(result.success)
        self.assertTrue(result.cache_hit)
        self.assertNotIn("problem_score", result.problems[0])
        self.llm.chat.assert_not_called()
