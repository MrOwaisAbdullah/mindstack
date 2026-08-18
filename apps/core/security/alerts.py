"""Suspicious-activity alert helpers.

Single entry point — `notify_staff(subject, body)` — that the security
layer calls when a noteworthy event fires:

- Admin-login lockout sentinel set (failed-staff-login crosses the
  Phase 9.2.4 threshold).
- New MCP OAuth client registered (anonymous DCR is open by default;
  staff want a heads-up when one lands).
- Magic link from a new country (PHASE 9.9 carry-forward — needs
  GeoIP infrastructure not currently wired).

Implementation notes:
- Best-effort: catches every exception the email backend might raise
  so a dead SMTP host can't break the request that triggered the
  alert.
- Discovers the staff recipient list at call time (`User.objects.filter(
  is_staff=True, is_active=True)`) so newly-promoted staff start
  getting alerts without a config change.
- Uses `mail_admins` when `ADMINS` is configured (Django convention)
  AND falls back to per-staff `send_mail` otherwise. `ADMINS` is
  the right primary surface for ops-tier alerts; the per-staff
  fallback covers small deployments where `ADMINS` is unset.
"""
from __future__ import annotations

import logging

from django.core.mail import mail_admins, send_mail

log = logging.getLogger(__name__)


def notify_staff(subject: str, body: str) -> None:
    """Best-effort send of a security alert to the staff list.

    Subject prefix is added at send time (Django's `mail_admins` does
    its own EMAIL_SUBJECT_PREFIX prepending). Body kept plain-text —
    HTML alerts read worse in Slack-email-bridge / pager pipes.
    """
    subject = (subject or "")[:200] or "Security alert"
    body = body or ""

    try:
        from django.conf import settings as _settings

        admins = getattr(_settings, "ADMINS", []) or []
        if admins:
            mail_admins(subject, body, fail_silently=True)
            return
    except Exception:
        log.exception("notify_staff: mail_admins path failed")

    try:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        recipients = list(
            User.objects.filter(is_staff=True, is_active=True)
            .values_list("email", flat=True)
        )
        recipients = [r for r in recipients if r]
    except Exception:
        log.exception("notify_staff: staff recipient lookup failed")
        return

    if not recipients:
        log.info("notify_staff: no staff recipients configured; alert dropped")
        return

    try:
        from_email = getattr(
            __import__("django.conf", fromlist=["settings"]).settings,
            "DEFAULT_FROM_EMAIL",
            "noreply@example.com",
        )
        send_mail(
            subject,
            body,
            from_email,
            recipients,
            fail_silently=True,
        )
    except Exception:
        log.exception("notify_staff: send_mail raised")


__all__ = ["notify_staff"]
