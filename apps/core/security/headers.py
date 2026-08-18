"""Security-headers middleware + per-route CSP relax decorator.

Django's stock `SecurityMiddleware` already covers HSTS,
`X-Content-Type-Options: nosniff`, `Referrer-Policy`, and
`Cross-Origin-Opener-Policy`; `XFrameOptionsMiddleware` covers
`X-Frame-Options`. This middleware adds what Django doesn't:

- `Content-Security-Policy` (or `Content-Security-Policy-Report-Only`
  when `CSP_REPORT_ONLY=True` — dev default; prod enforces).
- `Permissions-Policy` denying camera / microphone / geolocation by
  default (the surface a browser exposes to a hijacked tab).

Per-route relaxation: views that legitimately need a wider CSP (the
docs Try-it panel in Phase 10.4 needs `connect-src` open to user-
provided URLs, for example) wrap their view function with
`@relax_csp(connect_src="'self' https://*", ...)`. The decorator
attaches a dict to the response which the middleware merges into
the default directives before stamping the header.

Why a custom middleware instead of django-csp:
- One less dep, one less migration footprint.
- The relax decorator is application-specific (we want overrides on
  the Try-it panel, not at install-time).
- The CSP report-uri is wired to our own `_csp-report` view that
  writes ErrorLog rows so violations show up next to view crashes
  in the admin Errors page.
"""
from __future__ import annotations

import functools
import secrets
from typing import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest, HttpResponse

# Default CSP directives. `script-src` + `style-src` now
# carry a placeholder `__CSP_NONCE__` which the middleware substitutes for
# a per-request nonce before stamping the header. The matching
# `{% csp_nonce %}` template tag echoes the same value onto every inline
# `<script>` / `<style>` block, so the browser only executes inline code
# the server intentionally produced.
#
# `'unsafe-eval'` remains on `script-src` because Alpine 3 evaluates
# `x-data` expressions via `new Function()` — Alpine's docs explicitly
# call this out as required. (The Alpine "CSP build" exists but drops
# inline expressions, which would break the dashboard's templates.)
#
# `'self'` lets the vendored Alpine + HTMX `<script src=...>` tags load.
_NONCE_PLACEHOLDER = "__CSP_NONCE__"

_DEFAULT_DIRECTIVES: dict[str, str] = {
    "default-src": "'self'",
    "script-src": f"'self' 'nonce-{_NONCE_PLACEHOLDER}' 'unsafe-eval'",
    "style-src": f"'self' 'nonce-{_NONCE_PLACEHOLDER}'",
    "img-src": "'self' data:",
    "font-src": "'self' data:",
    "connect-src": "'self'",
    "frame-ancestors": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
    "object-src": "'none'",
}


# Google Analytics 4 host allowlist, merged into the CSP only when
# `settings.GOOGLE_ANALYTICS_ID` is set. gtag.js is served from
# googletagmanager.com; the collect/beacon endpoints live under
# google-analytics.com (+ regional `region1.` / `*.analytics.google.com`
# shards). When GA is off (blank ID) none of these are added and the CSP
# stays as tight as it ships.
_GA_CSP_SOURCES: dict[str, str] = {
    "script-src": "https://www.googletagmanager.com",
    "img-src": "https://www.google-analytics.com https://www.googletagmanager.com",
    "connect-src": (
        "https://www.google-analytics.com https://*.google-analytics.com "
        "https://*.analytics.google.com https://www.googletagmanager.com"
    ),
}


def _augment_for_google_analytics(merged: dict[str, str]) -> dict[str, str]:
    """Append the GA host sources to `merged` in place when GA is configured.

    No-op when `settings.GOOGLE_ANALYTICS_ID` is blank/unset, so a deploy
    without analytics never advertises Google hosts in its CSP. Sources are
    appended (not replaced) so any per-view `relax_csp` override still wins
    its base directive and just gains the GA hosts alongside.
    """
    if not getattr(settings, "GOOGLE_ANALYTICS_ID", ""):
        return merged
    for directive, sources in _GA_CSP_SOURCES.items():
        current = merged.get(directive, "'self'")
        merged[directive] = f"{current} {sources}"
    return merged


def _generate_nonce() -> str:
    """16 bytes of entropy → URL-safe base64. CSP nonces only need to be
    unique per response + unguessable — 128 bits gets both.
    """
    return secrets.token_urlsafe(16)

_PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
    "interest-cohort=(), browsing-topics=()"
)

# Attribute the `relax_csp` decorator stamps onto the response object
# so the middleware can read overrides without a request-scoped variable.
_CSP_OVERRIDES_ATTR = "_csp_overrides"


def _directive_key(name: str) -> str:
    """Translate the kwarg-style `script_src` to the wire-format
    `script-src`."""
    return name.replace("_", "-")


