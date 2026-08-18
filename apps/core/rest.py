"""Generic async REST view for registered endpoints.

`make_endpoint_view(spec)` returns a Django async view that:

1. Verifies the HTTP method.
2. Parses the JSON body into `spec.input_model` (pydantic v2).
3. Resolves a `Principal` from `Authorization: Bearer mcpsk_...`. This
   server fronts a private knowledge base, so a bearer is mandatory —
   there is no anonymous tier at any layer (PLAN.md §5).
4. Builds a `Ctx` carrying request_id + user + credential.
5. Awaits `spec.cls().run(inp, ctx)`.
6. Serializes the output via `spec.output_model.model_dump_json()`.
7. Echoes `X-Request-ID` on the response.

Error contract:

- 400 `invalid_json`           — body is not valid JSON
- 401 `auth_required`          — no bearer
- 401 `invalid_credential`     — Authorization header present but bad
- 405 `method_not_allowed`     — wrong HTTP method
- 422 `input_validation_error` — pydantic input validation failed
- 429 `rate_limit_exceeded`    — throttle, or auth-failure lockout
- 500 `internal_error`         — anything else; safe message; no stacktrace leak
- 503 `endpoint_disabled`      — an operator took this endpoint offline
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError

from apps.core import endpoint_gating, error_hook, idempotency, log_context
from apps.core.bearer import resolve as resolve_bearer
from apps.core.ctx import build_ctx
from apps.core.events import EndpointCalled, fire
from apps.core.registry import EndpointSpec
from apps.core.security.client_ip import client_ip
from apps.core.security.lockout import is_token_locked
from apps.core.throttling import check as throttle_check

log = logging.getLogger(__name__)

AsyncView = Callable[[HttpRequest], Awaitable[HttpResponse]]


def make_endpoint_view(spec: EndpointSpec) -> AsyncView:
    """Build a per-spec async view. Used by `apps.core.urls`."""

    async def _dispatch(request: HttpRequest) -> HttpResponse:
        # RequestIdMiddleware sets request.request_id on every
        # inbound request. The fallback handles in-process callers (tests
        # using the test client without the middleware).
        request_id = getattr(request, "request_id", None) or uuid.uuid4().hex

        if request.method != spec.method:
            return _err(
                request_id,
                status=405,
                code="method_not_allowed",
                message=f"This endpoint only accepts {spec.method}.",
            )

        # runtime endpoint disable. The
        # registry is import-time only; this row gives operators a
        # knob to take a buggy endpoint offline without a redeploy.
        # Sits before auth so a disabled endpoint never burns cycles on
        # credential resolution. 503 (not 4xx) tells well-behaved clients
        # + status-page monitors this is a server-side decision, not a
        # client error.
        gated = await sync_to_async(
            endpoint_gating.is_disabled, thread_sensitive=True
        )(spec.slug)
        if gated:
            response = _err(
                request_id,
                status=503,
                code="endpoint_disabled",
                message=(
                    "This endpoint is temporarily disabled by an operator. "
                    "Try again later."
                ),
            )
            response.headers["Retry-After"] = "60"
            return response

        # resolve Principal from Authorization: Bearer.
        # The resolver registry in `apps.core.bearer` lets feature apps
        # (api_keys today, mcp_oauth) plug in without
        # apps.core importing them — Contract 1 stays clean.
        principal = None
        bearer = _bearer_token(request)
        if bearer is not None:
            # fail fast when the bearer's `mcpsk_<8>`
            # prefix is in lockout. Returns 429 with Retry-After even
            # when the secret half is correct — the lockout is at the
            # prefix level, so a stolen-prefix attacker gets stopped
            # without leaking whether the secret was right.
            gate = is_token_locked(bearer)
            if not gate.allowed:
                response = _err(
                    request_id,
                    status=429,
                    code="rate_limit_exceeded",
                    message=(
                        "Too many failed authentication attempts on this key prefix. "
                        f"Retry after {gate.retry_after_s}s."
                    ),
                    extra={"retry_after_s": gate.retry_after_s},
                )
                response.headers["Retry-After"] = str(gate.retry_after_s)
                return response

            principal = await resolve_bearer(bearer)
            if principal is None:
                return _err(
                    request_id,
                    status=401,
                    code="invalid_credential",
                    message="Bearer token is invalid, revoked, or expired.",
                )
        # Stash the resolved principal so the `EndpointCalled` fire at the
        # end of `view()` can attribute the call without re-doing the auth
        # lookup. (This slot originally fed a RequestLogMiddleware that
        # was never installed — the fire in `view()` is what reads it now.)
        request._principal = principal  # type: ignore[attr-defined]

        # backfill `user_id` on the log contextvar now that
        # bearer resolution succeeded. `RequestIdMiddleware` bound
        # (request_id, user_id=None) on entry; this update lets every log
        # record emitted inside `run()` carry the resolved user. Reset is
        # handled by the outer middleware's `unbind()`.
        if principal is not None:
            log_context.update_user_id(principal.user.pk)

        # There was a second gate here — `admin_only`, "hide this
        # endpoint from non-staff callers". It gated on
        # `principal.user.is_staff`, and setup sets `is_staff=True` on
        # the single account every credential in this product resolves
        # to, so the branch was unreachable by construction. Removed
        # before launch rather than left looking live. `disabled`
        # (above) is the surviving runtime knob.

        # BRAIN-SERVER FORK DIVERGENCE: every endpoint requires an
        # authenticated caller. The upstream template allowed anonymous
        # calls to any endpoint it wasn't billing for; this server fronts
        # a private knowledge base, so there is no anonymous tier
        # (PLAN.md §5 "No anonymous access, any layer").
        if principal is None:
            return _err(
                request_id,
                status=401,
                code="auth_required",
                message=(
                    "Authentication required. "
                    "Provide an `Authorization: Bearer <token>` header."
                ),
            )

        # rate limit. Sync DB-bound (entitlement lookup +
        # bucket consume), so wrap in sync_to_async. The throttle hook
        # returns "allowed" when rate_limit isn't installed (no-op
        # default) so unit tests of EndpointView still work.
        principal_user = principal.user if principal else None
        # BRAIN-SERVER FORK DIVERGENCE: pass the credential so the
        # throttle can be per-KEY (all consumers share one user here;
        # the upstream per-user bucket would let one chatty consumer
        # starve the rest — PLAN.md grill A3).
        throttle = await sync_to_async(throttle_check, thread_sensitive=True)(
            user=principal_user,
            endpoint_slug=spec.slug,
            ip=client_ip(request),
            credential=principal.credential if principal else None,
        )
        if not throttle.allowed:
            response = _err(
                request_id,
                status=429,
                code="rate_limit_exceeded",
                message=(
                    f"Rate limit exceeded ({throttle.limit_per_min}/min). "
                    f"Retry after {throttle.retry_after_s}s."
                ),
                extra={
                    "limit_per_min": throttle.limit_per_min,
                    "retry_after_s": throttle.retry_after_s,
                },
            )
            response.headers["Retry-After"] = str(throttle.retry_after_s)
            response.headers["X-RateLimit-Limit"] = str(throttle.limit_per_min)
            response.headers["X-RateLimit-Remaining"] = "0"
            return response

        # Idempotency-Key dispatch. Runs BEFORE execute so a replay
        # never re-runs the endpoint. Anonymous callers SKIP processing
        # (no user scope = collision risk).
        # Body bytes are read here once (we'll re-use them for json.loads
        # below). Cache hits short-circuit with the previously-stored
        # response verbatim; mismatch → 422; in-flight → 409.
        body_bytes = request.body or b""
        idem_record: Any = None
        if bearer is not None and principal is not None:
            outcome = await sync_to_async(
                idempotency.process_request, thread_sensitive=True
            )(
                key=request.headers.get("Idempotency-Key", ""),
                user=principal.user,
                method=request.method or "",
                path=request.path or "",
                body=body_bytes,
            )
            if isinstance(outcome, idempotency.InvalidKey):
                return _err(
                    request_id,
                    status=400,
                    code="idempotency_key_invalid",
                    message=outcome.reason,
                )
            if isinstance(outcome, idempotency.Mismatch):
                return _err(
                    request_id,
                    status=422,
                    code="idempotency_key_mismatch",
                    message=(
                        "An Idempotency-Key with a different request body "
                        "was used recently. Use a fresh key or re-send the "
                        "original body."
                    ),
                )
            if isinstance(outcome, idempotency.InFlight):
                response = _err(
                    request_id,
                    status=409,
                    code="idempotency_request_in_flight",
                    message=(
                        "A request with this Idempotency-Key is currently "
                        "being processed. Retry in a moment."
                    ),
                )
                response.headers["Retry-After"] = "1"
                return response
            if isinstance(outcome, idempotency.Replay):
                response = HttpResponse(
                    outcome.response_body,
                    content_type=outcome.response_content_type or "application/json",
                    status=outcome.response_status,
                )
                response.headers["X-Request-ID"] = request_id
                response.headers["Idempotent-Replayed"] = "true"
                return response
            if isinstance(outcome, idempotency.Pending):
                idem_record = outcome.record
                # Stashed for the cancellation guard in `view()`: a
                # request killed mid-run must not squat on its key.
                request._idem_record = idem_record  # type: ignore[attr-defined]
            # Skipped → idem_record stays None; flow continues normally.

        try:
            raw = body_bytes.decode("utf-8") if body_bytes else "{}"
            payload: Any = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return await _finalize_idem(
                idem_record,
                _err(
                    request_id,
                    status=400,
                    code="invalid_json",
                    message=f"Request body is not valid JSON: {exc}",
                ),
            )
        if not isinstance(payload, dict):
            return await _finalize_idem(
                idem_record,
                _err(
                    request_id,
                    status=400,
                    code="invalid_json",
                    message="Request body must be a JSON object.",
                ),
            )

        try:
            inp = spec.input_model.model_validate(payload)
        except ValidationError as exc:
            return await _finalize_idem(
                idem_record,
                _err(
                    request_id,
                    status=422,
                    code="input_validation_error",
                    message="Input validation failed.",
                    extra={"errors": exc.errors(include_url=False)},
                ),
            )

        ctx = build_ctx(
            request_id=request_id,
            source="rest",
            user=principal.user if principal else getattr(request, "user", None),
            credential=principal.credential if principal else None,
        )
        # let `ctx.trace.exception(...)` resolve the slug
        # without the endpoint having to pass it in explicitly.
        ctx.meta["endpoint_slug"] = spec.slug

        # A credit charge used to wrap `run()` here: build an idempotency
        # key, enter a charge context manager, map InsufficientCreditsError
        # to 402, refund on 422/500. None of it could fire — no endpoint in
        # this product declares a `credits_cost`, and no backend was ever
        # registered, so `charge()` handed back a no-op context manager on
        # every request. Removed with the rest of the billing apparatus;
        # this server is not a metered API.

        try:
            instance = spec.cls()
            result = await instance.run(inp, ctx)
        except ValueError as exc:
            # CLAUDE.md `run()` contract — an endpoint signals "user
            # input passed Pydantic but failed a downstream check" by
            # raising ValueError. Canonical examples: `safe_request`
            # refused the URL as unsafe (SSRF guard), an upstream API
            # returned an unparseable shape the endpoint re-raised as
            # ValueError, the user-supplied URL pointed at a private
            # IP. These are 422s, NOT 500s — the input was bad even
            # though it parsed.
            #
            # Why not record an error event: the Pydantic-422 path
            # above doesn't either. Logging every SSRF probe + every
            # bad-upstream response would flood `/ops/logs/` with rows
            # operators have to learn to ignore. An error row is
            # reserved for actual server-side bugs.
            #
            # Message cap: surface the endpoint author's message
            # verbatim (the contract is that ValueError messages are
            # user-safe), but cap at 500 chars so a runaway exception
            # can't return a multi-KB response.
            msg = str(exc)[:500] or "Input failed downstream validation."
            return await _finalize_idem(
                idem_record,
                _err(
                    request_id,
                    status=422,
                    code="input_validation_error",
                    message=msg,
                ),
            )
        except Exception as exc:
            # Persist a structured error row via the `apps.core.error_hook`
            # bridge, which `EventsConfig.ready()` registers.
            try:
                await sync_to_async(
                    error_hook.record_error, thread_sensitive=True
                )(
                    exc=exc,
                    request_id=request_id,
                    source="rest",
                    endpoint_slug=spec.slug,
                    request_path=getattr(request, "_scrubbed_path", request.path) or "",
                    request_method=request.method or "",
                    status_code=500,
                    user_id=(principal.user.pk if principal is not None else None),
                    ip=client_ip(request),
                    user_agent=request.headers.get("User-Agent", ""),
                    handled=False,
                )
            except Exception:
                log.exception(
                    "rest: error event write failed",
                    extra={"request_id": request_id, "slug": spec.slug},
                )
            log.exception(
                "endpoint %s/%s raised",
                spec.version,
                spec.slug,
                extra={"request_id": request_id},
            )
            return await _finalize_idem(
                idem_record,
                _err(
                    request_id,
                    status=500,
                    code="internal_error",
                    message="The endpoint failed unexpectedly.",
                ),
            )

        if not isinstance(result, spec.output_model):
            log.error(
                "endpoint %s/%s returned %r, expected %s",
                spec.version,
                spec.slug,
                type(result).__name__,
                spec.output_model.__name__,
                extra={"request_id": request_id},
            )
            return await _finalize_idem(
                idem_record,
                _err(
                    request_id,
                    status=500,
                    code="internal_error",
                    message="The endpoint returned an unexpected response shape.",
                ),
            )

        body = result.model_dump_json()
        response = HttpResponse(body, content_type="application/json", status=200)
        response.headers["X-Request-ID"] = request_id
        if spec.deprecated and spec.deprecated_at is None:
            # Legacy boolean flag (pre-F3) had no date associated; stamp
            # a coarse `Deprecation: true` so existing clients still see
            # the warning. The richer Deprecation/Sunset/Link triad lands
            # via the outer wrapper when `deprecated_at` is set.
            response.headers["Deprecation"] = "true"

        # Note: stamping APIKey.last_used_* lives in apps.api_keys as a
        # subscriber on `EndpointCalled` — keeps the core
        # request path free of feature-app imports.
        return await _finalize_idem(idem_record, response)

    @csrf_exempt
    async def view(request: HttpRequest) -> HttpResponse:
        # FinalPolish F3 — RFC 8594 deprecation + sunset gate. Sits at the
        # very top of dispatch so a sunset endpoint never burns cycles on
        # auth resolution. Deprecation only stamps advisory headers;
        # sunset short-circuits with 410 Gone.
        now = timezone.now()
        request_id = getattr(request, "request_id", None) or uuid.uuid4().hex

        if spec.is_sunset_at(now):
            sunset_iso = spec.sunset_at.isoformat() if spec.sunset_at else ""
            response = _err(
                request_id,
                status=410,
                code="endpoint_sunset",
                message=(
                    spec.deprecation_message
                    or f"This endpoint was sunset on {sunset_iso} and is no longer available."
                ),
                extra={"sunset_at": sunset_iso},
            )
            for k, v in spec.deprecation_response_headers().items():
                response.headers[k] = v
            return response

        start = time.perf_counter()
        try:
            response = await _dispatch(request)
        except BaseException:
            # `CancelledError` is a BaseException, so a client disconnect
            # or a `docker compose up -d` mid-call escaped every handler
            # in `_dispatch` — leaving the just-claimed Idempotency-Key
            # row incomplete. Every retry with that key then 409'd
            # "in flight" until the 24h purge. The claim is released here
            # (best-effort, shielded from a second cancellation) so the
            # retry takes the fresh-INSERT path instead.
            record = getattr(request, "_idem_record", None)
            if record is not None and not getattr(record, "is_complete", True):
                try:
                    await asyncio.shield(
                        sync_to_async(record.delete, thread_sensitive=True)()
                    )
                except BaseException:  # noqa: BLE001 - cleanup must not mask the exit
                    log.warning(
                        "rest: could not release idempotency claim key=%s",
                        getattr(record, "key", "?"),
                        exc_info=True,
                    )
            raise

        # Stamp the RFC 8594 advisory headers on EVERY response of a
        # deprecated-but-not-yet-sunset endpoint — including 4xx errors
        # so clients hitting validation failures still see the warning.
        if spec.is_deprecation_active_at(now):
            for k, v in spec.deprecation_response_headers().items():
                response.headers[k] = v

        # Fire `EndpointCalled` — the MCP proxy has always done this
        # (`_safe_record`), and `APIKey.last_used_*` is stamped by a
        # subscriber on it. The REST path only *stashed* the principal,
        # for a RequestLogMiddleware that was never installed, so a key
        # used exclusively over REST read as "never used" in its own
        # columns and the ops page had to work the truth out of the event
        # log instead (`apps.mind.consumers.rows`). One fire per call,
        # here, because every dispatch path funnels through this exit.
        # (The 410 sunset short-circuit above doesn't reach it — no auth
        # ran, so there is nothing to attribute; MCP behaves the same.)
        principal = getattr(request, "_principal", None)
        try:
            await sync_to_async(fire, thread_sensitive=True)(
                EndpointCalled(
                    request_id=request_id,
                    endpoint_slug=spec.slug,
                    source="rest",
                    status_code=response.status_code,
                    latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                    user_id=principal.user.pk if principal else None,
                    credential_id=(
                        principal.credential.pk
                        if principal and principal.credential is not None
                        else None
                    ),
                    credential_kind=(principal.credential_kind if principal else ""),
                    ip=client_ip(request),
                    user_agent=request.headers.get("User-Agent", ""),
                )
            )
        except Exception:
            log.exception(
                "rest: EndpointCalled fire failed",
                extra={"request_id": request_id, "slug": spec.slug},
            )
        return response

    view.__name__ = f"endpoint_{spec.version}_{spec.slug}"
    return view


async def _finalize_idem(record: Any, response: HttpResponse) -> HttpResponse:
    """Cache the rendered response on the idempotency row, if any.

    Called as the last step on every terminal exit of `make_endpoint_view`'s
    inner view. `cache_response` itself is a no-op when status is 5xx
    (the row is dropped instead so a transient failure doesn't cement
    a bad response). Always returns the response unchanged."""
    if record is None:
        return response
    body = response.content if hasattr(response, "content") else b""
    content_type = response.get("Content-Type", "application/json")
    try:
        await sync_to_async(idempotency.cache_response, thread_sensitive=True)(
            record,
            status=response.status_code,
            body=bytes(body),
            content_type=content_type,
        )
    except Exception:
        log.exception(
            "rest: idempotency cache_response failed (key=%s user=%s)",
            getattr(record, "key", "?"),
            getattr(record, "user_id", "?"),
        )
    return response



def _bearer_token(request: HttpRequest) -> str | None:
    """Extract the bearer token from `Authorization: Bearer <token>`.

    Returns None when the header is absent or doesn't carry a bearer
    scheme. We don't return the token for any other scheme — Phase 4 may
    layer additional schemes on top, but until then unknown ones are
    treated as "no credential" rather than 401, matching anonymous flow.
    """
    raw = request.headers.get("Authorization", "")
    if not raw:
        return None
    parts = raw.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _err(
    request_id: str,
    *,
    status: int,
    code: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> JsonResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    response = JsonResponse(body, status=status)
    response.headers["X-Request-ID"] = request_id
    return response
