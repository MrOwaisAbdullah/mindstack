"""Object-level permission isolation primitives.

Why a custom manager + queryset (instead of leaning on `request.user`
filtering inline in views):

- Centralizes the owner-field semantics on the model. A future view
  author who does `MyModel.objects.get(pk=...)` is one slip away
  from leaking another user's row; routing every read through
  `for_user(user)` makes the contract auditable + grep-able.
- Some models own data via a chain (`WebhookDelivery.endpoint.user`)
  rather than a direct FK. The `_owner_field` class attribute on
  the manager declares the lookup once, the queryset uses it
  uniformly.
- `get_for_user_or_404(user, **lookup)` is the canonical "fetch one
  row by id while enforcing isolation" helper — exactly the pattern
  the verify gate (`user A → user B's id → 404`) tests.

Usage on a model:

    class APIKey(models.Model):
        user = models.ForeignKey(...)
        ...
        objects = OwnerOnlyManager()  # default owner_field="user"

    class WebhookDelivery(models.Model):
        endpoint = models.ForeignKey("WebhookEndpoint", ...)
        ...
        objects = OwnerOnlyManager(owner_field="endpoint__user")

Usage in a view:

    key = APIKey.objects.get_for_user_or_404(request.user, pk=key_id)
    # or:
    user_keys = APIKey.objects.for_user(request.user).filter(is_deleted=False)

`for_user(None)` and `for_user(<anonymous>)` both return an empty
queryset — defensive against a code path that accidentally drops
the user check. We never want "no user" to mean "all rows".
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models
from django.http import Http404

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


class OwnerOnlyQuerySet(models.QuerySet):
    """Per-queryset owner filter. Reads `_owner_field` from the manager
    to know whether to filter on `user=...` or a deeper lookup like
    `endpoint__user=...`."""

    def for_user(self, user: "AbstractBaseUser | None") -> "OwnerOnlyQuerySet":
        """Filter to rows owned by `user`. Empty queryset for unauth /
        None — never falls back to "all rows"."""
        owner_field = getattr(self, "_owner_field", "user")
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(**{owner_field: user})


class OwnerOnlyManager(models.Manager.from_queryset(OwnerOnlyQuerySet)):  # type: ignore[misc]
    """Manager with `for_user(user)` + `get_for_user_or_404(user, **lookup)`.

    Constructed with `owner_field` matching the lookup chain to the
    user FK. Default is `"user"` (most models have a direct FK).
    Cross-FK chains use Django's `__` separator: `"endpoint__user"`,
    `"team__owner"`, etc.
    """

    def __init__(self, *, owner_field: str = "user") -> None:
        super().__init__()
        self._owner_field = owner_field

    def get_queryset(self) -> "OwnerOnlyQuerySet":
        qs: OwnerOnlyQuerySet = super().get_queryset()  # type: ignore[assignment]
        # Stash the owner-field on the queryset so `for_user` reads it
        # without re-binding to the manager.
        qs._owner_field = self._owner_field  # type: ignore[attr-defined]
        return qs

    def for_user(self, user: "AbstractBaseUser | None") -> "OwnerOnlyQuerySet":
        return self.get_queryset().for_user(user)

    def get_for_user_or_404(
        self,
        user: "AbstractBaseUser | None",
        **lookup: Any,
    ) -> models.Model:
        """Fetch exactly one row matching `**lookup` AND owned by `user`,
        or raise `Http404`. Use in views that resolve a row by id.

        The `Http404` (not `PermissionDenied`) choice is deliberate: a
        404 doesn't reveal that the row exists for someone else —
        same response the user would get for a totally unknown id.
        Phase 9.5.3 verifies this end-to-end.
        """
        try:
            return self.for_user(user).get(**lookup)
        except self.model.DoesNotExist as exc:
            raise Http404(
                f"{self.model.__name__} matching {lookup!r} not found"
            ) from exc


__all__ = ["OwnerOnlyManager", "OwnerOnlyQuerySet"]
