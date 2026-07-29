#!/usr/bin/env bash
set -uo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="${STREAMHOME_ROOT_OVERRIDE:-$SCRIPT_ROOT}"
ROOT_DIR="$(cd "$ROOT_DIR" && pwd -P)"
RUN_DIR="$ROOT_DIR/.run"
ENV_FILE="$ROOT_DIR/.env"
LIFECYCLE_LOCK="$RUN_DIR/lifecycle.lock"
RUNTIME_CONTROL="$SCRIPT_ROOT/server/scratch/runtime_control.py"
QUIET=false
STARTUP_MODE=false
LOCK_HELD=false
LOCK_ACQUIRED=false
RECOVERY_PORTS=()

append_recovery_port() {
    local candidate="$1" existing
    for existing in "${RECOVERY_PORTS[@]}"; do
        [[ "$existing" == "$candidate" ]] && return 0
    done
    RECOVERY_PORTS+=("$candidate")
}

usage() {
    cat <<'EOF'
StreamHome Linux shutdown

Usage:
  ./stop.sh [--quiet] [--help]

Stops the backend and web process trees owned by this StreamHome installation,
including orphaned listeners left behind after stale or missing PID records.
EOF
}

release_lifecycle_lock() {
    if [[ "$LOCK_ACQUIRED" == true && -d "$LIFECYCLE_LOCK" ]]; then
        rm -f -- "$LIFECYCLE_LOCK/owner.pid"
        rmdir -- "$LIFECYCLE_LOCK" 2>/dev/null || true
        LOCK_ACQUIRED=false
    fi
}
trap release_lifecycle_lock EXIT

acquire_lifecycle_lock() {
    local owner=""
    if [[ "$LOCK_HELD" == true ]]; then
        owner="$(cat "$LIFECYCLE_LOCK/owner.pid" 2>/dev/null || true)"
        if [[ ! "$owner" =~ ^[0-9]+$ || "$owner" -ne "$PPID" ]] || ! kill -0 "$owner" 2>/dev/null; then
            printf '[StreamHome] --lock-held is valid only when invoked by the lifecycle-lock owner.\n' >&2
            exit 1
        fi
        return 0
    fi
    [[ -d "$RUN_DIR" ]] || mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    if mkdir "$LIFECYCLE_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$LIFECYCLE_LOCK/owner.pid"
        LOCK_ACQUIRED=true
        return 0
    fi
    owner="$(cat "$LIFECYCLE_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
        printf '[StreamHome] Another start or stop operation is already running.\n' >&2
        exit 1
    fi
    rm -f -- "$LIFECYCLE_LOCK/owner.pid"
    if ! rmdir -- "$LIFECYCLE_LOCK" 2>/dev/null; then
        printf '[StreamHome] The stale lifecycle lock could not be removed: %s\n' "$LIFECYCLE_LOCK" >&2
        exit 1
    fi
    if ! mkdir "$LIFECYCLE_LOCK"; then
        printf '[StreamHome] Could not acquire the lifecycle lock.\n' >&2
        exit 1
    fi
    printf '%s\n' "$$" > "$LIFECYCLE_LOCK/owner.pid"
    LOCK_ACQUIRED=true
}

read_env() {
    local key="$1" default_value="$2" value
    value="$(awk -F= -v key="$key" '
        {
            line=$0
            sub(/^[[:space:]]*export[[:space:]]+/, "", line)
            split(line, parts, "=")
            candidate=parts[1]
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", candidate)
            if (candidate == key) {
                sub(/^[^=]*=/, "", line)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
                print line
                exit
            }
        }
    ' "$ENV_FILE" 2>/dev/null || true)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s' "${value:-$default_value}"
}

select_python() {
    if [[ -x "$ROOT_DIR/venv/bin/python" ]] \
        && "$ROOT_DIR/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
        RUNTIME_PYTHON="$ROOT_DIR/venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        RUNTIME_PYTHON="$(command -v python3)"
    else
        printf '[StreamHome] Python 3 is unavailable; runtime ownership cannot be inspected safely.\n' >&2
        return 1
    fi
}

select_runtime_control() {
    if [[ ! -f "$RUNTIME_CONTROL" ]]; then
        RUNTIME_CONTROL="$ROOT_DIR/server/scratch/runtime_control.py"
    fi
    if [[ ! -f "$RUNTIME_CONTROL" ]]; then
        printf '[StreamHome] Runtime ownership controller is missing: %s\n' "$RUNTIME_CONTROL" >&2
        return 1
    fi
}

run_runtime_stop() {
    local -a arguments=(stop --root "$ROOT_DIR")
    local port
    for port in "${RECOVERY_PORTS[@]}"; do
        arguments+=(--port "$port")
    done
    [[ "$QUIET" == true ]] && arguments+=(--quiet)
    [[ "$STARTUP_MODE" == true ]] && arguments+=(--startup)
    "$RUNTIME_PYTHON" "$RUNTIME_CONTROL" "${arguments[@]}"
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --quiet)
                QUIET=true
                ;;
            --startup)
                QUIET=true
                STARTUP_MODE=true
                ;;
            --lock-held)
                LOCK_HELD=true
                ;;
            --recover-port)
                shift
                if [[ $# -eq 0 || ! "$1" =~ ^[0-9]+$ || "$1" -lt 1 || "$1" -gt 65535 ]]; then
                    printf 'Invalid or missing port for --recover-port.\n' >&2
                    exit 1
                fi
                append_recovery_port "$1"
                ;;
            --help|-h)
                usage
                return 0
                ;;
            *)
                printf 'Unknown argument: %s\n' "$1" >&2
                exit 1
                ;;
        esac
        shift
    done

    acquire_lifecycle_lock
    if [[ ${#RECOVERY_PORTS[@]} -eq 0 ]]; then
        local configured_port
        configured_port="$(read_env WEB_PORT 3000)"
        if [[ "$configured_port" =~ ^[0-9]+$ ]] && (( 10#$configured_port >= 1 && 10#$configured_port <= 65535 )); then
            configured_port="$((10#$configured_port))"
            RECOVERY_PORTS=()
            append_recovery_port 8000
            append_recovery_port "$configured_port"
        else
            printf '[StreamHome] WEB_PORT is invalid; shutdown cannot verify the configured web listener.\n' >&2
            return 1
        fi
    fi

    select_python || return 1
    select_runtime_control || return 1
    [[ "$QUIET" == true ]] || printf 'Stopping StreamHome processes...\n'
    if ! run_runtime_stop; then
        printf '[StreamHome] Shutdown did not complete cleanly.\n' >&2
        return 1
    fi
    rm -f -- "$RUN_DIR/maintenance.start"
    [[ "$QUIET" == true ]] || printf 'StreamHome stopped.\n'
}

main "$@"
