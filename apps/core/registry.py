"""Endpoint registry.

Single source of truth for every API endpoint. The `@endpoint` decorator
registers an `Endpoint` subclass under a slug + version. The registry then
drives:

- REST URL generation (`apps/core/urls.py`)
- OpenAPI 3.1 generation (`apps/core/openapi.py`)
- MCP tool registration via FastMCP (`apps/core/mcp/bridge.py`)
- Catalog endpoint (`/api/v1/_catalog`)
- CI validation (`manage.py validate_endpoints`)

Endpoint authors only ever see `@endpoint` and the `Endpoint` base.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar

from pydantic import BaseModel

from apps.core.ctx import Ctx

# Slug rules: lowercase ASCII, digits, hyphens; must start with a letter; 1–50 chars.
# These also become URL path segments and MCP tool names — keep them tight.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
_VERSION_RE = re.compile(r"^v[0-9]+$")
_RESERVED_SLUGS = frozenset({"_catalog", "_openapi", "_openapi.json", "_health"})


class RegistryError(RuntimeError):
    """Raised at import time when an endpoint declaration is malformed."""


class Endpoint:
    """Base class for every registered endpoint.

    Subclasses MUST declare two nested Pydantic v2 models — `Input` and
    `Output` — and implement `async def run(self, inp, ctx) -> Output`.

        class Hello(Endpoint):
            class Input(BaseModel):
                name: str = "world"

            class Output(BaseModel):
                greeting: str

            async def run(self, inp: "Hello.Input", ctx: Ctx) -> "Hello.Output":
                return self.Output(greeting=f"Hello, {inp.name}")

    The `@endpoint(slug=...)` decorator validates these and registers the
    spec on the global `registry`.
    """

    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]

    async def run(self, inp: BaseModel, ctx: Ctx) -> BaseModel:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    """Immutable record of one registered endpoint."""

    slug: str
    version: str
    cls: type[Endpoint]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    method: str = "POST"
    description: str = ""
    deprecated: bool = False
    tags: tuple[str, ...] = ()
    # FinalPolish F3 — RFC 8594-style deprecation + sunset metadata.
    # `deprecated_at`: endpoint is still callable but the REST view stamps
    # `Deprecation:` + `Sunset:` + `Link:` headers on every response so
    # clients can pick up the warning. `sunset_at`: the REST view 410s
    # (and the MCP bridge raises a tool error) on any call after this
    # instant. `deprecation_message` rides in the 410 body + docs banner
    # so endpoint authors can point migrators at the replacement.
    # Both timestamps must be timezone-aware (`datetime.now(tz=UTC)`-shaped);
    # the decorator rejects naive values at import time.
    deprecated_at: datetime | None = None
    sunset_at: datetime | None = None
    deprecation_message: str = ""

    @property
    def path(self) -> str:
        """URL path under `/api/<version>/`. Used by the URLconf builder."""
        return f"{self.slug}"

    @property
    def mcp_tool_name(self) -> str:
        """Phase 2.3.3 uses this when registering the FastMCP tool."""
        return self.slug if self.version == "v1" else f"{self.slug}__{self.version}"

    @property
    def is_deprecated(self) -> bool:
        """Truthy if the spec is flagged via the legacy bool OR a
        deprecation date is set. Docs catalog + detail templates query
        this so the chip / banner shows for both expression styles."""
        return self.deprecated or self.deprecated_at is not None

    def is_sunset_at(self, now: datetime) -> bool:
        """True when `sunset_at` is set and `now` has passed it. Compared
        against a caller-supplied `now` so the REST/MCP gates and tests
        share one decision rule."""
        return self.sunset_at is not None and now >= self.sunset_at

    def is_deprecation_active_at(self, now: datetime) -> bool:
        """True when `deprecated_at` is set and `now` has passed it. The
        REST view stamps Deprecation/Sunset/Link headers when this fires."""
        return self.deprecated_at is not None and now >= self.deprecated_at

    def deprecation_response_headers(self) -> dict[str, str]:
        """RFC 8594 / RFC 8288 advisory headers for callers of a
        deprecated endpoint. `Deprecation` carries the unix timestamp
        of the deprecation moment; `Sunset` (when set) carries the
        HTTP-date of the removal; `Link` points clients at the docs
        page so they can read the migration notes.

        Returns an empty dict when no deprecation metadata is set —
        the caller can unconditionally `headers.update(spec.deprecation_response_headers())`.
        Relative URIs are used in the Link target so the same value is
        valid behind any reverse proxy / host (RFC 5988 § 5.2 allows it)."""
        from email.utils import format_datetime

        out: dict[str, str] = {}
        if self.deprecated_at is None and self.sunset_at is None:
            return out
        if self.deprecated_at is not None:
            out["Deprecation"] = str(int(self.deprecated_at.timestamp()))
        if self.sunset_at is not None:
            # `usegmt=True` produces an IMF-fixdate string (RFC 7231 §
            # 7.1.1.1), which is what Sunset wants per RFC 8594 § 3.
            out["Sunset"] = format_datetime(self.sunset_at, usegmt=True)
        # Docs URL is the same for both states. Keep relative so the
        # value isn't sensitive to public host / reverse-proxy config.
        out["Link"] = f'</docs/{self.slug}/>; rel="deprecation"'
        return out

    def to_catalog_entry(self) -> dict[str, Any]:
        """Shape of one row in `/api/v1/_catalog`."""
        return {
            "slug": self.slug,
            "version": self.version,
            "method": self.method,
            "path": f"/api/{self.version}/{self.path}",
            "description": self.description,
            "deprecated": self.is_deprecated,
            "deprecated_at": (
                self.deprecated_at.isoformat() if self.deprecated_at else None
            ),
            "sunset_at": self.sunset_at.isoformat() if self.sunset_at else None,
            "deprecation_message": self.deprecation_message,
            "tags": list(self.tags),
        }


class Registry:
    """In-process endpoint registry. Single global instance below."""

    def __init__(self) -> None:
        # Keyed by (version, slug) so v2 of the same slug coexists with v1.
        self._specs: dict[tuple[str, str], EndpointSpec] = {}

    def register(self, spec: EndpointSpec) -> None:
        key = (spec.version, spec.slug)
        if key in self._specs:
            existing = self._specs[key]
            raise RegistryError(
                f"Duplicate endpoint registration: {spec.version}/{spec.slug} "
                f"already registered as {existing.cls.__module__}.{existing.cls.__name__}"
            )
        self._specs[key] = spec

    def all(self) -> list[EndpointSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.version, s.slug))

    def get(self, version: str, slug: str) -> EndpointSpec | None:
        return self._specs.get((version, slug))

    def by_slug(self, slug: str) -> list[EndpointSpec]:
        return [s for s in self._specs.values() if s.slug == slug]

    def clear(self) -> None:
        """Test helper. Never called from production code."""
        self._specs.clear()

    def __len__(self) -> int:
        return len(self._specs)


registry = Registry()


def endpoint(
    *,
    slug: str,
    version: str = "v1",
    method: str = "POST",
    description: str = "",
    deprecated: bool = False,
    tags: tuple[str, ...] = (),
    deprecated_at: datetime | None = None,
    sunset_at: datetime | None = None,
    deprecation_message: str = "",
) -> Callable[[type[Endpoint]], type[Endpoint]]:
    """Register an `Endpoint` subclass on the global registry.

    Validation runs at decoration time so misconfigured endpoints fail at
    import, not at first request. The companion `validate_endpoints`
    management command runs the same checks in CI.
    """
    if not _SLUG_RE.fullmatch(slug):
        raise RegistryError(
            f"Invalid endpoint slug {slug!r}. Must match {_SLUG_RE.pattern} "
            f"(lowercase ASCII, digits, hyphens; start with a letter; ≤50 chars)."
        )
    if slug in _RESERVED_SLUGS:
        raise RegistryError(
            f"Slug {slug!r} is reserved for built-in routes (_catalog, _openapi, _health)."
        )
    if not _VERSION_RE.fullmatch(version):
        raise RegistryError(
            f"Invalid endpoint version {version!r}. Must be 'v1', 'v2', etc."
        )
    if method not in {"POST"}:
        # We POST everything by default — endpoints take typed bodies, not query params.
        # If GET ever earns its keep, relax this check. (Phase 11.3 example endpoints
        # may revisit; for now, POST-only keeps the surface predictable.)
        raise RegistryError(f"Unsupported HTTP method {method!r}. Only POST for now.")
    # FinalPolish F3 — require tz-aware deprecation/sunset timestamps.
    # A naive datetime compared against `timezone.now()` (which is UTC and
    # aware) raises TypeError at request time; catch it at import instead
    # so the offending endpoint never reaches production.
    if deprecated_at is not None and deprecated_at.tzinfo is None:
        raise RegistryError(
            f"deprecated_at must be timezone-aware (got naive {deprecated_at!r}). "
            "Use `datetime(..., tzinfo=UTC)` or `django.utils.timezone.now()`."
        )
    if sunset_at is not None and sunset_at.tzinfo is None:
        raise RegistryError(
            f"sunset_at must be timezone-aware (got naive {sunset_at!r}). "
            "Use `datetime(..., tzinfo=UTC)` or `django.utils.timezone.now()`."
        )
    if (
        deprecated_at is not None
        and sunset_at is not None
        and sunset_at <= deprecated_at
    ):
        raise RegistryError(
            f"sunset_at ({sunset_at.isoformat()}) must be strictly after "
            f"deprecated_at ({deprecated_at.isoformat()}). "
            "Give clients time between the deprecation warning and the removal."
        )

    def _wrap(cls: type[Endpoint]) -> type[Endpoint]:
        _validate_endpoint_class(cls, slug=slug)
        resolved_description = description or _first_line(inspect.getdoc(cls))
        spec = EndpointSpec(
            slug=slug,
            version=version,
            cls=cls,
            input_model=cls.Input,
            output_model=cls.Output,
            method=method,
            description=resolved_description,
            deprecated=deprecated,
            tags=tuple(tags),
            deprecated_at=deprecated_at,
            sunset_at=sunset_at,
            deprecation_message=deprecation_message,
        )
        registry.register(spec)
        cls.__endpoint_spec__ = spec  # type: ignore[attr-defined]
        return cls

    return _wrap


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _validate_endpoint_class(cls: type[Endpoint], *, slug: str) -> None:
    """Catch the most common mistakes at decoration time with helpful errors."""
    where = f"@endpoint(slug={slug!r}) on {cls.__module__}.{cls.__name__}"

    if not isinstance(cls, type) or not issubclass(cls, Endpoint):
        raise RegistryError(f"{where}: must subclass apps.core.registry.Endpoint.")

    inp = cls.__dict__.get("Input")
    out = cls.__dict__.get("Output")
    if inp is None or not isinstance(inp, type) or not issubclass(inp, BaseModel):
        raise RegistryError(f"{where}: missing nested `Input(BaseModel)` class.")
    if out is None or not isinstance(out, type) or not issubclass(out, BaseModel):
        raise RegistryError(f"{where}: missing nested `Output(BaseModel)` class.")

    run = cls.__dict__.get("run")
    if run is None:
        raise RegistryError(f"{where}: missing `async def run(self, inp, ctx)`.")
    if not inspect.iscoroutinefunction(run):
        raise RegistryError(f"{where}: `run` must be `async def`, not a regular function.")

    sig = inspect.signature(run)
    params = list(sig.parameters.values())
    # Expect: self, inp, ctx (3 positional). Defaults / extras allowed but uncommon.
    if len(params) < 3:
        raise RegistryError(
            f"{where}: `run` must take (self, inp, ctx). Got signature {sig}."
        )
