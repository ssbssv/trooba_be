"""
Tests for ChatSessionValidator (saved-data fingerprint locks).
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.ai.models import ChatSession
from apps.ai.services.session_validator import (
    MODEL_CHANGED_MESSAGE,
    ChatSessionValidator,
)


def _factory(**kwargs):
    factory = MagicMock()
    factory.id = kwargs.get("id", "factory-1")
    factory.updated_at = kwargs.get("updated_at", timezone.now())
    factory.last_run_at = kwargs.get("last_run_at", None)
    factory.run_status = kwargs.get("run_status", "current")
    return factory


def _session(**kwargs):
    session = MagicMock(spec=ChatSession)
    session.status = kwargs.get("status", ChatSession.STATUS_ACTIVE)
    session.lock_reason = kwargs.get("lock_reason", None)
    session.model_version = kwargs.get("model_version", "")
    session.last_activity_at = kwargs.get("last_activity_at", timezone.now())
    session.updated_at = kwargs.get("updated_at", timezone.now())
    session.created_at = kwargs.get("created_at", timezone.now())
    session.pk = kwargs.get("pk", "session-1")
    return session


class ChatSessionValidatorTests(SimpleTestCase):
    def setUp(self):
        self.validator = ChatSessionValidator()

    @patch(
        "apps.ai.services.session_validator.resolve_factory_model_version",
        return_value="fingerprint-v1",
    )
    def test_active_session_passes(self, _mock_version):
        factory = _factory()
        session = _session(model_version="fingerprint-v1")
        result = self.validator.validate(session, factory=factory)
        self.assertTrue(result.ok)
        self.assertFalse(result.session_expired)
        self.assertFalse(result.model_changed)

    @patch(
        "apps.ai.services.session_validator.resolve_factory_model_version",
        return_value="fingerprint-v1",
    )
    def test_old_inactivity_no_longer_locks(self, _mock_version):
        """Idle time alone must not lock the session."""
        from datetime import timedelta

        factory = _factory()
        session = _session(
            model_version="fingerprint-v1",
            last_activity_at=timezone.now() - timedelta(hours=48),
        )
        self.validator.lock_session = MagicMock()
        result = self.validator.validate(session, factory=factory)
        self.assertTrue(result.ok)
        self.validator.lock_session.assert_not_called()

    @patch(
        "apps.ai.services.session_validator.resolve_factory_model_version",
        return_value="fingerprint-new",
    )
    def test_model_change_locks_session(self, _mock_version):
        factory = _factory()
        session = _session(model_version="fingerprint-old")
        self.validator.lock_session = MagicMock(
            side_effect=lambda s, reason: setattr(s, "status", ChatSession.STATUS_READ_ONLY)
            or setattr(s, "lock_reason", reason)
            or s
        )

        result = self.validator.validate(session, factory=factory)
        self.assertFalse(result.ok)
        self.assertTrue(result.model_changed)
        self.assertEqual(result.message, MODEL_CHANGED_MESSAGE)
        self.assertFalse(result.should_auto_replace)

    def test_already_read_only_model_changed(self):
        factory = _factory()
        session = _session(
            status=ChatSession.STATUS_READ_ONLY,
            lock_reason=ChatSession.LOCK_MODEL_CHANGED,
        )
        result = self.validator.validate(session, factory=factory)
        self.assertFalse(result.ok)
        self.assertTrue(result.model_changed)

    def test_legacy_timeout_lock_treated_as_model_changed(self):
        factory = _factory()
        session = _session(
            status=ChatSession.STATUS_READ_ONLY,
            lock_reason=ChatSession.LOCK_SESSION_TIMEOUT,
        )
        result = self.validator.validate(session, factory=factory)
        self.assertFalse(result.ok)
        self.assertTrue(result.model_changed)
        self.assertEqual(result.message, MODEL_CHANGED_MESSAGE)
