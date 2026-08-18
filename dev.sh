#!/usr/bin/env bash
# dev.sh — the brain stack (web + mcp + worker + postgres + redis) on Docker.
#
# The bash twin of dev.ps1: same commands, same compose files, same project
# name, so the two are interchangeable on a machine that has both. The repo
# is bind-mounted into every app container, so a source edit is live
# immediately: web reloads itself, mcp and worker need `./dev.sh reload`.
# Only a dependency or Dockerfile change needs `./dev.sh rebuild`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SERVICES=(web mcp worker)

USAGE='dev.sh - the brain stack (web + mcp + worker + postgres + redis) on Docker

  ./dev.sh [up]               build if needed, start everything, wait for health
  ./dev.sh reload [svc...]    restart app containers - picks up code, no rebuild
  ./dev.sh rebuild [--no-cache]  rebuild images and recreate containers
  ./dev.sh down [--volumes]   stop and remove containers (--volumes drops the DB)
  ./dev.sh logs [svc...]      follow logs
  ./dev.sh ps                 container + health status
  ./dev.sh status             status plus a /readyz probe
  ./dev.sh shell [svc]        bash inside a container (default: web)
  ./dev.sh manage <args>      run manage.py in the web container
  ./dev.sh superuser          create a Django superuser
  ./dev.sh css [--watch]      rebuild static/css/tw.css from app.css
  ./dev.sh help               this text

Code edits are live: web reloads itself, `reload` covers mcp + worker.
TEMPLATE edits also need `reload web`. Adding a Tailwind class to a
template needs `css` too - tw.css is a committed build artifact, not
something the container generates.'

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }
fail() { printf '\033[31m!!  %s\033[0m\n' "$*"; }

dotenv_value() { # $1=key $2=default
    local line
    if [[ -f "$ROOT/.env" ]]; then
        while IFS= read -r line; do
            line="${line#"${line%%[![:space:]]*}"}"
            [[ "$line" == \#* ]] && continue
            if [[ "$line" == "$1="* ]]; then
                line="${line#"$1="}"
                line="${line%\"}"; line="${line#\"}"
                line="${line%\'}"; line="${line#\'}"
                printf '%s' "$line"
                return
            fi
        done < "$ROOT/.env"
    fi
    printf '%s' "$2"
}

init_compose() {
    if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
        fail "Docker isn't responding. Is it installed and running?"; exit 1
    fi
    if docker compose version >/dev/null 2>&1; then
        COMPOSE=(docker compose)
    elif docker-compose version >/dev/null 2>&1; then
        COMPOSE=(docker-compose)
    else
        fail "Neither 'docker compose' nor 'docker-compose' works."; exit 1
    fi
    COMPOSE+=(--project-name brain-local --project-directory "$ROOT"
              -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.local.yml")
    # docker-compose.yml interpolates ${POSTGRES_PASSWORD:?...}; unset, compose
    # refuses to PARSE the file. A fixed local default keeps `up` a one-liner.
    export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-brain-local-dev}"
}

init_environment() {
    [[ -f "$ROOT/.env" ]] || warn ".env is missing - containers fall back to built-in defaults."
    # Bind-mounted dirs: when the host dir doesn't exist Docker creates it
    # root-owned and the app can't write to it.
    mkdir -p "$ROOT/data/brain-repo" "$ROOT/data/brain-views" "$ROOT/data/state"
    if [[ -z "$(ls -A "$ROOT/data/brain-repo" 2>/dev/null)" ]]; then
        warn "data/brain-repo is empty - web will try to clone BRAIN_REPO_URL on boot."
    fi
    # compose mounts these secrets as FILES; a missing path becomes a
    # root-owned DIRECTORY and git fails confusingly. Empty file = feature
    # cleanly disabled.
    mkdir -p "$ROOT/secrets"
    if [[ ! -f "$ROOT/secrets/brain_deploy_key" ]]; then
        : > "$ROOT/secrets/brain_deploy_key"
        warn "secrets/brain_deploy_key was missing - created an empty placeholder."
    fi
    if [[ ! -f "$ROOT/secrets/brain_write_pat" ]]; then
        : > "$ROOT/secrets/brain_write_pat"
        warn "secrets/brain_write_pat was missing - created an empty placeholder (paste the fine-grained write PAT into it to enable approval pushes)."
    fi
}

wait_healthy() {
    local cid state running deadline
    cid="$("${COMPOSE[@]}" ps -q web 2>/dev/null | head -n1)"
    if [[ -z "$cid" ]]; then warn "web container not found - skipping health wait."; return 1; fi
    printf '==> waiting for web to pass its healthcheck '
    deadline=$(( $(date +%s) + 300 ))
    while (( $(date +%s) < deadline )); do
        state="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || true)"
        if [[ "$state" == healthy ]]; then printf '\033[32m healthy\033[0m\n'; return 0; fi
        running="$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null || true)"
        if [[ "$state" == unhealthy || "$running" == false ]]; then
            printf '\n'; fail "web is ${state:-gone} / running=${running:-?}. Last 40 log lines:"
            "${COMPOSE[@]}" logs --tail 40 web || true
            return 1
        fi
        printf '.'; sleep 2
    done
    printf '\n'; warn "web didn't report healthy within 300s - check ./dev.sh logs web"
    return 1
}

show_readiness() {
    # /readyz is the honest check: DB reachable AND the brain clone valid with
    # its contract files present. Probed from inside the container.
    local text
    text="$("${COMPOSE[@]}" exec -T web curl -sS -m 20 http://127.0.0.1:8000/readyz 2>/dev/null || true)"
    if [[ -z "$text" ]]; then warn "/readyz returned nothing."; return; fi
    if [[ "$text" == *'"status": "ok"'* || "$text" == *'"status":"ok"'* ]]; then
        printf '\033[32m==> /readyz ok - db reachable, brain clone valid\033[0m\n'
    else
        warn "/readyz reports NOT ready: $text"
        warn "Usually the brain clone - check data/brain-repo, then ./dev.sh manage brain_bootstrap"
    fi
}

show_endpoints() {
    local ops adm
    ops="$(dotenv_value ADMIN_PANEL_URL_PATH 'ops/')"; ops="${ops%/}"; ops="${ops#/}"
    adm="$(dotenv_value DJANGO_ADMIN_URL_PATH 'django-admin/')"; adm="${adm%/}"; adm="${adm#/}"
    cat <<EOF

  App        http://localhost:8000
  Ops UI     http://localhost:8000/$ops/
  Django     http://localhost:8000/$adm/
  Docs       http://localhost:8000/docs/
  MCP        http://localhost:8000/mcp   (container direct on :9002)
  Health     http://localhost:8000/healthz     Readiness /readyz
  Postgres   localhost:5433  db=brain user=brain password=$POSTGRES_PASSWORD
  Redis      localhost:6380

  Code edits are live; web reloads itself.
  ./dev.sh reload   restart mcp + worker after a code change
  ./dev.sh logs     follow logs         ./dev.sh down   stop
  No login yet?  ./dev.sh superuser
EOF
}

css_build() {
    # Rebuild static/css/tw.css with the Tailwind v4 standalone binary.
    # No Node, no node_modules: the binary is cached in .cache/ (gitignored)
    # and the OUTPUT is committed, so Docker and self-hosters never need a
    # toolchain.
    local version os arch asset bin url args=()
    version="$(tr -d '[:space:]' < "$ROOT/scripts/tailwind-version.txt")"
    case "$(uname -s)" in
        Darwin) os=macos ;;
        Linux)  os=linux ;;
        *) fail "unsupported OS for the standalone tailwind binary: $(uname -s)"; exit 1 ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) arch=arm64 ;;
        x86_64|amd64)  arch=x64 ;;
        *) fail "unsupported arch: $(uname -m)"; exit 1 ;;
    esac
    asset="tailwindcss-$os-$arch"
    bin="$ROOT/.cache/tailwindcss-$version-$asset"
    if [[ ! -x "$bin" ]]; then
        mkdir -p "$ROOT/.cache"
        step "downloading tailwindcss $version ($asset)"
        url="https://github.com/tailwindlabs/tailwindcss/releases/download/$version/$asset"
        curl -fsSL -o "$bin" "$url"
        chmod +x "$bin"
    fi
    args=(-i "$ROOT/assets/css/app.css" -o "$ROOT/static/css/tw.css" --minify)
    if [[ "${1:-}" == "--watch" || "${1:-}" == "-w" ]]; then
        step "watching templates -> static/css/tw.css (Ctrl+C to stop)"
        args+=(--watch)
        "$bin" "${args[@]}"
    else
        step "building static/css/tw.css"
        "$bin" "${args[@]}"
        printf '\033[32m    static/css/tw.css - %s bytes\033[0m\n' "$(wc -c < "$ROOT/static/css/tw.css" | tr -d ' ')"
        warn "commit tw.css: the image ships the artifact, it is not built at deploy time."
    fi
}

