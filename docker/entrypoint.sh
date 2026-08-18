#!/usr/bin/env bash
# Entrypoint for all three roles. Starts as root only long enough to fix
# /data ownership (named volumes are created root-owned; the app runs as
# uid 1000 — PLAN.md grill A5c), then drops privileges via gosu.
set -euo pipefail

ROLE="${1:-web}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/brain-repo /data/brain-views /data/state
    # Named volumes need the chown; the local stack bind-mounts host dirs
    # whose ownership the container can't (and needn't) change.
    chown -R app:app /data 2>/dev/null || true
    # "$@", not "$ROLE": the fallthrough role below is a full command line.
    exec gosu app "$0" "$@"
fi

cd /app

case "$ROLE" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    # Converge the Q2 cron schedule on every deploy (config/scheduled.py).
    # web owns this for the same reason it owns migrate: exactly one role
    # must do it. Non-fatal — a schedule that failed to sync must not stop
    # the server from serving, and the next deploy retries.
    python manage.py sync_scheduled \
        || echo "entrypoint: sync_scheduled failed — background beats may be stale" >&2
    exec gunicorn config.asgi:application \
        -k uvicorn.workers.UvicornWorker \
        --bind 0.0.0.0:8000 \
        --workers "${WEB_CONCURRENCY:-3}" \
        --graceful-timeout "${WEB_GRACEFUL_TIMEOUT:-30}" \
        --timeout "${WEB_TIMEOUT:-60}"
    ;;
  dev)
    # Local full-stack role (docker-compose.local.yml + dev.ps1). uvicorn
    # --reload, NOT runserver: SSE (chat streaming, M3.1) buffers under
    # runserver's WSGI handler — uvicorn matches the prod server. Reload
    # must poll: inotify doesn't cross the Windows bind mount reliably.
    python manage.py migrate --noinput
    python manage.py brain_bootstrap \
        || echo "entrypoint: brain_bootstrap failed — /readyz stays red until the clone is valid" >&2
    export WATCHFILES_FORCE_POLLING=true
    exec python -m uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload
    ;;
  mcp)
    # FastMCP loopback subprocess; the web container's /mcp proxy targets
    # this over the compose network with MCP_LOOPBACK_SECRET auth.
    exec python -m apps.core.mcp.server
    ;;
  worker)
    # django-q2 cluster — feed extraction only (PLAN.md §7); ack timeout
    # > task timeout is validated at boot (double-billing guard, A11).
    exec python manage.py qcluster
    ;;
  *)
    exec "$@"
    ;;
esac
