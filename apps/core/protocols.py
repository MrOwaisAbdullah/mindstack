"""Project-wide Protocol / generic types.

Per-app swappable concerns (email backend, payment provider, OAuth provider,
MCP transport) live next to their host in `apps/<app>/protocols.py`. This
module keeps types that more than one app needs.
"""
from __future__ import annotations

from typing import Generic, Iterable, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class Storage(Protocol):
    """File storage backend.

    Default driver = filesystem (in-tree). Phase 9 introduces S3 / R2 swap
    via `STORAGE_DRIVER` env var.
    """

    def save(self, key: str, data: bytes) -> str: ...
    def open(self, key: str, mode: str = "rb") -> object: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def url(self, key: str) -> str: ...


class Provider(Generic[T]):
    """Driver registry for swappable backends.

    Each app registers its drivers in `AppConfig.ready()`:

        email_provider = Provider[EmailBackend]("email")
        email_provider.register("postmark", PostmarkBackend)
        email_provider.register("smtp", SmtpBackend)

    Then resolve via env var:

        cls = email_provider.get(settings.EMAIL_BACKEND_DRIVER)
        backend = cls(...)
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._registry: dict[str, type[T]] = {}

    def register(self, key: str, cls: type[T]) -> None:
        if key in self._registry:
            raise ValueError(f"Provider {self.name!r}: duplicate driver {key!r}")
        self._registry[key] = cls

    def get(self, key: str) -> type[T]:
        if key not in self._registry:
            available = ", ".join(sorted(self._registry)) or "(none)"
            raise KeyError(
                f"Provider {self.name!r}: no driver {key!r}. Available: {available}"
            )
        return self._registry[key]

    def keys(self) -> Iterable[str]:
        return self._registry.keys()
