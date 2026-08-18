"""Ops UI: `/ops/consumers/` — the keys that let agents read this brain.

Until this page existed there was no way to create a credential at all
short of a Django shell, and `apps.core.rest` 401s every unauthenticated
call (no anonymous tier, deliberately — PLAN.md §5). So a fresh install
could not reach its own API. This is that gap closed.

Keys belong to the operator's own user: single-user, single-brain. A
"consumer" here is a *client* — Claude Desktop, claude.ai, a script —
not another person.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.api_keys import api as api_keys
from apps.brainconfig.nav import ops_context
from apps.mind import consumers
from apps.mind.models import DEFAULT_RATE_LIMIT_PER_MIN


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def consumers_page(request):
    if request.method == "POST":
        _act(request)
        return redirect(request.path)

    all_rows = consumers.rows(request.user)
    # Live keys first, dead ones sink. Rotation revokes a row every time
    # it runs, so the revoked pile grows for the life of the install and
    # would otherwise interleave with the keys you can still act on.
    # list.sort is stable, so newest-first (from the API layer) survives
    # within each band.
    _rank = {"active": 0, "expired": 1, "revoked": 2}
    all_rows.sort(key=lambda r: _rank.get(r["state"], 3))
    page_obj = Paginator(all_rows, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "ops/consumers.html",
        {
            "rows": page_obj.object_list,
            "page_obj": page_obj,
            "total": len(all_rows),
            "active_count": sum(1 for r in all_rows if r["state"] == "active"),
            # Paired with the prose here rather than looked up in the
            # template: Django has no dict-by-key lookup without a custom
            # filter, and a filter is not worth writing for three strings.
            "tiers": [
                {"value": t, "help": consumers.TIER_HELP[t]} for t in consumers.TIERS
            ],
            "default_rate_limit": DEFAULT_RATE_LIMIT_PER_MIN,
            "min_rate_limit": consumers.MIN_RATE_LIMIT,
            "max_rate_limit": consumers.MAX_RATE_LIMIT,
            # Shown exactly once, then gone: the plaintext is never stored
            # server-side (only its SHA-256), so a refresh cannot get it
            # back and the page has to say so plainly.
            "new_secret": request.session.pop("new_consumer_secret", None),
            "api_base": request.build_absolute_uri("/api/v1/"),
            "mcp_url": request.build_absolute_uri("/mcp"),
            **ops_context(request),
        },
    )


def _act(request) -> None:
    action = request.POST.get("action", "")

    try:
        if action == "create":
            generated = consumers.create(
                request.user,
                name=request.POST.get("name", ""),
                max_visibility=request.POST.get("tier", ""),
                rate_limit_per_min=request.POST.get("rate_limit", DEFAULT_RATE_LIMIT_PER_MIN),
                note=request.POST.get("note", ""),
            )
            request.session["new_consumer_secret"] = {
                "secret": generated.secret,
                "name": generated.api_key.name,
            }
            messages.success(
                request,
                f"Minted {generated.api_key.name} — copy the key below, it is shown once.",
            )
            return

        key = _key_or_none(request)
        if key is None:
            messages.error(request, "That key no longer exists.")
            return

        if action == "update":
            consumers.update_profile(
                key,
                max_visibility=request.POST.get("tier", ""),
                rate_limit_per_min=request.POST.get("rate_limit", DEFAULT_RATE_LIMIT_PER_MIN),
                note=request.POST.get("note", ""),
            )
            messages.success(request, f"Updated {key.name} — in effect on the next call.")

        elif action == "rotate":
            generated = consumers.rotate(key)
            request.session["new_consumer_secret"] = {
                "secret": generated.secret,
                "name": generated.api_key.name,
            }
            messages.warning(
                request,
                f"Rotated {key.name}. The old key stopped working now — "
                "swap it in your client before closing this page.",
            )

        elif action == "revoke":
            consumers.revoke(key)
            messages.success(request, f"Revoked {key.name}. Calls with it fail immediately.")

        else:
            messages.error(request, f"Unknown action {action!r}.")

    except consumers.InvalidConsumer as exc:
        messages.error(request, str(exc))


def _key_or_none(request):
    """Resolve `key_id` under the owner-only manager.

    `get_user_key` (not `.filter(pk=...)`) so a crafted id belonging to
    another user is indistinguishable from one that never existed.
    """
    try:
        key_id = int(request.POST.get("key_id", ""))
    except (TypeError, ValueError):
        return None
    return api_keys.get_user_key(request.user, key_id=key_id)
