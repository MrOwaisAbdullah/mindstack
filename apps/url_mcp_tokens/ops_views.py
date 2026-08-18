"""Ops UI: `/ops/connectors/` — the URLs that reach this brain from
claude.ai.

Separate from `/ops/consumers/` because these are a different credential
with a different failure mode, and putting them on one page would blur
the distinction the operator most needs to keep: a bearer key is a secret
you hold, a connector URL is a secret you *paste into someone else's
product*. The mint flow therefore hands back a whole URL rather than a
token, because a URL is the thing that actually gets pasted, and the
copy affordance should hand over exactly what claude.ai's dialog wants.

`MCP_URL_AUTH_ENABLED` gates the surface itself. When it is off the proxy
404s `/mcp/k/...`, so a token minted here would authenticate against
nothing — the page says so at the top rather than letting the operator
discover it in a connector dialog that reports only "couldn't connect".
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.brainconfig.nav import ops_context
from apps.url_mcp_tokens import api as url_tokens


def connector_url(request, secret: str) -> str:
    """The full address to paste into claude.ai's custom-connector dialog.

    Built from the request rather than a setting so it is right on the
    host the operator is actually looking at.
    """
    return request.build_absolute_uri(f"/mcp/k/{secret}/")


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def connectors_page(request):
    if request.method == "POST":
        _act(request)
        return redirect(request.path)

    # Assembled by hand rather than through `build_absolute_uri`, which
    # percent-encodes the ellipsis into `%E2%80%A6` and turns a display
    # string into line noise.
    origin = request.build_absolute_uri("/").rstrip("/")
    all_rows = [
        {
            "token": t,
            "state": t.state,
            # The URL without its secret half. Enough to recognise which
            # connector a log line or a claude.ai entry belongs to;
            # useless to anyone who copies it.
            "masked_url": f"{origin}/mcp/k/{t.prefix}…{t.last_4}/",
        }
        for t in url_tokens.list_for_user(request.user)
    ]
    # Live first, dead sinks — rotation revokes a row every time it runs,
    # so the revoked pile grows for the life of the install. Stable sort,
    # so newest-first survives inside each band.
    _rank = {"active": 0, "expired": 1, "revoked": 2}
    all_rows.sort(key=lambda r: _rank.get(r["state"], 3))
    page_obj = Paginator(all_rows, 50).get_page(request.GET.get("page"))

    new_secret = request.session.pop("new_url_token", None)

    return render(
        request,
        "ops/connectors.html",
        {
            "rows": page_obj.object_list,
            "page_obj": page_obj,
            "total": len(all_rows),
            "active_count": sum(1 for r in all_rows if r["state"] == "active"),
            "tiers": [
                {"value": t, "help": url_tokens.TIER_HELP[t]}
                for t in url_tokens.TIERS
            ],
            "ttl_choices": url_tokens.TTL_CHOICES,
            "default_ttl_days": url_tokens.default_ttl_days(),
            "default_rate_limit": url_tokens.DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN,
            "min_rate_limit": url_tokens.MIN_RATE_LIMIT,
            "max_rate_limit": url_tokens.MAX_RATE_LIMIT,
            # Shown exactly once. Only the SHA-256 is stored, so a refresh
            # cannot bring it back and the page has to say so plainly.
            "new_secret": new_secret,
            "url_auth_enabled": bool(
                getattr(settings, "MCP_URL_AUTH_ENABLED", False)
            ),
            **ops_context(request),
        },
    )


def _act(request) -> None:
    action = request.POST.get("action", "")

    try:
        if action == "create":
            ttl_days = url_tokens.clean_ttl_days(
                request.POST.get("ttl_days", url_tokens.default_ttl_days())
            )
            generated = url_tokens.generate(
                request.user,
                name=request.POST.get("name", ""),
                max_visibility=request.POST.get("tier", ""),
                rate_limit_per_min=request.POST.get(
                    "rate_limit", url_tokens.DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN
                ),
                expires_at=url_tokens.expires_at_from_ttl(ttl_days),
            )
            _stash_secret(request, generated)
            messages.success(
                request,
                f"Minted {generated.token.name} — copy the connector URL below, "
                "it is shown once.",
            )
            return

        token = _token_or_none(request)
        if token is None:
            messages.error(request, "That connector no longer exists.")
            return

        if action == "rotate":
            generated = url_tokens.rotate(token)
            _stash_secret(request, generated)
            messages.warning(
                request,
                f"Rotated {token.name}. The old URL stopped working now — paste "
                "the new one into claude.ai before closing this page.",
            )

        elif action == "revoke":
            url_tokens.revoke(token)
            messages.success(
                request,
                f"Revoked {token.name}. That URL fails immediately; your API keys "
                "are untouched.",
            )

        else:
            messages.error(request, f"Unknown action {action!r}.")

    except url_tokens.InvalidURLToken as exc:
        messages.error(request, str(exc))


def _stash_secret(request, generated) -> None:
    """Hand the one-and-only plaintext to the next render, as a URL.

    In the session, so a refresh of the redirect target cannot re-show it
    — `pop` on read. The token itself is never written to the session
    beyond this hop and never stored server-side at all.
    """
    request.session["new_url_token"] = {
        "url": connector_url(request, generated.secret),
        "name": generated.token.name,
        "tier": generated.token.max_visibility,
    }


def _token_or_none(request):
    """Resolve `token_id` under the owner-only manager, so a crafted id
    belonging to another user is indistinguishable from one that never
    existed."""
    try:
        token_id = int(request.POST.get("token_id", ""))
    except (TypeError, ValueError):
        return None
    return url_tokens.get_user_token(request.user, token_id=token_id)
