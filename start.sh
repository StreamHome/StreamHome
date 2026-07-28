#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$ROOT_DIR/.env"
RUN_DIR="$ROOT_DIR/.run"
LIFECYCLE_LOCK="$RUN_DIR/lifecycle.lock"
LOCK_ACQUIRED=false
STARTED_ANY=false
START_SUCCEEDED=false

usage() {
    cat <<'EOF'
StreamHome Linux startup

Usage:
  ./start.sh [--help]

Starts the FastAPI service on loopback port 8000 and the production web
service on the configured public web port.
EOF
}

fail() {
    printf '\n[StreamHome] ERROR: %s\n' "$1" >&2
    exit 1
}

release_lifecycle_lock() {
    if [[ "$LOCK_ACQUIRED" == true && -d "$LIFECYCLE_LOCK" ]]; then
        rm -f -- "$LIFECYCLE_LOCK/owner.pid"
        rmdir -- "$LIFECYCLE_LOCK" 2>/dev/null || true
        LOCK_ACQUIRED=false
    fi
}

cleanup() {
    if [[ "$START_SUCCEEDED" == false && "$STARTED_ANY" == true && -x "$ROOT_DIR/stop.sh" ]]; then
        "$ROOT_DIR/stop.sh" --quiet --lock-held >/dev/null 2>&1 || true
    fi
    release_lifecycle_lock
}
trap cleanup EXIT

acquire_lifecycle_lock() {
    local owner=""
    [[ -d "$RUN_DIR" ]] || mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    if mkdir "$LIFECYCLE_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$LIFECYCLE_LOCK/owner.pid"
        LOCK_ACQUIRED=true
        return 0
    fi
    owner="$(cat "$LIFECYCLE_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
        fail "Another StreamHome start or stop operation is already running."
    fi
    rm -f -- "$LIFECYCLE_LOCK/owner.pid"
    rmdir -- "$LIFECYCLE_LOCK" 2>/dev/null || fail "The stale lifecycle lock could not be removed: $LIFECYCLE_LOCK"
    mkdir "$LIFECYCLE_LOCK" || fail "Could not acquire the StreamHome lifecycle lock."
    printf '%s\n' "$$" > "$LIFECYCLE_LOCK/owner.pid"
    LOCK_ACQUIRED=true
}

select_python() {
    if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
        BACKEND_PYTHON="$ROOT_DIR/venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        BACKEND_PYTHON="$(command -v python3)"
    else
        fail "Python is unavailable. Run ./setup.sh first."
    fi
}

read_env() {
    "$BACKEND_PYTHON" - "$ENV_FILE" "$1" "$2" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
requested = sys.argv[2]
default = sys.argv[3]
if not path.is_file():
    print(default, end="")
    raise SystemExit

for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != requested:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value or default, end="")
    break
else:
    print(default, end="")
PY
}

validate_public_url() {
    "$BACKEND_PYTHON" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit

try:
    parsed = urlsplit(sys.argv[1])
    port = parsed.port
except ValueError:
    raise SystemExit(1)
valid = (
    parsed.scheme in {"http", "https"}
    and bool(parsed.hostname)
    and parsed.username is None
    and parsed.password is None
    and not parsed.query
    and not parsed.fragment
    and parsed.path in {"", "/"}
    and (port is None or 1 <= port <= 65535)
)
raise SystemExit(0 if valid else 1)
PY
}

public_url_is_loopback() {
    "$BACKEND_PYTHON" - "$1" <<'PY'
import ipaddress
import sys
from urllib.parse import urlsplit

hostname = (urlsplit(sys.argv[1]).hostname or "").strip().lower()
if hostname == "localhost":
    raise SystemExit(0)
try:
    raise SystemExit(0 if ipaddress.ip_address(hostname).is_loopback else 1)
except ValueError:
    raise SystemExit(1)
PY
}

