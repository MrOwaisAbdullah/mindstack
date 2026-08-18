"""Local full-stack settings — dev ergonomics on prod-shaped services.

`dev.ps1` starts web + mcp + worker + postgres + redis from
`docker-compose.local.yml` and points every container at this module:
DEBUG + the runserver autoreloader from dev.py, but Postgres and Redis
instead of SQLite + LocMem, so the laptop stack matches the deployed one.

NEVER deploy with this module — `assert_prod_safe()` runs from prod.py
only, and DEBUG is on here.
"""
from urllib.parse import urlparse

from .dev import *  # noqa: F401,F403
from .env import settings as _env

# Compose hands every container
# DATABASE_URL=postgres://brain:…@postgres:5432/brain. Deliberately
# smaller than prod.py's parser: no pool tuning, no application_name —
# this is one developer on one laptop. CONN_MAX_AGE is 0 because the
# autoreloader restarts the process constantly and persistent
# connections just leak until Postgres hits max_connections.
if _env.DATABASE_URL.startswith(("postgres://", "postgresql://")):
    _db = urlparse(_env.DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": (_db.path or "").lstrip("/"),
            "USER": _db.username or "",
            "PASSWORD": _db.password or "",
            "HOST": _db.hostname or "",
            "PORT": str(_db.port) if _db.port else "",
            "CONN_MAX_AGE": 0,
            "OPTIONS": {"connect_timeout": 5},
        }
    }

# The browser reaches the stack on localhost (dev.py covers that); these
# are the in-network names — the web healthcheck curls itself, and the
# proxy talks to `mcp` over the compose network.
ALLOWED_HOSTS = list({*ALLOWED_HOSTS, "web", "mcp", "worker"})  # noqa: F405
