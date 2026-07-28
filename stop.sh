#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUN_DIR="$ROOT_DIR/.run"
ENV_FILE="$ROOT_DIR/.env"
LIFECYCLE_LOCK="$RUN_DIR/lifecycle.lock"
QUIET=false
STARTUP_MODE=false
LOCK_HELD=false
LOCK_ACQUIRED=false
STOP_FAILED=false
RECOVERY_PORTS=()

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

process_command() {
    ps -p "$1" -o command= 2>/dev/null || true
}

process_is_running() {
    local pid="$1" state
    kill -0 "$pid" 2>/dev/null || return 1
    state="$(ps -p "$pid" -o stat= 2>/dev/null | awk '{$1=$1; print}' || true)"
    [[ "$state" != Z* ]]
}

process_start_marker() {
    local marker
    marker="$(ps -p "$1" -o lstart= 2>/dev/null | awk '{$1=$1; print}' || true)"
    printf '%s' "${marker:-unknown}"
}

process_identity_matches() {
    local pid="$1" expected="$2"
    process_is_running "$pid" || return 1
    [[ "$expected" == "unknown" || "$(process_start_marker "$pid")" == "$expected" ]]
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
            [[ "$command" == *"main.py"* || "$command" == *"uvicorn"*"main:app"* ]]
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
    local pid="$1" child marker
    while IFS= read -r child; do
        [[ "$child" =~ ^[0-9]+$ ]] && collect_process_tree "$child"
    done < <(child_pids "$pid")
    marker="$(process_start_marker "$pid")"
    printf '%s|%s\n' "$pid" "$marker"
}

tree_is_running() {
    local tree="$1" candidate marker
    while IFS='|' read -r candidate marker; do
        if [[ "$candidate" =~ ^[0-9]+$ ]] && process_identity_matches "$candidate" "$marker"; then
            return 0
        fi
    done <<< "$tree"
    return 1
}

stop_process_tree() {
    local pid="$1" tree candidate marker
    tree="$(collect_process_tree "$pid")"
    while IFS='|' read -r candidate marker; do
        if [[ "$candidate" =~ ^[0-9]+$ ]] && process_identity_matches "$candidate" "$marker"; then
            kill "$candidate" 2>/dev/null || true
        fi
    done <<< "$tree"

    for _ in {1..30}; do
        tree_is_running "$tree" || return 0
        sleep 0.1
    done

    while IFS='|' read -r candidate marker; do
        if [[ "$candidate" =~ ^[0-9]+$ ]] && process_identity_matches "$candidate" "$marker"; then
            kill -9 "$candidate" 2>/dev/null || true
        fi
    done <<< "$tree"

    for _ in {1..10}; do
        tree_is_running "$tree" || return 0
        sleep 0.1
    done
    return 1
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
    local port="$1" pid
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        if is_streamhome_process "$pid"; then
            if [[ "$QUIET" == false || "$STARTUP_MODE" == true ]]; then
                printf '[StreamHome] Port %s is occupied by an earlier StreamHome process (PID %s). Stopping it...\n' "$port" "$pid"
            fi
            if ! stop_process_tree "$pid"; then
                printf '[StreamHome] Process tree rooted at PID %s survived shutdown and still owns StreamHome runtime state.\n' "$pid" >&2
                STOP_FAILED=true
            fi
        elif [[ "$QUIET" == false || "$STARTUP_MODE" == true ]]; then
            report_unrelated_listener "$port" "$pid"
        fi
    done < <(listener_pids "$port")
}

stop_recorded_process() {
    local name="$1" pid_file pid
    pid_file="$RUN_DIR/$name.pid"
    [[ -f "$pid_file" ]] || return 0
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && process_is_running "$pid"; then
        if is_streamhome_process "$pid"; then
            if stop_process_tree "$pid"; then
                rm -f -- "$pid_file"
                [[ "$QUIET" == true ]] || printf '[StreamHome] Stopped %s.\n' "$name"
            else
                printf '[StreamHome] Failed to stop %s process tree rooted at PID %s; its PID record was preserved.\n' "$name" "$pid" >&2
                STOP_FAILED=true
            fi
            return 0
        fi
        if [[ "$QUIET" == false ]]; then
            printf '[StreamHome] Skipped stale %s PID record %s because it no longer belongs to this installation.\n' "$name" "$pid" >&2
        fi
    fi
    rm -f -- "$pid_file"
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
                RECOVERY_PORTS+=("$1")
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
            RECOVERY_PORTS=(8000 "$configured_port")
        else
            printf '[StreamHome] WEB_PORT is invalid; only recorded processes and API port 8000 can be recovered safely.\n' >&2
            RECOVERY_PORTS=(8000)
            STOP_FAILED=true
        fi
    fi

    [[ "$QUIET" == true ]] || printf 'Stopping StreamHome processes...\n'
    stop_recorded_process web
    stop_recorded_process backend
    local port
    for port in "${RECOVERY_PORTS[@]}"; do
        recover_port "$port"
    done

    if [[ "$STOP_FAILED" == true ]]; then
        printf '[StreamHome] Shutdown did not complete cleanly.\n' >&2
        return 1
    fi
    [[ "$QUIET" == true ]] || printf 'StreamHome stopped.\n'
}

main "$@"
