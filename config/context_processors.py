"""Template context processors that inject project-wide values."""
from django.conf import settings
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse


def app_meta(request: HttpRequest) -> dict[str, object]:
    """Expose `APP_NAME` (and later DEBUG / version) to every template."""
    return {
        # Where the sidebar/topbar wordmark points. Both partials had
        # `/docs/` hardcoded, so clicking the brand inside the ops UI
        # navigated OUT to the public API reference.
        "brand_url": _brand_url(request),
        # Operator-editable on /ops/settings/, falling back to the env
        # value. `app_name()` swallows DB errors, so a page still renders
        # branded when the database is what's broken.
        "APP_NAME": _app_name(),
        # used by the magic-link login template to decide which
        # social buttons to render. Empty dict → no social row at all.
        "social_providers": list(getattr(settings, "SOCIALACCOUNT_PROVIDERS", {}).keys()),
        # Public GA4 measurement ID — empty string when analytics is off.
        # `partials/_analytics.html` (gtag bootstrap) and
        # `partials/_cookie_consent.html` (banner) both gate on this being
        # truthy, so a blank ID renders neither.
        "GOOGLE_ANALYTICS_ID": getattr(settings, "GOOGLE_ANALYTICS_ID", ""),
        # True only when the dev-login shortcut is live (DEBUG +
        # DEV_LOGIN_ENABLED). The login page renders a one-click
        # "Dev login (skip 2FA)" button when set; always False in prod.
        "dev_login_enabled": _dev_login_enabled(),
    }


def _brand_url(request: HttpRequest) -> str:
    """The brand's destination: the ops dashboard for the operator.

    Staff-gated for the same reason the docs "Server" pivot is: /ops/ is
    session-authed, so pointing a public reader's logo at it would send
    them to a login page. Anonymous visitors keep the docs home, which
    is where the hardcoded link went before — no regression for them.

    Reverse failures fall back rather than 500ing: this runs on EVERY
    template render, including the error pages, where a broken urlconf
    is a plausible reason we got there at all. `request.user` is absent
    when auth middleware hasn't run (hand-built requests in tests).
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_staff:
        try:
            return reverse("brainconfig:dashboard")
        except NoReverseMatch:
            pass
    try:
        return reverse("docs:index")
    except NoReverseMatch:
        return "/"


def _app_name() -> str:
    # Local import, like `_dev_login_enabled` below: this module is named
    # by string in TEMPLATES, and brainconfig reaches models.
    from apps.brainconfig.services import app_name

    return app_name()


def _dev_login_enabled() -> bool:
    # Local import keeps the context processor importable even if the helper
    # module is ever relocated; mirrors the DEBUG-AND-flag gate the view uses.
    from apps.core.security.dev_login import is_active

    return is_active()


def csp_nonce(request: HttpRequest) -> dict[str, object]:
    """Expose the per-request CSP nonce to every template.

    The nonce is minted by `SecurityHeadersMiddleware` BEFORE the view runs
    and stamped on `request.csp_nonce`. Templates echo it onto inline
    `<script nonce="{{ csp_nonce }}">` and `<style nonce="{{ csp_nonce }}">`
    blocks (or via `{% csp_nonce %}`) so the browser only executes inline
    code the server intentionally produced. The middleware substitutes the
    same value into the response's CSP `script-src` / `style-src` source
    list so the header and the tags agree.

    Falls back to "" when the middleware hasn't run (e.g. an inner test
    that crafts a Request manually) — templates render fine; the nonce
    just doesn't match anything in the CSP header for that response.
    """
    return {"csp_nonce": getattr(request, "csp_nonce", "")}
