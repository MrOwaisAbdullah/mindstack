"""Dev-only direct-login shortcut — shared gate + session contract.

The `/auth/dev-login/` view (apps/accounts/views.py::dev_login_view) signs a
builder straight in as a staff superuser and stamps a session marker so the
staff-2FA gate (`apps.core.security.admin_required`) waves them through without
a TOTP device. This module holds the two pieces both sides need:

  - `is_active()` — the feature is live ONLY when DEBUG *and* DEV_LOGIN_ENABLED
    are both on. Either alone is inert.
  - the session-key constant + `bypass_2fa()` — so a session minted by the
    shortcut (and ONLY such a session) skips the TOTP challenge.

It lives in `apps.core.security` (not `apps.accounts`) so `admin_required`
can read the gate without `apps.core` taking a reverse dependency on a feature
app (Contract 1). `apps.accounts` imports DOWN into core, which is allowed.

Safety posture — three independent layers, all fail-closed:
  1. `Settings.assert_prod_safe()` refuses to boot prod when the env flag is
     set (the load-bearing guard — runs from `config/settings/prod.py`).
  2. `is_active()` additionally requires `DEBUG`, so even a non-prod settings
     module without the flag scrubbed won't expose it unless DEBUG is on.
  3. The view 404s when `is_active()` is False — the route is indistinguishable
     from an unmounted one.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest

# Stamped on the session by `dev_login_view`; `admin_required` reads it to skip
# the staff-2FA gate for THAT session only (not every staff session in dev).
DEV_LOGIN_2FA_BYPASS_SESSION_KEY = "dev_login_2fa_bypass"


def is_active() -> bool:
    """True only when BOTH `DEBUG` and `DEV_LOGIN_ENABLED` are on."""
    return bool(getattr(settings, "DEBUG", False)) and bool(
        getattr(settings, "DEV_LOGIN_ENABLED", False)
    )


def bypass_2fa(request: HttpRequest) -> bool:
    """True when this session was minted by the dev-login shortcut AND the
    feature is active. `admin_required` short-circuits the TOTP gate on this."""
    return is_active() and bool(
        request.session.get(DEV_LOGIN_2FA_BYPASS_SESSION_KEY)
    )


__all__ = ["DEV_LOGIN_2FA_BYPASS_SESSION_KEY", "is_active", "bypass_2fa"]
