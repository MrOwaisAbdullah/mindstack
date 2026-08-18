"""Read-only queries: the ops page, and the bearer rehydrator.

`get_live_token_by_id` is the load-bearing one. It is registered as the
`url_token` rehydrator in `AppConfig.ready()`, which means it is how the
MCP subprocess rebuilds this credential from a bare pk — and therefore
how the caller gets a tier at all. It re-applies every liveness guard
`authenticate_url_token` applies rather than assuming the proxy checked:
it grants a tier, so it must not hand back a revoked token to anyone who
happens to know its id.

Skipping it is not a no-op that costs a little metadata. `credential =
None` is exactly what an unprofiled key looks like to
`tiers.tier_for_credential`, so every MCP caller would silently read at
`public` and `propose-feed` would refuse everyone — with no error
anywhere. That failure is written up in `docs/DOCS-LAB-FINDINGS.md` §1;
it cost a full debugging session for API keys, and it fails closed,
which is why it survived so long.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from apps.url_mcp_tokens.models import URLMCPToken

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


def is_live(token: URLMCPToken | None) -> bool:
    """The single liveness gate — revoked / expired / inactive user.

    Both the resolver (token in hand) and the rehydrator (pk in hand)
    call this, so the two cannot drift. A guard that exists on only one
    of those paths is a guard the MCP hop walks around.
    """
    if token is None:
        return False
    if token.revoked_at is not None:
        return False
    if token.expires_at is not None and token.expires_at <= timezone.now():
        return False
    if not token.user.is_active:
        return False
    return True


def get_live_token_by_id(token_id: str | int) -> URLMCPToken | None:
    """Resolve by pk alone, applying every liveness guard. Registered as
    the `url_token` bearer rehydrator.

    There is no `user` to scope by: the only caller is the MCP subprocess
    rebuilding an identity the Django proxy already authenticated on this
    same request, and it holds a pk rather than a token.

    Returns None on a non-integer id, an unknown pk, or any dead token.
    """
    try:
        pk = int(token_id)
    except (TypeError, ValueError):
        return None
    token = URLMCPToken.objects.select_related("user").filter(pk=pk).first()
    return token if is_live(token) else None


def list_for_user(user: "AbstractBaseUser") -> list[URLMCPToken]:
    """Every token for a user, newest first — revoked ones included, so
    the page can show what was pulled and when."""
    return list(URLMCPToken.objects.for_user(user).order_by("-created_at"))


def get_user_token(user: "AbstractBaseUser", *, token_id: int) -> URLMCPToken | None:
    """Resolve by id but only if it belongs to `user`.

    Through the owner-only manager, so a crafted id belonging to another
    user is indistinguishable from one that never existed.
    """
    return URLMCPToken.objects.for_user(user).filter(pk=token_id).first()