def build_csp_header_value(
    overrides: dict[str, str] | None = None,
    *,
    report_uri: str | None = None,
    nonce: str | None = None,
) -> str:
    """Compose a CSP header value from `_DEFAULT_DIRECTIVES` + caller overrides.

    `overrides` maps wire-format directive names (`script-src`) to their
    full source-list strings. A `None` value removes a directive.
    `report_uri` appends `report-uri <path>` if set.
    `nonce`: substituted for the `__CSP_NONCE__`
    placeholder in `script-src` + `style-src` so the response's CSP
    header advertises a fresh nonce that matches the one the template
    tag stamped on every inline `<script>` / `<style>` block. When
    None, the placeholder is left in the header — harmless, but the
    browser will reject every inline tag because no `nonce-*` source
    will match the literal `__CSP_NONCE__` placeholder. Production
    callers always pass a real nonce; the placeholder default exists
    only so unit tests that don't care about nonce values can call
    `build_csp_header_value()` with the old shape.
    """
    merged = dict(_DEFAULT_DIRECTIVES)
    if overrides:
        for key, value in overrides.items():
            wire = _directive_key(key)
            if value is None:
                merged.pop(wire, None)
            else:
                merged[wire] = value
    _augment_for_google_analytics(merged)
    if nonce is not None:
        merged = {k: v.replace(_NONCE_PLACEHOLDER, nonce) for k, v in merged.items()}
    parts = [f"{k} {v}" for k, v in merged.items()]
    if report_uri:
        parts.append(f"report-uri {report_uri}")
    return "; ".join(parts)


def relax_csp(**overrides: str) -> Callable[[Callable], Callable]:
    """Per-view CSP relaxation. Kwargs use underscored names that map to
    wire-format directives (`script_src` → `script-src`). Pass an empty
    string to disable a directive entirely (rarely the right call —
    prefer narrowing).

    Works on both sync and async views; the wrapper detects the inner
    callable's coroutine-ness so async endpoint views don't lose their
    awaitable shape.
    """

    def decorator(view: Callable) -> Callable:
        if iscoroutinefunction(view):

            @functools.wraps(view)
            async def awrapper(request, *args, **kwargs):  # type: ignore[no-untyped-def]
                response = await view(request, *args, **kwargs)
                _stamp_overrides(response, overrides)
                return response

            return awrapper

        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):  # type: ignore[no-untyped-def]
            response = view(request, *args, **kwargs)
            _stamp_overrides(response, overrides)
            return response

        return wrapper

    return decorator


def _stamp_overrides(response: HttpResponse, overrides: dict[str, str]) -> None:
    """Merge fresh overrides onto a possibly-already-decorated response.

    A view chain that nests `@relax_csp` (rare) gets last-decorator-wins
    semantics for any colliding directive — same as how the language's
    decorator order resolves attribute reads.
    """
    existing: dict[str, str] = getattr(response, _CSP_OVERRIDES_ATTR, None) or {}
    merged = {**existing, **{_directive_key(k): v for k, v in overrides.items()}}
    setattr(response, _CSP_OVERRIDES_ATTR, merged)


class SecurityHeadersMiddleware:
    """Async-only. Adds CSP + Permissions-Policy to every response.

    Headers are only ever set on responses — this middleware doesn't
    touch the request side. Sits in the middleware chain after Django's
    `SecurityMiddleware` (so HSTS / nosniff / COOP / Referrer-Policy
    have already been written by Django) but before `WhiteNoiseMiddleware`
    (so static-file responses also get CSP).
    """

    sync_capable = False
    async_capable = True

    def __init__(
        self,
        get_response: Callable[[HttpRequest], Awaitable[HttpResponse]],
    ) -> None:
        self.get_response = get_response
        markcoroutinefunction(self)
        if not iscoroutinefunction(get_response):
            raise RuntimeError(
                "SecurityHeadersMiddleware requires the async ASGI stack."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        # mint a per-request CSP nonce BEFORE running
        # downstream middleware + the view. The context processor reads
        # it off the request to expose `{{ csp_nonce }}` to every template,
        # and `{% csp_nonce %}` echoes it on inline `<script>` / `<style>`
        # blocks. The response-side `_apply_headers` then substitutes
        # the same value into the CSP header so the browser sees a
        # consistent (nonce-XXX) source on script-src + style-src.
        #
        # `request` is None in the few unit tests that exercise the
        # middleware in isolation; guard the attribute write but still
        # mint a nonce so the response header is still well-formed.
        nonce = _generate_nonce()
        if request is not None:
            try:
                request.csp_nonce = nonce  # type: ignore[attr-defined]
            except AttributeError:
                # Some test fakes / proxy objects may not accept new
                # attrs; the nonce in the header still works for the
                # response-side check.
                pass
        response = await self.get_response(request)
        self._apply_headers(response, nonce)
        return response

    def _apply_headers(self, response: HttpResponse, nonce: str) -> None:
        # CSP — pull per-view overrides off the response, fall back to defaults.
        overrides = getattr(response, _CSP_OVERRIDES_ATTR, None)
        report_uri = getattr(settings, "CSP_REPORT_URI", "/_csp-report/")
        header_value = build_csp_header_value(
            overrides, report_uri=report_uri, nonce=nonce
        )
        report_only = bool(getattr(settings, "CSP_REPORT_ONLY", False))
        header_name = (
            "Content-Security-Policy-Report-Only"
            if report_only
            else "Content-Security-Policy"
        )
        # Don't overwrite a header an inner layer already set deliberately.
        # (No layer does today, but it's cheaper than chasing a regression.)
        if header_name not in response.headers:
            response.headers[header_name] = header_value

        if "Permissions-Policy" not in response.headers:
            response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY


__all__ = [
    "SecurityHeadersMiddleware",
    "build_csp_header_value",
    "relax_csp",
]
