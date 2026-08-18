"""Auto-built URLconf from the endpoint registry (ETag).

Mounted under `/api/` by `config/urls.py`. For every (version, slug) in the
registry we register `<version>/<slug>` → `make_endpoint_view(spec)`. For
each version present we also register the three built-in routes:

- `<version>/_catalog`       — every endpoint in this version
- `<version>/_openapi.json`  — OpenAPI 3.1 doc for this version
- `<version>/_health`        — minimal liveness check

Phase 7.5 promotes `_health` to richer `/healthz` + `/readyz` endpoints.
Every call through `make_endpoint_view` fires `EndpointCalled` (there is
no RequestLogMiddleware in this fork — the view itself is the fire site).

`_openapi.json` and `_catalog` set an ETag header so
`django.middleware.http.ConditionalGetMiddleware` short-circuits
`If-None-Match` re-requests to 304 (Not Modified). Both bodies are pure
functions of the in-process registry which never changes after boot, so
the ETag is the SHA-256 of the body computed once per process.
"""
from __future__ import annotations

import hashlib
import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import URLPattern, path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from apps.core.openapi import build_openapi
from apps.core.registry import registry
from apps.core.rest import make_endpoint_view


def _etag_for(body: str) -> str:
    """Strong ETag (RFC 7232 §2.3) — quoted ASCII hex digest."""
    return '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] + '"'


def _build_urlpatterns() -> list[URLPattern]:
    patterns: list[URLPattern] = []
    versions = sorted({s.version for s in registry.all()}) or ["v1"]

    for version in versions:
        patterns.extend(_builtin_routes(version))

    for spec in registry.all():
        view = make_endpoint_view(spec)
        patterns.append(
            path(
                f"{spec.version}/{spec.slug}",
                view,
                name=f"endpoint_{spec.version}_{spec.slug}",
            )
        )
    return patterns


def _builtin_routes(version: str) -> list[URLPattern]:
    return [
        path(f"{version}/_catalog", _make_catalog_view(version), name=f"catalog_{version}"),
        path(
            f"{version}/_openapi.json",
            _make_openapi_view(version),
            name=f"openapi_{version}",
        ),
        path(f"{version}/_health", _health_view, name=f"health_{version}"),
        # `_jobs/<id>` is mounted from `config/urls.py`
        # against `apps.jobs.urls` rather than wired here, so apps.core
        # stays free of an `apps.jobs` import (Contract 1).
    ]


def _make_catalog_view(version: str):
    """ETag-tagged catalog, rendered once per process.

    The body used to vary by requester: admin-only endpoints were dropped
    for non-staff callers. That flag is gone (it could never fire in a
    single-operator product — see `apps.core.models.EndpointFlag`), so
    the catalog is again a pure function of the registry, which never
    changes after boot. `ConditionalGetMiddleware` flips a matching
    `If-None-Match` to 304."""
    cached: list[tuple[str, str]] = []

    @csrf_exempt
    @require_GET
    def view(_request: HttpRequest) -> HttpResponse:
        if not cached:
            entries = [
                s.to_catalog_entry() for s in registry.all() if s.version == version
            ]
            body = json.dumps(
                {"version": version, "count": len(entries), "endpoints": entries}
            )
            cached.append((body, _etag_for(body)))
        body, etag = cached[0]
        resp = HttpResponse(body, content_type="application/json", status=200)
        resp["ETag"] = etag
        return resp

    view.__name__ = f"catalog_{version}"
    return view


def _make_openapi_view(version: str):
    """cache the rendered OpenAPI body in-process forever
    (until the process restarts). The doc is a pure function of the
    registry, which only changes at startup, so the document only needs
    to be (re)built once per process.

    ETag set so `ConditionalGetMiddleware` short-circuits
    `If-None-Match` re-requests to 304. SDK generators / docs sites
    that re-fetch the OpenAPI doc on each load now pay one TCP round-
    trip instead of one body re-render."""
    cached: list[tuple[str, str]] = []

    @csrf_exempt
    @require_GET
    def view(_request: HttpRequest) -> HttpResponse:
        if not cached:
            # Inside the cache-miss branch on purpose: the title is
            # operator-editable on /ops/settings/, but reading it per
            # request would put a DB query back on the hot path this
            # cache exists to keep clear. Renames land on restart.
            from apps.brainconfig.services import app_name

            doc = build_openapi(version=version, title=app_name() or "API")
            body = json.dumps(doc)
            cached.append((body, _etag_for(body)))
        body, etag = cached[0]
        # Use HttpResponse + manual dumps so we control the content-type exactly
        # (some validators are picky about `application/openapi+json` vs json).
        resp = HttpResponse(body, content_type="application/json", status=200)
        resp["ETag"] = etag
        return resp

    view.__name__ = f"openapi_{version}"
    return view


@csrf_exempt
@require_GET
def _health_view(_request: HttpRequest) -> JsonResponse:
    # Phase 7.5 expands this with /healthz (liveness) + /readyz (DB/Redis/MCP probes).
    return JsonResponse({"status": "ok"})


urlpatterns = _build_urlpatterns()
