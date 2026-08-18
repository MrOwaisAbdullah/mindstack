"""Production settings: fail-fast on missing or default-value secrets.

Phase 9.4.4 admin-URL hardening + SECRET_KEY + ALLOWED_HOSTS validation
lives in `Settings.assert_prod_safe()` — called below at module load so
boot fails immediately rather than at first request.

Phase 11.4 added `DATABASE_URL` parsing: when set to a `postgres://...`
URL (the canonical 12-factor shape), prod swaps the SQLite default from
base.py for psycopg + connection pooling. The parser is intentionally
inlined here (no `dj-database-url` dep) — the URL format is stable
enough that 30 lines of regex-free urllib.parse handles it.
"""
import os
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .env import settings as _env

_env.assert_prod_safe()

DEBUG = False

# ----- DATABASE_URL parsing ---------------------------
# Operators set DATABASE_URL=postgres://user:pass@host:5432/db. The Q2
# default broker is also the ORM (Postgres-backed), so Q2 inherits this DB.
#
# This used to fall through to base.py's SQLite default in silence. That
# file lives at /app/db.sqlite3 — inside the container, on no volume — so
# the install worked for weeks and then lost every feed, event, API key and
# ledger row on the next image pull. A typo'd or missing DATABASE_URL is a
# misconfiguration, not a fallback; prod now refuses to boot.
if not _env.DATABASE_URL:
    raise ImproperlyConfigured(
        "DATABASE_URL is required in production. Set it to "
        "postgres://USER:PASSWORD@HOST:5432/DBNAME. (The compose stack "
        "builds this for you from POSTGRES_PASSWORD.) Refusing to fall "
        "back to container-local SQLite, which is lost on redeploy. "
        "For a throwaway smoke test, run with "
        "DJANGO_SETTINGS_MODULE=config.settings.docker instead."
    )
if not _env.DATABASE_URL.startswith(("postgres://", "postgresql://")):
    raise ImproperlyConfigured(
        f"DATABASE_URL must be a postgres:// or postgresql:// URL; got "
        f"{_env.DATABASE_URL.split('://', 1)[0] + '://' if '://' in _env.DATABASE_URL else _env.DATABASE_URL!r}. "
        "Refusing to fall back to container-local SQLite, which is lost "
        "on redeploy."
    )
_db = urlparse(_env.DATABASE_URL)
# urlparse leaves percent-encoding in place. Passwords routinely contain
# characters that must be encoded to survive a URL (`@`, `/`, `:`, `#`,
# `?`), so decode before handing them to psycopg — otherwise a correctly
# encoded password authenticates as its literal encoded form and fails.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote((_db.path or "").lstrip("/")),
        "USER": unquote(_db.username or ""),
        "PASSWORD": unquote(_db.password or ""),
        "HOST": _db.hostname or "",
        "PORT": str(_db.port) if _db.port else "",
        # CONN_MAX_AGE keeps connections warm across requests. Default
        # 60s assumes either no pgbouncer or session-pool mode; set
        # DB_CONN_MAX_AGE=0 in `.env` when fronting Postgres with
        # transaction-mode PgBouncer. Env-driven so operators don't need a
        # code edit to flip the value.
        "CONN_MAX_AGE": _env.DB_CONN_MAX_AGE,
        "OPTIONS": {
            "connect_timeout": 5,
            # `application_name` shows up in pg_stat_activity. Useful
            # for distinguishing web vs worker vs migration connections
            # in production troubleshooting.
            "application_name": _env.APP_NAME or "brainoutside",
        },
    }
}

# ----- TLS + cookie + HSTS hardening --------------------
# Force every request to HTTPS at the edge. SECURE_SSL_REDIRECT honors the
# `X-Forwarded-Proto` header set by SECURE_PROXY_SSL_HEADER (configure on
# your reverse proxy / load balancer). Without that header, every request
# would 301-redirect into a loop behind a TLS-terminating proxy.
#
# `SECURE_SSL_REDIRECT_ENABLED` env override — defaults to True so
# the prod posture stays safe-by-default. Operators running the full compose
# stack locally (no TLS terminator in front) set this to "0" so the dev
# browser hand-walk works without an HTTPS terminator. Don't disable in real
# prod; HSTS + secure cookies assume HTTPS-only.
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# CSRF_TRUSTED_ORIGINS — without this there was no way out of a very common
# hole. Behind a proxy that doesn't forward X-Forwarded-Proto every request
# 301-loops; the documented remedy is SECURE_SSL_REDIRECT_ENABLED=0, but
# then the browser sends `Origin: https://host` while the request scheme is
# http, so Django rejects EVERY POST — including /setup/ account creation
# and /login/. The install became unrecoverable without editing code.
#
# Defaults to https:// + http:// for each configured ALLOWED_HOSTS entry,
# which is what a self-hoster on a LAN or behind a plain proxy needs.
# Override explicitly with a comma-separated CSRF_TRUSTED_ORIGINS.
_csrf_env = os.environ.get("CSRF_TRUSTED_ORIGINS", "").strip()
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(",") if o.strip()]
else:
    CSRF_TRUSTED_ORIGINS = [
        # `.example.com` in ALLOWED_HOSTS means "any subdomain"; the CSRF
        # setting spells that `*.example.com`.
        f"{scheme}://{'*' + host if host.startswith('.') else host}"
        for host in ALLOWED_HOSTS  # noqa: F405 - from base
        if host != "*"
        for scheme in ("https", "http")
    ]

# Secure cookies follow the same toggle — secure cookies + plain HTTP causes
# Django to drop session cookies on the floor, breaking magic-link sign-in.
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT

# HSTS — `max-age=1y; includeSubDomains; preload`. Operators submitting to
# the HSTS preload list need all three. Test on a non-preloaded subdomain
# first; once preloaded, every browser refuses HTTP for the registered
# eTLD+1 indefinitely (the un-preload process takes weeks).
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
