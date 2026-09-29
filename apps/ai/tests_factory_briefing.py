"""
Tests for Factory Briefing validation, caching and AI failure handling.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.ai.llm.base import LLMResponse
from apps.ai.llm.http import LLMProviderError
from apps.ai.services.briefing_validation import (
    count_words,
    needs_shorten,
    sanitize_briefing,
    strip_formatting,
    truncate_at_sentence,
)
from apps.ai.services.factory_briefing_service import (
    GENERATE_WITH_EXISTING_MESSAGE,
    NEEDS_SIMULATION_MESSAGE,
    STALE_MESSAGE,
    FactoryBriefingService,
)


class BriefingValidationTests(SimpleTestCase):
    def test_strip_formatting_removes_markdown_keeps_keywords(self):
        raw = "# Title\n\n- bullet one\n\nOverall **{VT_LATHE}** is tight."
        cleaned = strip_formatting(raw)
        self.assertNotIn("# Title", cleaned)
        self.assertNotIn("- bullet", cleaned)
        self.assertIn("{VT_LATHE}", cleaned)
        self.assertNotIn("**", cleaned)

    def test_sanitize_limits_to_four_paragraphs(self):
        raw = "\n\n".join([f"Paragraph {i} with enough detail here." for i in range(1, 7)])
        briefing, paragraphs = sanitize_briefing(raw)
        self.assertEqual(len(paragraphs), 4)
        self.assertEqual(briefing.count("\n\n"), 3)

    def test_truncate_at_sentence_boundary(self):
        sentence = "Alpha findings are clear. "
        raw = sentence * 80
        truncated = truncate_at_sentence(raw, max_words=50)
        self.assertLessEqual(count_words(truncated), 55)
        self.assertTrue(truncated.endswith(".") or truncated.endswith("."))

    def test_needs_shorten_when_too_long(self):
        long_text = "word " * 320
        self.assertTrue(needs_shorten(long_text))
        self.assertFalse(needs_shorten("Short paragraph one.\n\nShort paragraph two."))


class FactoryBriefingServiceTests(SimpleTestCase):
    def setUp(self):
        self.llm = MagicMock()
        self.service = FactoryBriefingService(llm_client=self.llm)
        self.factory = MagicMock()
        self.factory.id = "factory-1"
        self.factory.run_status = "current"
        self.scenario = MagicMock()
        self.scenario.id = "scenario-base"
        self.scenario.is_basecase = True
        self.scenario.status = "calculated"

    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value=None,
    )
    def test_needs_simulation_when_no_results(self, _mock_version):
        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertFalse(result.success)
        self.assertTrue(result.needs_simulation)
        self.assertEqual(result.error, NEEDS_SIMULATION_MESSAGE)
        self.llm.chat.assert_not_called()

    @patch("apps.ai.services.factory_briefing_service.FactoryBriefing.objects")
    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value="result-v1",
    )
    def test_returns_cached_briefing_when_version_matches(self, _mock_version, mock_objects):
        row = MagicMock()
        row.briefing = "Cached para one.\n\nCached para two.\n\nCached para three."
        row.paragraphs = ["Cached para one.", "Cached para two.", "Cached para three."]
        row.generated_at = timezone.now()
        row.model_name = "gpt-test"
        row.result_version = "result-v1"
        mock_objects.filter.return_value.order_by.return_value.first.return_value = row

        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertTrue(result.success)
        self.assertTrue(result.cached)
        self.assertEqual(result.paragraphs[0], "Cached para one.")
        self.llm.chat.assert_not_called()

    @patch("apps.ai.services.factory_briefing_service.FactoryBriefing.objects")
    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value="result-v1",
    )
    def test_stale_flag_when_needs_recalc_with_cache(self, _mock_version, mock_objects):
        self.factory.run_status = "needs_recalc"
        row = MagicMock()
        row.briefing = "Old briefing remains."
        row.paragraphs = ["Old briefing remains."]
        row.generated_at = timezone.now()
        row.model_name = "gpt-test"
        row.result_version = "result-v1"
        mock_objects.filter.return_value.order_by.return_value.first.return_value = row

        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertTrue(result.success)
        self.assertTrue(result.is_stale)
        self.assertEqual(result.stale_message, STALE_MESSAGE)
        self.llm.chat.assert_not_called()

    @patch("apps.ai.services.factory_briefing_service.FactoryBriefing.objects")
    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value="result-v1",
    )
    def test_offers_generate_with_existing_when_stale_and_no_cache(
        self, _mock_version, mock_objects
    ):
        self.factory.run_status = "needs_recalc"
        mock_objects.filter.return_value.order_by.return_value.first.return_value = None

        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertFalse(result.success)
        self.assertTrue(result.can_generate_with_existing)
        self.assertEqual(result.error, GENERATE_WITH_EXISTING_MESSAGE)
        self.llm.chat.assert_not_called()

    @patch.object(FactoryBriefingService, "_persist")
    @patch.object(FactoryBriefingService, "_generate")
    @patch("apps.ai.services.factory_briefing_service.FactoryBriefing.objects")
    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value="result-v2",
    )
    def test_regenerates_when_result_version_changes(
        self, _mock_version, mock_objects, mock_generate, mock_persist
    ):
        old = MagicMock()
        old.result_version = "result-v1"
        mock_objects.filter.return_value.order_by.return_value.first.return_value = old

        mock_generate.return_value = (
            "P1 with {bottleneck}.\n\nP2 continues.\n\nP3 closes.",
            ["P1 with {bottleneck}.", "P2 continues.", "P3 closes."],
            "gpt-test",
        )
        saved = MagicMock()
        saved.briefing = "P1 with {bottleneck}.\n\nP2 continues.\n\nP3 closes."
        saved.paragraphs = ["P1 with {bottleneck}.", "P2 continues.", "P3 closes."]
        saved.generated_at = timezone.now()
        saved.model_name = "gpt-test"
        mock_persist.return_value = saved

        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertTrue(result.success)
        self.assertFalse(result.cached)
        mock_generate.assert_called_once()
        mock_persist.assert_called_once()

    @patch.object(FactoryBriefingService, "_persist")
    @patch.object(FactoryBriefingService, "_generate")
    @patch("apps.ai.services.factory_briefing_service.FactoryBriefing.objects")
    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value="result-v1",
    )
    def test_force_generate_with_stale_existing_results(
        self, _mock_version, mock_objects, mock_generate, mock_persist
    ):
        self.factory.run_status = "needs_recalc"
        mock_objects.filter.return_value.order_by.return_value.first.return_value = None
        mock_generate.return_value = (
            "Forced para one.\n\nForced para two.\n\nForced para three.",
            ["Forced para one.", "Forced para two.", "Forced para three."],
            "gpt-test",
        )
        saved = MagicMock()
        saved.briefing = "Forced para one.\n\nForced para two.\n\nForced para three."
        saved.paragraphs = ["Forced para one.", "Forced para two.", "Forced para three."]
        saved.generated_at = timezone.now()
        saved.model_name = "gpt-test"
        mock_persist.return_value = saved

        result = self.service.get_or_generate(
            factory=self.factory,
            scenario=self.scenario,
            force_generate=True,
        )
        self.assertTrue(result.success)
        mock_generate.assert_called_once()

    @patch("apps.ai.services.factory_briefing_service.FactoryBriefing.objects")
    @patch(
        "apps.ai.services.factory_briefing_service.resolve_scenario_result_version",
        return_value="result-v1",
    )
    def test_ai_failure_returns_friendly_error_when_no_cache(self, _mock_version, mock_objects):
        mock_objects.filter.return_value.order_by.return_value.first.return_value = None
        self.service._generate = MagicMock(side_effect=LLMProviderError("down"))

        result = self.service.get_or_generate(
            factory=self.factory, scenario=self.scenario
        )
        self.assertFalse(result.success)
        self.assertIn("Something went wrong", result.error)
        self.assertEqual(result.briefing, "")
        self.assertEqual(result.paragraphs, [])

    @patch.object(FactoryBriefingService, "_shorten")
    def test_generate_shortens_when_too_long(self, mock_shorten):
        long_body = ("Long sentence about factory performance with details. " * 40).strip()
        long_text = f"{long_body}\n\n{long_body}\n\n{long_body}\n\n{long_body}"
        short_text = (
            "Short para one with {VT_LATHE}.\n\n"
            "Short para two about {WIP}.\n\n"
            "Short para three with actions."
        )
        self.llm.chat.return_value = LLMResponse(content=long_text, model="gpt-test")
        mock_shorten.return_value = short_text

        self.service.context_builder.load_scenario_factory_input = MagicMock(
            return_value={"name": "F"}
        )
        self.service.context_builder.load_factory_output = MagicMock(
            return_value={"ok": True}
        )

        briefing, paragraphs, model_name = self.service._generate(
            self.factory, self.scenario
        )

        self.assertEqual(model_name, "gpt-test")
        self.assertLessEqual(count_words(briefing), 300)
        self.assertLessEqual(len(paragraphs), 4)
        mock_shorten.assert_called_once()
