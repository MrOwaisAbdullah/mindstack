"""Fernet helper for AppSetting values.

Key resolution:
- `FIELD_ENCRYPTION_KEY` (a Fernet key: urlsafe-b64 of 32 bytes) when set —
  REQUIRED in prod (`env.assert_prod_safe` refuses boot without it).
- Dev fallback: a key derived from SECRET_KEY. Fine locally; dangerous in
  prod because rotating SECRET_KEY would then destroy every encrypted value
  — which is exactly why prod boot demands a dedicated key.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

__all__ = ["decrypt", "encrypt", "InvalidToken"]


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = (settings.FIELD_ENCRYPTION_KEY or "").strip()
    if not key:
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
