#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_PATH="$ROOT_DIR/restart.sh"
RESTART_LOG="$ROOT_DIR/restart.log"
RUN_DIR="$ROOT_DIR/.run"
RESTART_LOCK="$RUN_DIR/restart.lock"
RESTART_DELAY_SECONDS="${STREAMHOME_RESTART_DELAY_SECONDS:-2}"
LOCK_ACQUIRED=false

if ! [[ "$RESTART_DELAY_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    RESTART_DELAY_SECONDS=2
fi

release_restart_lock() {
    if [[ "$LOCK_ACQUIRED" == true && -d "$RESTART_LOCK" ]]; then
        rm -f -- "$RESTART_LOCK/owner.pid"
        rm -f -- "$RESTART_LOCK/owner.start"
        rmdir -- "$RESTART_LOCK" 2>/dev/null || true
        LOCK_ACQUIRED=false
    fi
}
trap release_restart_lock EXIT

process_start_marker() {
    ps -p "$1" -o lstart= 2>/dev/null | awk '{$1=$1; print}' || true
}

acquire_restart_lock() {
    local owner="" expected_start="" actual_start=""
    mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    if mkdir "$RESTART_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$RESTART_LOCK/owner.pid"
        process_start_marker "$$" > "$RESTART_LOCK/owner.start"
        LOCK_ACQUIRED=true
        return 0
    fi
    owner="$(cat "$RESTART_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
        expected_start="$(cat "$RESTART_LOCK/owner.start" 2>/dev/null || true)"
        actual_start="$(process_start_marker "$owner")"
        if [[ -z "$expected_start" || "$expected_start" == "$actual_start" ]]; then
            printf '[StreamHome Restart] Another detached restart is already pending under PID %s.\n' "$owner"
            return 1
        fi
    fi
    rm -f -- "$RESTART_LOCK/owner.pid"
    rm -f -- "$RESTART_LOCK/owner.start"
    rmdir -- "$RESTART_LOCK" 2>/dev/null || return 1
    mkdir "$RESTART_LOCK" || return 1
    printf '%s\n' "$$" > "$RESTART_LOCK/owner.pid"
    process_start_marker "$$" > "$RESTART_LOCK/owner.start"
    LOCK_ACQUIRED=true
}

run_restart() {
    acquire_restart_lock || return 0
    sleep "$RESTART_DELAY_SECONDS"
    printf '[StreamHome Restart] Starting lifecycle restart at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    release_restart_lock
    exec bash "$ROOT_DIR/start.sh"
}

queue_restart() {
    printf '[StreamHome Restart] Detached restart handoff queued at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        >> "$RESTART_LOG"
    nohup env \
        -u STREAMHOME_INSTANCE_ROOT \
        -u STREAMHOME_INSTANCE_TOKEN \
        -u STREAMHOME_SERVICE \
        setsid bash "$SCRIPT_PATH" --execute >> "$RESTART_LOG" 2>&1 < /dev/null &
}

case "${1:-}" in
    "")
        queue_restart
        ;;
    --execute)
        run_restart
        ;;
    --help|-h)
        cat <<'EOF'
StreamHome detached restart handoff

Usage:
  ./restart.sh

Queues a delayed lifecycle restart outside the current backend process tree.
Restart progress and failures are appended to restart.log.
EOF
        ;;
    *)
        printf 'Unknown argument: %s\n' "$1" >&2
        exit 1
        ;;
esac
