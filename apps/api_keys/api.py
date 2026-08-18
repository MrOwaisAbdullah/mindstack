"""Public surface of `apps.api_keys`.

Other apps import only from this module. Direct imports from
`apps.api_keys.{models, services, views, admin, auth_backend}` are
forbidden by the import-linter contracts in `pyproject.toml`.
"""
from __future__ import annotations

from apps.api_keys.auth_backend import authenticate_token, record_use
from apps.api_keys.services.generate import (
    GeneratedKey,
    generate,
    revoke,
    revoke_all_for_user,
    rotate,
)
from apps.api_keys.services.listing import (
    get_live_key_by_id,
    get_user_key,
    get_user_key_or_404,
    list_for_user,
)

__all__ = [
    "GeneratedKey",
    "authenticate_token",
    "generate",
    "get_live_key_by_id",
    "get_user_key",
    "get_user_key_or_404",
    "list_for_user",
    "record_use",
    "revoke",
    "revoke_all_for_user",
    "rotate",
]
