#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
RUN_DIR="$ROOT_DIR/.run"

read_env() {
    local key="$1" default_value="$2" value
    value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE" 2>/dev/null || true)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "${value:-$default_value}"
}

mkdir -p "$RUN_DIR"
WEB_PORT="$(read_env WEB_PORT 3000)"
SETUP="$(read_env SETUP false)"
if ! [[ "$WEB_PORT" =~ ^[0-9]+$ ]] || (( WEB_PORT < 1 || WEB_PORT > 65535 )); then
    echo "Invalid WEB_PORT in .env: $WEB_PORT" >&2
    exit 1
fi

export WEB_PORT SETUP
if [[ "${SETUP,,}" != "true" && "${SETUP}" != "1" ]]; then
    if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
        STREAMHOME_SETUP_CODE="$($ROOT_DIR/venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    else
        STREAMHOME_SETUP_CODE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    fi
    export STREAMHOME_SETUP_CODE
    echo "First-run setup is active."
    echo "Setup URL: http://localhost:${WEB_PORT}/setup"
    echo "One-time bootstrap code: ${STREAMHOME_SETUP_CODE}"
fi

if [[ -x "$ROOT_DIR/stop.sh" ]]; then
    "$ROOT_DIR/stop.sh" --quiet || true
fi

echo "Starting StreamHome API on 127.0.0.1:8000..."
if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
    nohup bash -c "cd '$ROOT_DIR/server' && exec '$ROOT_DIR/venv/bin/python' main.py" > "$ROOT_DIR/backend.log" 2>&1 &
else
    nohup bash -c "cd '$ROOT_DIR/server' && exec python3 main.py" > "$ROOT_DIR/backend.log" 2>&1 &
fi
echo $! > "$RUN_DIR/backend.pid"

echo "Starting StreamHome web on 0.0.0.0:${WEB_PORT}..."
nohup bash -c "cd '$ROOT_DIR/web' && exec env NODE_ENV=production WEB_PORT='$WEB_PORT' SETUP='$SETUP' npm run server" > "$ROOT_DIR/frontend.log" 2>&1 &
echo $! > "$RUN_DIR/web.pid"

echo "StreamHome is running at http://localhost:${WEB_PORT}"
echo "Logs: backend.log and frontend.log"
