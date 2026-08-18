"""Throttle hook registry.

apps.core can't import apps.rate_limit (Contract 1: core has no reverse
deps on feature apps). Same pattern as `apps.core.bearer` and
`apps.core.bearer` — the feature app calls `register(...)` from its
`AppConfig.ready()`, apps.core dispatches without knowing the impl.

When the rate_limit app isn't installed (or it failed to register a
checker), `check()` returns "allowed" so the rest of the request path
still works — useful for unit tests of EndpointView that don't need a
real bucket.

The checker contract is sync + DB-bound; the view-side adapters wrap
it in `sync_to_async` for async views. Phase 8.2 will cache the
entitlement read in Redis so the steady-state cost is a single Redis
RTT.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Protocol

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


@dataclass(frozen=True, slots=True)
class ThrottleResult:
    """Returned by `check()`. The view maps `allowed=False` to 429
    with `Retry-After: retry_after_s`. `limit_per_min` is informational
    (drives `X-RateLimit-Limit` headers)."""

    allowed: bool
    remaining: int
    retry_after_s: int
    limit_per_min: int
    reason: str = ""


class ThrottleChecker(Protocol):
    def __call__(
        self,
        *,
        user: Optional["AbstractBaseUser"],
        endpoint_slug: str,
        ip: str | None,
        cost: int = 1,
    ) -> object: ...


_checker: Optional[ThrottleChecker] = None


def register(checker: ThrottleChecker) -> None:
    """Install a throttle checker. Called from `RateLimitConfig.ready()`."""
    global _checker
    _checker = checker


def is_enabled() -> bool:
    return _checker is not None


def check(
    *,
    user: Optional["AbstractBaseUser"],
    endpoint_slug: str,
    ip: str | None,
    cost: int = 1,
    credential: object = None,
) -> ThrottleResult:
    """Sync entry point. Returns "allowed" no-op when no checker registered.

    BRAIN-SERVER FORK DIVERGENCE: accepts + forwards `credential` so the
    registered checker can rate-limit per KEY (see apps.mind.throttle).
    """
    if _checker is None:
        return ThrottleResult(
            allowed=True,
            remaining=0,
            retry_after_s=0,
            limit_per_min=0,
            reason="disabled",
        )
    raw = _checker(user=user, endpoint_slug=endpoint_slug, ip=ip, cost=cost, credential=credential)
    # The feature app may return its own dataclass; convert via duck
    # typing so `apps.core.throttling.ThrottleResult` is the only shape
    # callers see. Same trick as the apps.core.bearer Principal
    # convergence pattern.
    return ThrottleResult(
        allowed=bool(getattr(raw, "allowed", True)),
        remaining=int(getattr(raw, "remaining", 0)),
        retry_after_s=int(getattr(raw, "retry_after_s", 0)),
        limit_per_min=int(getattr(raw, "limit_per_min", 0)),
        reason=str(getattr(raw, "reason", "")),
    )


__all__ = ["ThrottleResult", "check", "is_enabled", "register"]
