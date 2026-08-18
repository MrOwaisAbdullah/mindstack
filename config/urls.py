"""Root URL routing — brain server.

Public surface: / (landing page, reveals nothing), /api/ (registry REST,
key-authed), /mcp (proxy → FastMCP subprocess, key-authed),
/mcp/k/<token> (same proxy, key in the path, off by default),
/webhooks/github (HMAC), /healthz, /readyz. Everything else — /docs/ and
the whole ops UI — is staff-session-only AND behind the IP-allowlist
perimeter in prod (nothing-public decision, 2026-07-28; PLAN.md §9).
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.db import connections
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import include, path

from apps.brain.views import github_webhook
from apps.core.views import csp_report, robots_txt, security_txt
from apps.mcp_proxy.views import mcp_proxy_view


def home(request):
    """Public landing page. Shows the name and a sign-in door, nothing
    else — no links that reveal URLs beyond the ops prefix (which the
    IP-allowlist perimeter 404s for outsiders in prod anyway)."""
    return render(request, "home.html")


def healthz(request):
    """Liveness: process is up, no external deps touched."""
    return HttpResponse("ok", content_type="text/plain")


def readyz(request):
    """Readiness: DB reachable + the RIGHT brain clone, with the contract.

    `origin_ok` is part of readiness, not a warning: a clone whose origin
    is not the configured repo answers every question from the wrong mind,
    which is worse than answering nothing.
    """
    from apps.brain.services import gitrepo

    checks: dict[str, object] = {}
    status = 200
    try:
        connections["default"].cursor().execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:  # pragma: no cover - failure path
        checks["db"] = f"fail: {exc.__class__.__name__}"
        status = 503
    brain = gitrepo.status_probe()
    if brain.get("valid") and brain.get("contract_ok") and brain.get("origin_ok", True):
        checks["brain"] = {"head": str(brain.get("head", ""))[:12]}
    else:
        checks["brain"] = brain
        status = 503
    return JsonResponse({"status": "ok" if status == 200 else "fail", **checks}, status=status)


urlpatterns: list = [
    path("", home, name="home"),
    # Branded login/logout (templates/login.html). Inside the
    # IP-allowlist perimeter in prod (IP_ALLOWLIST_EXTRA_PREFIXES).
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # First-run wizard. Top level, not under the ops prefix: it has to be
    # reachable before an operator account or a brain exists. Inside the
    # IP-allowlist perimeter in prod (IP_ALLOWLIST_EXTRA_PREFIXES).
    path("setup/", include("apps.brainconfig.setup_urls", namespace="setup")),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path(".well-known/security.txt", security_txt, name="security-txt"),
    path("robots.txt", robots_txt, name="robots-txt"),
    path("_csp-report/", csp_report, name="csp-report"),
    # Registry-driven REST: <version>/<slug> + _catalog/_openapi.json/_health.
    path("api/", include("apps.core.urls")),
    # Django proxy view → FastMCP loopback subprocess (bearer API-key auth).
    # Both slash forms: MCP clients POST without the trailing slash.
    path("mcp/", mcp_proxy_view, name="mcp"),
    path("mcp", mcp_proxy_view),
    # Same view, credential in the path instead of the header — the only
    # shape Claude.ai's custom connector (web + mobile) can authenticate
    # with, since its dialog has nowhere to put an Authorization header.
    # The view checks MCP_URL_AUTH_ENABLED itself and 404s when off, so
    # the surface is invisible rather than merely closed.
    path("mcp/k/<str:token>/", mcp_proxy_view, name="mcp-url-token"),
    path("mcp/k/<str:token>", mcp_proxy_view),
    # Docs site (registry-driven catalog + per-endpoint detail).
    # Staff-only; in prod also inside the IP-allowlist perimeter.
    path("docs/", include("apps.docs.urls", namespace="docs")),
    # GitHub push → sync (HMAC-verified, deduped, echo-guarded).
    path("webhooks/github", github_webhook, name="github-webhook"),
]

if settings.DJANGO_ADMIN_URL_PATH:
    urlpatterns.append(path(settings.DJANGO_ADMIN_URL_PATH, admin.site.urls))

# Ops UI (Settings now; dashboard/browser in M1.10). Mounted under the
# admin-panel prefix so AdminIPAllowlistMiddleware guards it; in prod the
# whole prefix also sits behind the network boundary (PLAN.md §9).
_ops_prefix = (settings.ADMIN_PANEL_URL_PATH or "ops/").strip("/") + "/"
urlpatterns.append(path(_ops_prefix, include("apps.brainconfig.urls")))

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")

handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"
