"""Inbound GitHub webhook — the push-to-server sync trigger.

Security posture (PLAN.md §4, grill C14/C19):
- HMAC SHA-256 via `hmac.compare_digest` (timing-safe); missing/invalid
  signature → 403, and an `auth_denied` Event.
- Dedupe on `X-GitHub-Delivery` (cache-backed) — a replayed delivery is
  acknowledged but does nothing.
- Echo guard: if the delivered head SHA already equals the local HEAD
  (our own approval-push webhooking back, grill C19), acknowledge
  without pulling — no lock contention window.
- The pull itself runs through the same single-writer lock as every
  other mutating git op (inside gitrepo.pull_rebase).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.brain.services import gitrepo, sync
from apps.events.models import emit

log = logging.getLogger(__name__)

_DELIVERY_CACHE_PREFIX = "gh-delivery:"
_DELIVERY_TTL = 60 * 60 * 24


def _webhook_secret() -> str:
    """Effective HMAC secret: env/compose wins, else the stored setting.

    Read per request, not at import. It used to come from
    `settings.GITHUB_WEBHOOK_SECRET`, which is resolved once at startup —
    so the webhook-secret field on the Settings page wrote a value that
    nothing ever read, and the wizard could not generate one without a
    restart.
    """
    try:
        from apps.brainconfig import services as cfg

        return cfg.get("GITHUB_WEBHOOK_SECRET").strip()
    except Exception:  # pragma: no cover - DB down
        log.warning("brain: webhook secret lookup failed", exc_info=True)
        return settings.GITHUB_WEBHOOK_SECRET


def _signature_ok(request: HttpRequest) -> bool:
    secret = _webhook_secret()
    if not secret:
        return False  # unset secret = webhook disabled, never open
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


@csrf_exempt
@require_POST
def github_webhook(request: HttpRequest) -> HttpResponse:
    if not _signature_ok(request):
        emit("auth_denied", surface="webhook", reason="bad or missing HMAC signature")
        return JsonResponse({"error": "invalid signature"}, status=403)

    delivery = request.headers.get("X-GitHub-Delivery", "")
    if delivery:
        cache_key = f"{_DELIVERY_CACHE_PREFIX}{delivery}"
        if cache.get(cache_key):
            return JsonResponse({"status": "duplicate delivery — ignored"})
        cache.set(cache_key, True, _DELIVERY_TTL)

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return JsonResponse({"status": "pong"})
    if event and event != "push":
        return JsonResponse({"status": f"event {event} ignored"})

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        payload = {}
    delivered_sha = str(payload.get("after", ""))

    if delivered_sha and gitrepo.is_valid_repo() and delivered_sha == gitrepo.head_sha():
        return JsonResponse({"status": "already at delivered SHA — no-op"})

    try:
        run = sync.sync(trigger="webhook")
    except (gitrepo.BrainRepoError, sync.SyncError) as exc:
        log.error("brain: webhook sync failed: %s", exc)
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse(
        {
            "status": "synced",
            "head": run.commit_sha[:12],
            "added": run.added,
            "changed": run.changed,
            "removed": run.removed,
            "drift": run.drift_detected,
        }
    )
