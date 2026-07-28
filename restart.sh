#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_PATH="$ROOT_DIR/restart.sh"
RESTART_LOG="$ROOT_DIR/restart.log"
RESTART_DELAY_SECONDS="${STREAMHOME_RESTART_DELAY_SECONDS:-2}"

if ! [[ "$RESTART_DELAY_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    RESTART_DELAY_SECONDS=2
fi

run_restart() {
    sleep "$RESTART_DELAY_SECONDS"
    printf '[StreamHome Restart] Starting lifecycle restart at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    exec bash "$ROOT_DIR/start.sh"
}

queue_restart() {
    printf '[StreamHome Restart] Detached restart handoff queued at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        >> "$RESTART_LOG"
    nohup bash "$SCRIPT_PATH" --execute >> "$RESTART_LOG" 2>&1 < /dev/null &
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