cmd="${1:-up}"; shift || true
init_compose

case "$cmd" in
    up)
        init_environment
        step "starting the stack (first run builds the image - a few minutes)"
        "${COMPOSE[@]}" up -d --build --remove-orphans
        wait_healthy && show_readiness || true
        "${COMPOSE[@]}" ps || true
        show_endpoints
        ;;
    reload)
        services=("$@"); [[ ${#services[@]} -eq 0 ]] && services=("${APP_SERVICES[@]}")
        step "restarting: ${services[*]}"
        "${COMPOSE[@]}" restart "${services[@]}"
        for s in "${services[@]}"; do [[ "$s" == web ]] && { wait_healthy || true; }; done
        printf '\033[32m==> reloaded\033[0m\n'
        ;;
    rebuild)
        init_environment
        build_args=(build --pull)
        for a in "$@"; do [[ "$a" == "--no-cache" ]] && build_args+=(--no-cache) || build_args+=("$a"); done
        step "rebuilding images"
        "${COMPOSE[@]}" "${build_args[@]}"
        step "recreating containers"
        "${COMPOSE[@]}" up -d --force-recreate --remove-orphans
        wait_healthy && show_readiness || true
        show_endpoints
        ;;
    down)
        down_args=(down --remove-orphans)
        for a in "$@"; do
            if [[ "$a" == "--volumes" || "$a" == "-v" ]]; then
                warn "removing named volumes - the local Postgres database will be DELETED."
                down_args+=(--volumes)
            fi
        done
        step "stopping the stack"
        "${COMPOSE[@]}" "${down_args[@]}"
        printf '\033[32m==> down\033[0m\n'
        ;;
    logs)   "${COMPOSE[@]}" logs -f --tail 100 "$@" || true ;;
    ps)     "${COMPOSE[@]}" ps || true ;;
    status) "${COMPOSE[@]}" ps || true; show_readiness ;;
    shell)  "${COMPOSE[@]}" exec "${1:-web}" bash || true ;;
    manage)
        if [[ $# -eq 0 ]]; then fail "usage: ./dev.sh manage <command> [args]"; exit 1; fi
        "${COMPOSE[@]}" exec web python manage.py "$@" || true
        ;;
    superuser) "${COMPOSE[@]}" exec web python manage.py createsuperuser || true ;;
    css)    css_build "${1:-}" ;;
    help|-h|--help) printf '%s\n' "$USAGE" ;;
    *)
        fail "unknown command '$cmd'"
        printf '%s\n' "$USAGE"
        exit 1
        ;;
esac
