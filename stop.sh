#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUN_DIR="$ROOT_DIR/.run"
ENV_FILE="$ROOT_DIR/.env"
QUIET=false
STARTUP_MODE=false
RECOVERY_PORTS=()

usage() {
    cat <<'EOF'
StreamHome Unix shutdown

Usage:
  ./stop.sh [--quiet] [--help]

Stops the backend and web process trees owned by this StreamHome installation,
including orphaned listeners left behind after stale or missing PID records.
EOF
}

read_env() {
    local key="$1" default_value="$2" value
    value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE" 2>/dev/null || true)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "${value:-$default_value}"
}

process_command() {
    ps -p "$1" -o command= 2>/dev/null || true
}

process_is_running() {
    local pid="$1" state
    kill -0 "$pid" 2>/dev/null || return 1
    state="$(ps -p "$pid" -o stat= 2>/dev/null | awk '{$1=$1; print}' || true)"
    [[ "$state" != Z* ]]
}

process_label() {
    local label
    label="$(ps -p "$1" -o comm= 2>/dev/null | awk '{$1=$1; print}' || true)"
    printf '%s' "${label:-unknown}"
}

process_cwd() {
    local pid="$1" cwd=""
    if [[ -L "/proc/$pid/cwd" ]]; then
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    elif command -v lsof >/dev/null 2>&1; then
        cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    fi
    printf '%s' "$cwd"
}

is_streamhome_process() {
    local pid="$1" cwd command
    process_is_running "$pid" || return 1
    cwd="$(process_cwd "$pid")"
    command="$(process_command "$pid")"

    case "$cwd" in
        "$ROOT_DIR/server"|"$ROOT_DIR/server/"*)
            [[ "$command" == *"main.py"* ]]
            ;;
        "$ROOT_DIR/web"|"$ROOT_DIR/web/"*)
            [[ "$command" == *"npm"*"run"*"server"* || "$command" == *"tsx"*"server.ts"* || "$command" == *"server.ts"* ]]
            ;;
        *)
            return 1
            ;;
    esac
}

child_pids() {
    local parent_pid="$1"
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -P "$parent_pid" 2>/dev/null || true
    else
        ps -eo pid=,ppid= 2>/dev/null | awk -v parent="$parent_pid" '$2 == parent {print $1}'
    fi
}

collect_process_tree() {
    local pid="$1" child
    while IFS= read -r child; do
        [[ "$child" =~ ^[0-9]+$ ]] && collect_process_tree "$child"
    done < <(child_pids "$pid")
    printf '%s\n' "$pid"
}

stop_process_tree() {
    local pid="$1" tree candidate
    tree="$(collect_process_tree "$pid")"
    while IFS= read -r candidate; do
        [[ "$candidate" =~ ^[0-9]+$ ]] && kill "$candidate" 2>/dev/null || true
    done <<< "$tree"

    for _ in {1..30}; do
        local running=false
        while IFS= read -r candidate; do
            if [[ "$candidate" =~ ^[0-9]+$ ]] && process_is_running "$candidate"; then
                running=true
                break
            fi
        done <<< "$tree"
        [[ "$running" == false ]] && return 0
        sleep 0.1
    done

    while IFS= read -r candidate; do
        if [[ "$candidate" =~ ^[0-9]+$ ]] && process_is_running "$candidate"; then
            kill -9 "$candidate" 2>/dev/null || true
        fi
    done <<< "$tree"

    for _ in {1..10}; do
        local running=false
        while IFS= read -r candidate; do
            if [[ "$candidate" =~ ^[0-9]+$ ]] && process_is_running "$candidate"; then
                running=true
                break
            fi
        done <<< "$tree"
        [[ "$running" == false ]] && return 0
        sleep 0.1
    done
}

listener_pids() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
    elif command -v ss >/dev/null 2>&1; then
        ss -H -ltnp "sport = :$port" 2>/dev/null \
            | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
            | sort -u
    elif command -v fuser >/dev/null 2>&1; then
        fuser "$port"/tcp 2>/dev/null \
            | tr ' ' '\n' \
            | sed -n '/^[0-9][0-9]*$/p' \
            | sort -u
    fi
}

report_unrelated_listener() {
    local port="$1" pid="$2" cwd
    cwd="$(process_cwd "$pid")"
    printf '[StreamHome] Port %s is owned by unrelated process PID %s (%s)' \
        "$port" "$pid" "$(process_label "$pid")" >&2
    if [[ -n "$cwd" ]]; then
        printf ' in %s' "$cwd" >&2
    fi
    printf '. It was not stopped.\n' >&2
}

recover_port() {
    local port="$1" pid found=false
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        found=true
        if is_streamhome_process "$pid"; then
            if [[ "$QUIET" == false || "$STARTUP_MODE" == true ]]; then
                printf '[StreamHome] Port %s is occupied by an earlier StreamHome process (PID %s). Stopping it...\n' "$port" "$pid"
            fi
            stop_process_tree "$pid"
        elif [[ "$QUIET" == false || "$STARTUP_MODE" == true ]]; then
            report_unrelated_listener "$port" "$pid"
        fi
    done < <(listener_pids "$port")
    [[ "$found" == true ]] || return 0
}

stop_recorded_process() {
    local name="$1" pid_file="$RUN_DIR/$name.pid" pid
    [[ -f "$pid_file" ]] || return 0
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && process_is_running "$pid"; then
        if is_streamhome_process "$pid"; then
            stop_process_tree "$pid"
            [[ "$QUIET" == true ]] || printf '[StreamHome] Stopped %s.\n' "$name"
        elif [[ "$QUIET" == false ]]; then
            printf '[StreamHome] Skipped stale %s PID record %s because it no longer belongs to this installation.\n' "$name" "$pid" >&2
        fi
    fi
    rm -f "$pid_file"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --quiet)
            QUIET=true
            ;;
        --startup)
            QUIET=true
            STARTUP_MODE=true
            ;;
        --recover-port)
            shift
            if [[ $# -eq 0 || ! "$1" =~ ^[0-9]+$ || "$1" -lt 1 || "$1" -gt 65535 ]]; then
                printf 'Invalid or missing port for --recover-port.\n' >&2
                exit 1
            fi
            RECOVERY_PORTS+=("$1")
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ ${#RECOVERY_PORTS[@]} -eq 0 ]]; then
    RECOVERY_PORTS=(8000 "$(read_env WEB_PORT 3000)")
fi

[[ "$QUIET" == true ]] || echo "Stopping StreamHome processes..."
stop_recorded_process web
stop_recorded_process backend
for port in "${RECOVERY_PORTS[@]}"; do
    recover_port "$port"
done
[[ "$QUIET" == true ]] || echo "StreamHome stopped."