detect_server_ip() {
    "$BACKEND_PYTHON" - <<'PY'
import ipaddress
import socket

candidates = []
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    probe.connect(("1.1.1.1", 80))
    candidates.append(probe.getsockname()[0])
except OSError:
    pass
finally:
    probe.close()

try:
    candidates.extend(
        entry[4][0]
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    )
except OSError:
    pass

for candidate in candidates:
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        continue
    if not address.is_loopback and not address.is_link_local and not address.is_unspecified:
        print(str(address))
        break
PY
}

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

wait_for_port_release() {
    local port="$1"
    for _ in {1..50}; do
        port_available "$port" && return 0
        sleep 0.1
    done
    return 1
}

write_pid_record() {
    local name="$1" pid="$2" temporary
    temporary="$(mktemp "$RUN_DIR/.${name}.pid.XXXXXX")"
    printf '%s\n' "$pid" > "$temporary"
    chmod 600 "$temporary" 2>/dev/null || true
    mv -f -- "$temporary" "$RUN_DIR/$name.pid"
}

start_backend() {
    local pid
    printf 'Starting StreamHome API on 127.0.0.1:8000...\n'
    STARTED_ANY=true
    (
        cd "$ROOT_DIR/server"
        if command -v setsid >/dev/null 2>&1; then
            nohup setsid env PYTHONUNBUFFERED=1 "$BACKEND_PYTHON" -m uvicorn main:app --host 127.0.0.1 --port 8000 \
                > "$ROOT_DIR/backend.log" 2>&1 < /dev/null &
        else
            nohup env PYTHONUNBUFFERED=1 "$BACKEND_PYTHON" -m uvicorn main:app --host 127.0.0.1 --port 8000 \
                > "$ROOT_DIR/backend.log" 2>&1 < /dev/null &
        fi
        pid=$!
        write_pid_record backend "$pid"
    )
}

start_web() {
    local pid
    printf 'Starting StreamHome web on 0.0.0.0:%s...\n' "$WEB_PORT"
    STARTED_ANY=true
    (
        cd "$ROOT_DIR/web"
        if command -v setsid >/dev/null 2>&1; then
            nohup setsid env NODE_ENV=production WEB_PORT="$WEB_PORT" SETUP="$SETUP" PUBLIC_URL="$PUBLIC_URL" \
                npm run server > "$ROOT_DIR/frontend.log" 2>&1 < /dev/null &
        else
            nohup env NODE_ENV=production WEB_PORT="$WEB_PORT" SETUP="$SETUP" PUBLIC_URL="$PUBLIC_URL" \
                npm run server > "$ROOT_DIR/frontend.log" 2>&1 < /dev/null &
        fi
        pid=$!
        write_pid_record web "$pid"
    )
}

endpoint_ready() {
    "$BACKEND_PYTHON" - "$1" "$2" <<'PY'
import json
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(sys.argv[1], timeout=1.5) as response:
        if response.status < 200 or response.status >= 400:
            raise SystemExit(1)
        if sys.argv[2] == "api":
            payload = json.loads(response.read().decode("utf-8"))
            raise SystemExit(0 if payload.get("status") == "ready" else 1)
except Exception:
    raise SystemExit(1)
PY
}

