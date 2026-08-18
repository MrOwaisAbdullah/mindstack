"""``manage.py doctor`` — operator readiness check.

A single command that runs every "is X reachable / configured /
sensible?" probe at once. Output is one line per probe, color-coded
by level (OK / WARN / FAIL). The admin Settings page imports
:func:`run_probes` directly + renders the same rows inline.

Probes (best-effort, each in its own try/except so one failing
doesn't bail the run):

- DEBUG state               — WARN if DEBUG=True in non-dev env
- ALLOWED_HOSTS             — FAIL when empty + DEBUG=False
- SECRET_KEY                — FAIL when blank or matches placeholder
- DATABASE                  — round-trip ``SELECT 1``
- Cache                     — set/get round-trip
- Migration drift           — pending unapplied migrations
- Field encryption          — FAIL when prod + FIELD_ENCRYPTION_KEY blank
- Admin URL paths           — FAIL when DJANGO_ADMIN_URL_PATH default placeholder
- DCR mode                  — WARN if anonymous
- Email backend             — WARN if console + non-dev environment
- Sentry                    — WARN if DSN unset (informational)
- MCP loopback              — verify subprocess port reachable
- Q2 broker                 — verify ``django_q.tasks.async_task`` importable
- Q2 worker alive           — heartbeat + queue-depth thresholds

Each probe returns a :class:`DoctorRow`. The :func:`Command.handle`
method dumps them with ANSI colors; the admin Settings page renders
them as table rows.
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection

log = logging.getLogger(__name__)

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorRow:
    name: str
    level: str
    message: str


def run_probes() -> list[DoctorRow]:
    """Run every probe + return the rows. Public so the admin Settings
    page can call it directly without spawning a subprocess.

    Builds the probe list from two sources: the built-in `_probe_*`
    callables in this module + any external probes registered via
    `apps.core.doctor_hooks.register_probe` (feature apps install
    theirs from their `AppConfig.ready()` — keeps Contract 1 intact)."""
    from apps.core.doctor_hooks import get_registered_probes

    probes: list[Callable[[], DoctorRow]] = [
        _probe_debug,
        _probe_allowed_hosts,
        _probe_secret_key,
        _probe_database,
        _probe_cache,
        _probe_migrations,
        _probe_field_encryption,
        _probe_admin_url_paths,
        _probe_dcr_mode,
        _probe_email_backend,
        _probe_sentry,
        _probe_mcp_loopback,
        _probe_q2_broker,
    ]
    probes.extend(get_registered_probes())
    out: list[DoctorRow] = []
    for fn in probes:
        try:
            out.append(fn())
        except Exception as exc:  # pragma: no cover - defensive
            out.append(DoctorRow(
                name=fn.__name__.removeprefix("_probe_").removeprefix("doctor_probe_"),
                level=LEVEL_FAIL,
                message=f"probe raised: {type(exc).__name__}: {exc}",
            ))
    return out


# ---- Individual probes -------------------------------------------------


def _probe_debug() -> DoctorRow:
    debug = bool(getattr(settings, "DEBUG", False))
    env = (getattr(settings, "ENVIRONMENT_NAME", "") or "").lower()
    if debug and env in {"prod", "production", "staging"}:
        return DoctorRow("debug", LEVEL_FAIL, f"DEBUG=True in env={env!r} — disable before deploy.")
    if debug:
        return DoctorRow("debug", LEVEL_OK, "DEBUG=True (dev).")
    return DoctorRow("debug", LEVEL_OK, "DEBUG=False.")


def _probe_allowed_hosts() -> DoctorRow:
    hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
    if getattr(settings, "DEBUG", False):
        return DoctorRow("allowed_hosts", LEVEL_OK, f"{hosts!r}")
    if not hosts:
        return DoctorRow("allowed_hosts", LEVEL_FAIL, "Empty ALLOWED_HOSTS in production.")
    return DoctorRow("allowed_hosts", LEVEL_OK, f"{len(hosts)} configured.")


def _probe_secret_key() -> DoctorRow:
    key = getattr(settings, "SECRET_KEY", "") or ""
    if not key:
        return DoctorRow("secret_key", LEVEL_FAIL, "SECRET_KEY is blank.")
    if "CHANGE-ME" in key or "django-insecure" in key:
        return DoctorRow("secret_key", LEVEL_WARN, "SECRET_KEY looks like a Django default — rotate.")
    return DoctorRow("secret_key", LEVEL_OK, "SECRET_KEY set.")


def _probe_database() -> DoctorRow:
    started = time.monotonic()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        return DoctorRow("database", LEVEL_FAIL, f"{type(exc).__name__}: {exc}")
    latency = int((time.monotonic() - started) * 1000)
    return DoctorRow("database", LEVEL_OK, f"SELECT 1 round-trip {latency}ms ({connection.vendor}).")


def _probe_cache() -> DoctorRow:
    started = time.monotonic()
    try:
        cache.set("doctor:probe", "1", timeout=10)
        got = cache.get("doctor:probe")
    except Exception as exc:
        return DoctorRow("cache", LEVEL_FAIL, f"{type(exc).__name__}: {exc}")
    latency = int((time.monotonic() - started) * 1000)
    # `cache` is a ConnectionProxy; dig into the actual backend class.
    try:
        backend_cls = type(cache._connections[cache._alias]).__name__
    except Exception:
        backend_cls = type(cache).__name__
    backend_module = type(cache._connections[cache._alias]).__module__ if hasattr(cache, "_connections") else ""
    if got != "1":
        return DoctorRow("cache", LEVEL_FAIL, f"round-trip failed (backend={backend_cls}).")
    if "locmem" in backend_module or "LocMem" in backend_cls:
        return DoctorRow(
            "cache",
            LEVEL_WARN,
            f"Using {backend_cls} (Redis fallback). Configure REDIS_URL for prod.",
        )
    return DoctorRow("cache", LEVEL_OK, f"{backend_cls} round-trip {latency}ms.")


def _probe_migrations() -> DoctorRow:
    try:
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:
        return DoctorRow("migrations", LEVEL_FAIL, f"{type(exc).__name__}: {exc}")
    if plan:
        return DoctorRow(
            "migrations",
            LEVEL_FAIL,
            f"{len(plan)} unapplied migration(s). Run `manage.py migrate`.",
        )
    return DoctorRow("migrations", LEVEL_OK, "No pending migrations.")


def _probe_field_encryption() -> DoctorRow:
    try:
        from config.settings.env import settings as env_settings  # type: ignore

        key = env_settings.FIELD_ENCRYPTION_KEY.get_secret_value()
    except Exception:
        key = ""
    debug = bool(getattr(settings, "DEBUG", False))
    if not key:
        if debug:
            return DoctorRow(
                "field_encryption",
                LEVEL_WARN,
                "FIELD_ENCRYPTION_KEY blank (dev). Will refuse to boot in prod.",
            )
        return DoctorRow(
            "field_encryption",
            LEVEL_FAIL,
            "FIELD_ENCRYPTION_KEY blank — assert_prod_safe will refuse to boot.",
        )
    return DoctorRow("field_encryption", LEVEL_OK, "FIELD_ENCRYPTION_KEY set.")


def _probe_admin_url_paths() -> DoctorRow:
    django_path = getattr(settings, "DJANGO_ADMIN_URL_PATH", "") or ""
    if "CHANGE-ME" in django_path or django_path == "admin/":
        return DoctorRow(
            "admin_url_paths",
            LEVEL_FAIL,
            f"DJANGO_ADMIN_URL_PATH={django_path!r} — change before deploy.",
        )
    return DoctorRow("admin_url_paths", LEVEL_OK, f"DJANGO_ADMIN_URL_PATH={django_path!r}")


def _probe_dcr_mode() -> DoctorRow:
    mode = getattr(settings, "MCP_OAUTH_DCR_MODE", "anonymous") or "anonymous"
    if mode == "anonymous":
        return DoctorRow(
            "dcr_mode",
            LEVEL_WARN,
            "MCP_OAUTH_DCR_MODE=anonymous — clients self-register without IAT. "
            "OK for Claude.ai compat; flip to iat_required when you've minted IATs.",
        )
    if mode == "disabled":
        return DoctorRow("dcr_mode", LEVEL_OK, "DCR disabled.")
    return DoctorRow("dcr_mode", LEVEL_OK, "DCR mode iat_required.")


def _probe_email_backend() -> DoctorRow:
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if "console" in backend:
        if getattr(settings, "DEBUG", False):
            return DoctorRow("email_backend", LEVEL_OK, "ConsoleBackend (dev).")
        return DoctorRow(
            "email_backend",
            LEVEL_WARN,
            "ConsoleBackend with DEBUG=False — emails won't reach users.",
        )
    return DoctorRow("email_backend", LEVEL_OK, backend.rsplit(".", 1)[-1] or backend)


def _probe_sentry() -> DoctorRow:
    try:
        from config.settings.env import settings as env_settings  # type: ignore

        dsn = env_settings.SENTRY_DSN
    except Exception:
        dsn = ""
    if not dsn:
        return DoctorRow(
            "sentry",
            LEVEL_WARN,
            "SENTRY_DSN unset — error mirroring disabled (informational).",
        )
    return DoctorRow("sentry", LEVEL_OK, "SENTRY_DSN configured.")


def _probe_mcp_loopback() -> DoctorRow:
    host = str(getattr(settings, "MCP_LOOPBACK_HOST", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(settings, "MCP_LOOPBACK_PORT", 0) or 0)
    if not port:
        return DoctorRow("mcp_loopback", LEVEL_WARN, "MCP_LOOPBACK_PORT not configured.")
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except Exception as exc:
        return DoctorRow("mcp_loopback", LEVEL_FAIL, f"{type(exc).__name__}: {exc}")
    latency = int((time.monotonic() - started) * 1000)
    return DoctorRow("mcp_loopback", LEVEL_OK, f"Connected to {host}:{port} ({latency}ms).")


def _probe_q2_broker() -> DoctorRow:
    try:
        import django_q.tasks  # noqa: F401
    except Exception as exc:
        return DoctorRow("q2_broker", LEVEL_FAIL, f"django-q2 import failed: {exc}")
    cluster = getattr(settings, "Q_CLUSTER", {}) or {}
    if cluster.get("redis"):
        return DoctorRow("q2_broker", LEVEL_OK, "Redis broker.")
    if cluster.get("orm"):
        # ORM broker is the documented downgrade path (drop Redis entirely).
        # Flag it in non-dev so an accidental swap-back gets noticed; Redis
        # is the shipped default because we already depend on it for cache,
        # rate-limit, idempotency, and sessions.
        if bool(getattr(settings, "DEBUG", False)):
            return DoctorRow(
                "q2_broker",
                LEVEL_OK,
                f"ORM broker on {cluster.get('orm')!r} (dev).",
            )
        return DoctorRow(
            "q2_broker",
            LEVEL_WARN,
            f"ORM broker on {cluster.get('orm')!r} — Redis is the shipped default. "
            "Swap back unless you intentionally dropped Redis.",
        )
    return DoctorRow("q2_broker", LEVEL_WARN, "Q_CLUSTER has no broker configured.")


# ---- BaseCommand integration -------------------------------------------


class Command(BaseCommand):
    help = "Run readiness probes (database, cache, migrations, security config, etc.)."

    def handle(self, *args, **options) -> None:  # pragma: no cover - shell wrapper
        rows = run_probes()
        ok_count = sum(1 for r in rows if r.level == LEVEL_OK)
        warn_count = sum(1 for r in rows if r.level == LEVEL_WARN)
        fail_count = sum(1 for r in rows if r.level == LEVEL_FAIL)
        for r in rows:
            tag = {
                LEVEL_OK: self.style.SUCCESS("OK   "),
                LEVEL_WARN: self.style.WARNING("WARN "),
                LEVEL_FAIL: self.style.ERROR("FAIL "),
            }.get(r.level, r.level.upper().ljust(5))
            self.stdout.write(f"{tag} {r.name:<24} {r.message}")
        self.stdout.write("")
        self.stdout.write(f"Summary: {ok_count} ok · {warn_count} warn · {fail_count} fail")
        if fail_count:
            raise SystemExit(1)
