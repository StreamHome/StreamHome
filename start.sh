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
PUBLIC_URL="$(read_env PUBLIC_URL "http://localhost:${WEB_PORT}")"
if ! [[ "$WEB_PORT" =~ ^[0-9]+$ ]] || (( WEB_PORT < 1 || WEB_PORT > 65535 )); then
    echo "Invalid WEB_PORT in .env: $WEB_PORT" >&2
    exit 1
fi
if [[ "$WEB_PORT" == "8000" ]]; then
    echo "WEB_PORT cannot be 8000 because the API uses that port." >&2
    exit 1
fi

export WEB_PORT SETUP PUBLIC_URL
SETUP_NORMALIZED="$(printf '%s' "$SETUP" | tr '[:upper:]' '[:lower:]')"
SETUP_ACTIVE=false
if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
    BACKEND_PYTHON="$ROOT_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    BACKEND_PYTHON="$(command -v python3)"
else
    echo "Python is unavailable. Run ./setup.sh first." >&2
    exit 1
fi

if [[ "$SETUP_NORMALIZED" != "true" && "$SETUP" != "1" ]]; then
    SETUP_ACTIVE=true
    STREAMHOME_SETUP_CODE="$($BACKEND_PYTHON -c 'import secrets; print(secrets.token_urlsafe(18))')"
    export STREAMHOME_SETUP_CODE
fi

if [[ -x "$ROOT_DIR/stop.sh" ]]; then
    "$ROOT_DIR/stop.sh" --startup || true
fi

port_available() {
    "$BACKEND_PYTHON" - "$1" <<'PY'
import socket
import sys

sock = socket.socket()
try:
    sock.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

if ! port_available 8000; then
    echo "API port 8000 is still in use by an unrelated or uninspectable service." >&2
    echo "StreamHome did not stop that service. Resolve the conflict, then run ./start.sh; setup and dependencies do not need to be repeated." >&2
    exit 1
fi
if ! port_available "$WEB_PORT"; then
    echo "Web port $WEB_PORT is still in use by an unrelated or uninspectable service." >&2
    echo "StreamHome did not stop that service. Resolve the conflict, then run ./start.sh; setup and dependencies do not need to be repeated." >&2
    exit 1
fi

echo "Starting StreamHome API on 127.0.0.1:8000..."
(
    cd "$ROOT_DIR/server"
    nohup "$BACKEND_PYTHON" main.py > "$ROOT_DIR/backend.log" 2>&1 &
    echo $! > "$RUN_DIR/backend.pid"
)

echo "Starting StreamHome web on 0.0.0.0:${WEB_PORT}..."
(
    cd "$ROOT_DIR/web"
    nohup env NODE_ENV=production WEB_PORT="$WEB_PORT" SETUP="$SETUP" PUBLIC_URL="$PUBLIC_URL" npm run server > "$ROOT_DIR/frontend.log" 2>&1 &
    echo $! > "$RUN_DIR/web.pid"
)

sleep 1
BACKEND_PID="$(cat "$RUN_DIR/backend.pid")"
WEB_PID="$(cat "$RUN_DIR/web.pid")"
if ! kill -0 "$BACKEND_PID" 2>/dev/null || ! kill -0 "$WEB_PID" 2>/dev/null; then
    "$ROOT_DIR/stop.sh" --quiet || true
    echo "A StreamHome process exited during startup. Review backend.log and frontend.log." >&2
    exit 1
fi

echo "StreamHome is running at http://localhost:${WEB_PORT}"
if [[ "$SETUP_ACTIVE" == true ]]; then
    echo "First-run setup is active."
    echo "Setup URL: http://localhost:${WEB_PORT}/setup"
    echo "One-time bootstrap code: ${STREAMHOME_SETUP_CODE}"
fi
echo "Logs: backend.log and frontend.log"