wait_for_services() {
    local backend_pid web_pid
    backend_pid="$(cat "$RUN_DIR/backend.pid")"
    web_pid="$(cat "$RUN_DIR/web.pid")"
    for _ in {1..60}; do
        kill -0 "$backend_pid" 2>/dev/null || return 1
        kill -0 "$web_pid" 2>/dev/null || return 1
        if endpoint_ready "http://127.0.0.1:8000/api/health" api \
            && endpoint_ready "http://127.0.0.1:${WEB_PORT}/" web; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

show_startup_logs() {
    printf '\n[StreamHome] Backend log tail:\n' >&2
    tail -n 25 "$ROOT_DIR/backend.log" 2>/dev/null >&2 || true
    printf '\n[StreamHome] Frontend log tail:\n' >&2
    tail -n 25 "$ROOT_DIR/frontend.log" 2>/dev/null >&2 || true
}

main() {
    if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
        usage
        return 0
    fi
    [[ $# -eq 0 ]] || fail "Unknown argument: $1 (use --help for usage)"

    select_python
    command -v npm >/dev/null 2>&1 || fail "npm is unavailable. Run ./setup.sh first."
    [[ -s "$ROOT_DIR/web/dist/index.html" ]] || fail "Production web assets are missing. Run ./setup.sh first."

    WEB_PORT="$(read_env WEB_PORT 3000)"
    SETUP="$(read_env SETUP false)"
    CONFIGURED_PUBLIC_URL="$(read_env PUBLIC_URL "")"
    if ! [[ "$WEB_PORT" =~ ^[0-9]+$ ]] || (( 10#$WEB_PORT < 1 || 10#$WEB_PORT > 65535 )); then
        fail "Invalid WEB_PORT in .env: $WEB_PORT"
    fi
    WEB_PORT="$((10#$WEB_PORT))"
    [[ "$WEB_PORT" -ne 8000 ]] || fail "WEB_PORT cannot be 8000 because the API uses that port."

    SETUP_NORMALIZED="$(printf '%s' "$SETUP" | tr '[:upper:]' '[:lower:]')"
    case "$SETUP_NORMALIZED" in
        true|1)
            SETUP_ACTIVE=false
            SETUP=true
            ;;
        false|0)
            SETUP_ACTIVE=true
            SETUP=false
            ;;
        *)
            fail "Invalid SETUP value in .env: $SETUP"
            ;;
    esac

    if [[ -n "$CONFIGURED_PUBLIC_URL" ]]; then
        validate_public_url "$CONFIGURED_PUBLIC_URL" || fail "PUBLIC_URL must be an http(s) origin without credentials, query parameters, fragments, or a path."
    fi
    PUBLIC_URL="$CONFIGURED_PUBLIC_URL"
    STREAMHOME_PUBLIC_URL_EXPLICIT=true
    if [[ -z "$PUBLIC_URL" ]] || { [[ "$SETUP_ACTIVE" == true ]] && public_url_is_loopback "$PUBLIC_URL"; }; then
        SERVER_IP="$(detect_server_ip)"
        if [[ -n "$SERVER_IP" ]]; then
            PUBLIC_URL="http://${SERVER_IP}:${WEB_PORT}"
        else
            PUBLIC_URL="http://localhost:${WEB_PORT}"
            printf 'Warning: no reachable server IP was detected. Set PUBLIC_URL in .env before opening setup remotely.\n' >&2
        fi
        STREAMHOME_PUBLIC_URL_EXPLICIT=false
    fi
    PUBLIC_URL="${PUBLIC_URL%/}"
    validate_public_url "$PUBLIC_URL" || fail "The resolved PUBLIC_URL is invalid: $PUBLIC_URL"
    export WEB_PORT SETUP PUBLIC_URL STREAMHOME_PUBLIC_URL_EXPLICIT

    acquire_lifecycle_lock
    "$ROOT_DIR/stop.sh" --startup --lock-held

    wait_for_port_release 8000 || fail "API port 8000 is still in use by an unrelated or uninspectable service. StreamHome did not stop it."
    wait_for_port_release "$WEB_PORT" || fail "Web port $WEB_PORT is still in use by an unrelated or uninspectable service. StreamHome did not stop it."

    if [[ "$SETUP_ACTIVE" == true ]]; then
        STREAMHOME_SETUP_CODE="$("$BACKEND_PYTHON" -c 'import secrets; print(secrets.token_urlsafe(18))')"
        export STREAMHOME_SETUP_CODE
    fi

    start_backend
    start_web
    if ! wait_for_services; then
        show_startup_logs
        fail "StreamHome did not become healthy within 30 seconds. The partial startup was stopped."
    fi

    START_SUCCEEDED=true
    printf '\nStreamHome is running at %s\n' "$PUBLIC_URL"
    if [[ "$SETUP_ACTIVE" == true ]]; then
        printf 'First-run setup is active.\n'
        printf 'Setup URL: %s/setup\n' "$PUBLIC_URL"
        printf 'One-time bootstrap code: %s\n' "$STREAMHOME_SETUP_CODE"
    fi
    printf 'Logs: backend.log and frontend.log\n'
}

main "$@"
