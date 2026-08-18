"""Public security surface + branded error views +
SEO surface (robots.txt + sitemap.xml).

Three security endpoints:

- `/.well-known/security.txt` (RFC 9116) — published security contact +
  disclosure policy. Returns 404 when `SECURITY_TXT_CONTACT` is unset
  rather than shipping a placeholder; we don't pretend we have a contact
  channel until the operator wires one up.
- `/_csp-report/` — receives `Content-Security-Policy` violation reports
  from browsers. Each report writes one ErrorLog row with
  `exc_class="CSPViolation"` so violations surface in the same admin
  Errors panel that view crashes do, with the same filtering / triage
  affordances. The endpoint is unauthenticated (browsers POST without
  cookies in some configurations) but rate-limited at the CDN/edge in
  prod via the same path-prefix rules as `/healthz`.
- `/robots.txt` + `/sitemap.xml` — Phase 12.47. Allow indexing of public
  surfaces (landing, privacy, docs catalog, docs detail per registered
  endpoint, docs guides); disallow indexing of operator-only surfaces
  (`/admin/`, `/dashboard/`, `/api/`, `/mcp/`, OAuth + auth flows).

Plus the branded error views wired as Django's `handler404` / `handler500`
in `config/urls.py`. They render `templates/errors/{404,500}.html` with
the request_id (RequestIdMiddleware bound it before the view ran) so
support has something to grep for.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.core import error_hook

log = logging.getLogger(__name__)


@require_http_methods(["GET"])
def security_txt(_request: HttpRequest) -> HttpResponse:
    """RFC 9116 security.txt. Operator-configured."""
    contact = getattr(settings, "SECURITY_TXT_CONTACT", "") or ""
    if not contact:
        return HttpResponse(status=404)

    policy_url = getattr(settings, "SECURITY_TXT_POLICY_URL", "") or ""
    encryption_url = getattr(settings, "SECURITY_TXT_ENCRYPTION_URL", "") or ""
    expires_days = int(getattr(settings, "SECURITY_TXT_EXPIRES_DAYS", 365) or 365)
    expires_at = (timezone.now() + timedelta(days=expires_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    lines = [
        f"Contact: {contact}",
        f"Expires: {expires_at}",
        "Preferred-Languages: en",
    ]
    if policy_url:
        lines.append(f"Policy: {policy_url}")
    if encryption_url:
        lines.append(f"Encryption: {encryption_url}")

    body = "\n".join(lines) + "\n"
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


class CSPViolation(Exception):
    """Synthetic exception carrying the violation report. Routed through
    `record_error` so the row lands next to view crashes in the admin
    Errors panel."""


@csrf_exempt
@require_http_methods(["POST"])
def csp_report(request: HttpRequest) -> HttpResponse:
    """Receive `report-uri` POST from a browser CSP-violation event.

    Body shape: `{"csp-report": {"document-uri": ..., "violated-directive": ...}}`.
    The chrome/firefox content-type is `application/csp-report` (not JSON
    proper). We accept either.
    """
    raw = request.body.decode("utf-8", errors="replace") if request.body else ""
    payload: dict = {}
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("csp_report: payload not valid JSON; storing as raw text")

    report = payload.get("csp-report") if isinstance(payload, dict) else None
    if not isinstance(report, dict):
        report = {}

    directive = (
        report.get("effective-directive")
        or report.get("violated-directive")
        or "unknown"
    )[:200]
    blocked = (report.get("blocked-uri") or "")[:300]
    document = (report.get("document-uri") or "")[:300]
    # Capture source-file / line / column / sample so triage can pinpoint
    # the offending element — without these, every CSPViolation row reads
    # the same and you can't tell HTMX's indicator style apart from a
    # downstream library's runtime inline-style injection.
    source_file = (report.get("source-file") or "")[:300]
    line_no = report.get("line-number") or ""
    col_no = report.get("column-number") or ""
    sample = (report.get("script-sample") or "")[:200]
    message = (
        f"directive={directive} blocked={blocked} document={document}"
        f" source={source_file}:{line_no}:{col_no} sample={sample!r}"
    )
    summary = message[:512]

    exc = CSPViolation(summary)
    request_id = getattr(request, "request_id", "") or ""
    user_id = None
    principal = getattr(request, "_principal", None)
    if principal is not None and getattr(principal, "user", None) is not None:
        user_id = getattr(principal.user, "pk", None)

    try:
        error_hook.record_error(
            exc=exc,
            request_id=request_id,
            source="system",
            endpoint_slug="csp_violation",
            request_path=document or request.path,
            request_method="POST",
            status_code=204,
            user_id=user_id,
            ip=request.META.get("REMOTE_ADDR"),
            user_agent=request.headers.get("User-Agent", ""),
            handled=True,
        )
    except Exception:
        # Defensive — never surface an internal failure on the CSP
        # report endpoint. A misbehaving browser firing a report storm
        # should not break the page that triggered it.
        log.exception("csp_report: record_error itself raised")

    # 204 No Content is the canonical response — browsers ignore the
    # body either way.
    return HttpResponse(status=204)


# ---- branded error views ------------------------------------


def _render_error(
    request: HttpRequest,
    *,
    template: str,
    status: int,
) -> HttpResponse:
    """Shared render path for handler404/handler500.

    Reads `request.request_id` (RequestIdMiddleware bound it) so the
    error page can show it for support hand-offs. Wrapped in a broad
    try/except because a handler500 path that itself raises produces
    Django's default 500 page — and we want the branded one to render
    even when the database / cache / template loader is the thing
    that's broken.
    """
    # Local import: apps/core is the vendored framework and must not gain a
    # module-level dependency on a brain app.
    from apps.brainconfig import services as brainconfig_services

    request_id = getattr(request, "request_id", "") or ""
    try:
        body = render_to_string(
            template,
            {
                # Never raises — falls back to the env value if the DB is
                # the thing that produced this 500 in the first place.
                "APP_NAME": brainconfig_services.app_name(),
                "request_id": request_id,
                "STATUS_PAGE_URL": getattr(settings, "STATUS_PAGE_URL", "") or "",
                "SUPPORT_EMAIL": getattr(settings, "SUPPORT_EMAIL", "") or "",
            },
            request=request,
        )
    except Exception:
        # Last-resort plain-text fallback so the user still sees
        # something coherent if templates themselves are broken.
        log.exception("error view: template render failed for %s", template)
        body = (
            f"{status}\n\n"
            f"Reference: {request_id}\n" if request_id else f"{status}\n"
        )
        return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")
    return HttpResponse(body, status=status, content_type="text/html; charset=utf-8")


def error_404(request: HttpRequest, exception=None) -> HttpResponse:
    """Branded 404 page. Wired as `handler404`."""
    return _render_error(request, template="errors/404.html", status=404)


def error_500(request: HttpRequest) -> HttpResponse:
    """Branded 500 page. Wired as `handler500`."""
    return _render_error(request, template="errors/500.html", status=500)


# ---- SEO surface (robots.txt + sitemap.xml) ------------------


def _site_origin(request: HttpRequest) -> str:
    """Build the absolute origin for sitemap URLs.

    Prefers `SITE_URL` from settings (operator sets to the canonical
    https origin in prod); falls back to the request's scheme+host so
    dev still works without configuration."""
    site_url = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if site_url:
        return site_url
    return f"{request.scheme}://{request.get_host()}"


@require_http_methods(["GET"])
def robots_txt(request: HttpRequest) -> HttpResponse:
    """RFC-style robots.txt. Allow public surfaces; disallow operator-only
    paths. Points crawlers at sitemap.xml so they can pull the full list
    of indexable pages without guessing."""
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /mcp/",
        "Disallow: /dashboard/",
        "Disallow: /oauth/",
        "Disallow: /auth/",
        "Disallow: /accounts/",
    ]
    admin_path = (settings.ADMIN_PANEL_URL_PATH or "").strip("/")
    if admin_path:
        lines.append(f"Disallow: /{admin_path}/")
    django_admin_path = (settings.DJANGO_ADMIN_URL_PATH or "").strip("/")
    if django_admin_path:
        lines.append(f"Disallow: /{django_admin_path}/")
    lines.append("")
    lines.append(f"Sitemap: {_site_origin(request)}/sitemap.xml")
    body = "\n".join(lines) + "\n"
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@require_http_methods(["GET"])
def sitemap_xml(request: HttpRequest) -> HttpResponse:
    """Sitemaps-protocol XML covering every public surface a search engine
    should know about: landing, privacy, docs catalog, one entry per
    registered @endpoint detail page, one entry per markdown guide.

    Operator-only surfaces (admin / dashboard / API / MCP / auth flows)
    are intentionally excluded — they're disallowed in robots.txt and
    have no public value to index."""
    # Late import for the registry (apps.core.registry — same package).
    # Guide slugs come from filesystem walk rather than an `apps.docs`
    # Python import: Contract 1 forbids apps.core from importing any
    # feature app, including apps.docs. Disk discovery is content-only
    # and stays inside the boundary.
    from pathlib import Path as _Path

    from apps.core.registry import registry

    origin = _site_origin(request)
    today = timezone.now().date().isoformat()

    urls: list[tuple[str, str, str]] = [
        (f"{origin}/", "weekly", "1.0"),
        (f"{origin}/privacy/", "monthly", "0.3"),
        (f"{origin}/docs/", "weekly", "0.8"),
    ]
    try:
        for spec in registry.all():
            urls.append((f"{origin}/docs/{spec.slug}/", "weekly", "0.6"))
    except Exception:
        log.exception("sitemap_xml: registry.all() failed")
    try:
        guides_dir = _Path(settings.BASE_DIR) / "apps" / "docs" / "guides"
        if guides_dir.is_dir():
            for guide_path in sorted(guides_dir.glob("*.md")):
                urls.append(
                    (f"{origin}/docs/guide/{guide_path.stem}/", "monthly", "0.5")
                )
    except Exception:
        log.exception("sitemap_xml: guides walk failed")

    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, changefreq, priority in urls:
        out.append("  <url>")
        out.append(f"    <loc>{loc}</loc>")
        out.append(f"    <lastmod>{today}</lastmod>")
        out.append(f"    <changefreq>{changefreq}</changefreq>")
        out.append(f"    <priority>{priority}</priority>")
        out.append("  </url>")
    out.append("</urlset>")
    body = "\n".join(out) + "\n"
    return HttpResponse(body, content_type="application/xml; charset=utf-8")
