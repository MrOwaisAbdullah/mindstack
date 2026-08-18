"""`@admin_required` decorator.

The single gate every staff-only view (custom admin panel in Phase 10.2,
ops-only management views, etc.) wraps. Three checks:

1. `request.user.is_authenticated` — anonymous → redirect to magic-link login.
2. `request.user.is_staff` — non-staff → 403.
3. Recently 2FA-verified — staff with no confirmed TOTP device → redirect
   to enrollment; staff with device but session timestamp older than
   `STAFF_2FA_TIMEOUT_HOURS` → redirect to verification with the
   originally-requested URL stashed in the session.

Why this lives in `apps.core.security` (not `apps.staff_2fa`):
- The decorator MUST be importable from any feature app's view layer
  WITHOUT pulling apps.staff_2fa into that app's import graph
  (Contract 1: apps.core has no reverse deps on feature apps; admin
  panel + dashboard only import from apps.core or via api.py
  surfaces). apps.staff_2fa is a feature app; apps.core.security
  knows about its URL names + session keys via stable string contracts
  rather than direct imports.
- The TOTPDevice query happens here too (django_otp is a third-party
  dep, not a feature app, so importing its model from apps.core is
  fine — it's effectively part of the framework layer).
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Callable

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

log = logging.getLogger(__name__)

_VERIFIED_AT_KEY = "staff_2fa_verified_at"
_NEXT_KEY = "staff_2fa_next"


def _has_confirmed_device(user: object) -> bool:
    """Lazily import `TOTPDevice` so loading apps.core at boot doesn't
    require django_otp to be in INSTALLED_APPS."""
    try:
        from django_otp.plugins.otp_totp.models import TOTPDevice
    except ImportError:
        log.warning("admin_required: django_otp not installed; failing closed")
        return False
    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def _verified_recently(request: HttpRequest) -> bool:
    raw = request.session.get(_VERIFIED_AT_KEY)
    if raw is None:
        return False
    try:
        verified_at = int(raw)
    except (TypeError, ValueError):
        return False
    timeout_h = int(getattr(settings, "STAFF_2FA_TIMEOUT_HOURS", 8) or 8)
    elapsed = int(time.time()) - verified_at
    return 0 <= elapsed <= timeout_h * 3600


def admin_required(view: Callable) -> Callable:
    """Decorator wrapping a view in the staff + 2FA gate.

    Sync only. Most admin/staff views are sync (Django admin, custom
    admin panel, dashboard); async staff views can wrap an inner sync
    function explicitly with the same gate logic if needed.
    """

    @functools.wraps(view)
    @login_required  # anonymous → /auth/login/?next=...
    def wrapper(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        user = request.user
        if not user.is_staff:
            raise PermissionDenied("Staff access required.")

        # Dev-only: a session minted by /auth/dev-login/ skips the TOTP gate so
        # a builder can reach the admin panel without enrolling a 2FA device.
        # Inert unless DEBUG + DEV_LOGIN_ENABLED are BOTH on (and prod refuses
        # to boot with the flag set) — see apps.core.security.dev_login.
        from apps.core.security.dev_login import bypass_2fa

        if bypass_2fa(request):
            return view(request, *args, **kwargs)

        if not _has_confirmed_device(user):
            # Stash the originally-requested URL so post-enrollment we
            # can bounce the user back where they were heading.
            request.session[_NEXT_KEY] = request.get_full_path()
            return redirect(reverse("staff_2fa:setup"))

        if not _verified_recently(request):
            request.session[_NEXT_KEY] = request.get_full_path()
            return redirect(reverse("staff_2fa:verify"))

        return view(request, *args, **kwargs)

    return wrapper


__all__ = ["admin_required"]
