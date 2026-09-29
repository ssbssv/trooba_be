"""Shared helpers for session/CSRF cookie attributes (Safari-safe delete, etc.)."""

from __future__ import annotations

from django.conf import settings


def _cookie_delete_kwargs(
    *,
    domain_setting: str,
    samesite_setting: str,
) -> dict:
    # delete_cookie() only accepts path/domain/samesite — not secure.
    # Django sets Secure automatically when samesite is "None".
    kwargs: dict = {"path": "/"}
    domain = getattr(settings, domain_setting, None) or None
    if domain:
        kwargs["domain"] = domain
    samesite = getattr(settings, samesite_setting, None)
    if samesite:
        kwargs["samesite"] = samesite
    return kwargs


def session_cookie_delete_kwargs() -> dict:
    return _cookie_delete_kwargs(
        domain_setting="SESSION_COOKIE_DOMAIN",
        samesite_setting="SESSION_COOKIE_SAMESITE",
    )


def csrf_cookie_delete_kwargs() -> dict:
    return _cookie_delete_kwargs(
        domain_setting="CSRF_COOKIE_DOMAIN",
        samesite_setting="CSRF_COOKIE_SAMESITE",
    )


def clear_auth_cookies(response) -> None:
    """Remove session + CSRF cookies using the same attributes they were set with."""
    response.delete_cookie(settings.SESSION_COOKIE_NAME, **session_cookie_delete_kwargs())
    response.delete_cookie("csrftoken", **csrf_cookie_delete_kwargs())
