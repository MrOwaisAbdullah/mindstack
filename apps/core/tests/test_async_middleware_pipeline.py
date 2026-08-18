"""Every middleware in the chain must be async-capable — no exceptions.

Django adapts per middleware: ONE sync-only middleware forces the whole
chain inside it through `async_to_sync`. This stack is async-only by
design (every custom middleware declares `sync_capable = False`), and
two stragglers — upstream `whitenoise.middleware.WhiteNoiseMiddleware`
and `SetupRequiredMiddleware` — were quietly wrapping the entire inner
stack: four sync/async boundary crossings and two pinned threadpool
threads per request. `assemble-context` at 5–30 s plus the 4 s
`activity.json` poll saturates that pool silently — the exact pathology
`log_scrub.py` documents at the outer edge, reintroduced mid-chain.

The structural sweep is the regression pin: a future `pip install
some-django-middleware` pasted into MIDDLEWARE without an async check
fails here by name.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, override_settings
from django.utils.module_loading import import_string

from apps.core.middleware import AsyncWhiteNoiseMiddleware

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_installed_middleware_is_async_capable() -> None:
    for dotted in settings.MIDDLEWARE:
        cls = import_string(dotted)
        assert getattr(cls, "async_capable", False) is True, (
            f"{dotted} is sync-only — Django will wrap the entire chain "
            f"inside it in async_to_sync, pinning threads on every request"
        )


# ---- the whitenoise adapter still serves files ----------------------------


@override_settings(WHITENOISE_AUTOREFRESH=False, STATIC_ROOT=str(_REPO_ROOT / "static"))
def test_adapter_serves_a_real_static_file() -> None:
    async def never(request):  # the file must come from the manifest
        raise AssertionError("request fell through to the app")

    mw = AsyncWhiteNoiseMiddleware(never)
    request = RequestFactory().get("/static/css/tw.css")
    response = async_to_sync(mw)(request)
    assert response.status_code == 200
    assert "text/css" in response.headers["Content-Type"]


@override_settings(WHITENOISE_AUTOREFRESH=False, STATIC_ROOT=str(_REPO_ROOT / "static"))
def test_adapter_passes_everything_else_through() -> None:
    seen = {}

    async def app(request):
        seen["path"] = request.path
        from django.http import HttpResponse

        return HttpResponse("app")

    mw = AsyncWhiteNoiseMiddleware(app)
    response = async_to_sync(mw)(RequestFactory().get("/api/v1/ping"))
    assert response.content == b"app"
    assert seen["path"] == "/api/v1/ping"


# ---- the setup redirect still redirects, now natively async ---------------


@pytest.mark.django_db
def test_setup_redirect_survives_the_async_rewrite(client) -> None:
    """End-to-end through the real chain: an operator on an incomplete
    install still bounces to the wizard, and the settings exemption from
    the lockout fix still holds."""
    user = User.objects.create_user(
        "async-mw-op", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(user)
    response = client.get("/ops/")
    assert response.status_code == 302
    assert response.url.startswith("/setup/")
    assert client.get("/ops/settings/").status_code == 200
