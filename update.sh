#!/usr/bin/env bash
set -uo pipefail

ORIGINAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$ORIGINAL_ROOT"
RUN_DIR=""
STATUS_FILE=""
UPDATE_LOG=""
UPDATE_LOCK=""
STAGING_DIR=""
MAINTENANCE_PID=""
LOCK_ACQUIRED=false
PRESERVE_MAINTENANCE=false
TARGET_COMMIT=""
OLD_COMMIT=""
AUTOMATIC=false
HANDOFF_FILE=""
UPDATE_BRANCH="main"
WEB_PORT=3000

log() {
    printf '[StreamHome Update] %s\n' "$1"
}

read_env() {
    local file="$1" key="$2" default_value="$3" value
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
    ' "$file" 2>/dev/null || true)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s' "${value:-$default_value}"
}

write_state() {
    local phase="$1" message="$2" error="${3:-}" failed_target="${4:-}" current_commit="${5:-$OLD_COMMIT}"
    python3 - "$STATUS_FILE" "$phase" "$message" "$error" "$failed_target" "$current_commit" "$TARGET_COMMIT" "$AUTOMATIC" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
try:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
except (OSError, ValueError, TypeError):
    payload = {}
phase, message, error, failed_target, current, target, automatic = sys.argv[2:]
now = time.time()
payload.update({
    "phase": phase,
    "message": message,
    "error": error,
    "failed_target": failed_target,
    "current_commit": current,
    "target_commit": target,
    "automatic": automatic == "true",
    "previous_commit": payload.get("previous_commit") or current,
    "update_available": phase in {"update_available", "queued"} and not failed_target,
    "updated_at": now,
})
if phase in {"succeeded", "failed", "rolled_back", "rollback_failed"}:
    payload["finished_at"] = now
if phase == "succeeded":
    payload["last_success_at"] = now
    payload["queued_at"] = None
path_tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
path_tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(path_tmp, path)
PY
}

release_lock() {
    if [[ "$LOCK_ACQUIRED" == true && -d "$UPDATE_LOCK" ]]; then
        rm -f -- "$UPDATE_LOCK/owner.pid"
        rmdir -- "$UPDATE_LOCK" 2>/dev/null || true
        LOCK_ACQUIRED=false
    fi
}

stop_maintenance() {
    if [[ "$MAINTENANCE_PID" =~ ^[0-9]+$ ]] && kill -0 "$MAINTENANCE_PID" 2>/dev/null; then
        kill "$MAINTENANCE_PID" 2>/dev/null || true
        wait "$MAINTENANCE_PID" 2>/dev/null || true
    fi
    MAINTENANCE_PID=""
}

cleanup() {
    if [[ "$PRESERVE_MAINTENANCE" == false ]]; then
        stop_maintenance
    fi
    if [[ -n "$HANDOFF_FILE" && "$HANDOFF_FILE" == "$RUN_DIR"/update-handoff.*.token ]]; then
        rm -f -- "$HANDOFF_FILE"
    fi
    if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
        rm -rf -- "$STAGING_DIR"
    fi
    release_lock
    if [[ "${BASH_SOURCE[0]}" == "$RUN_DIR"/update-controller.*.sh ]]; then
        rm -f -- "${BASH_SOURCE[0]}"
    fi
}

acquire_update_lock() {
    local owner=""
    mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    if mkdir "$UPDATE_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$UPDATE_LOCK/owner.pid"
        LOCK_ACQUIRED=true
        return 0
    fi
    owner="$(cat "$UPDATE_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
        return 1
    fi
    rm -f -- "$UPDATE_LOCK/owner.pid"
    rmdir -- "$UPDATE_LOCK" 2>/dev/null || return 1
    mkdir "$UPDATE_LOCK" || return 1
    printf '%s\n' "$$" > "$UPDATE_LOCK/owner.pid"
    LOCK_ACQUIRED=true
}

start_maintenance() {
    local python_bin="$ROOT_DIR/venv/bin/python"
    if [[ "$MAINTENANCE_PID" =~ ^[0-9]+$ ]] && kill -0 "$MAINTENANCE_PID" 2>/dev/null; then
        return 0
    fi
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    nohup "$python_bin" "$ROOT_DIR/server/scratch/maintenance_server.py" --port "$WEB_PORT" \
        >> "$UPDATE_LOG" 2>&1 &
    MAINTENANCE_PID=$!
    for _ in {1..30}; do
        kill -0 "$MAINTENANCE_PID" 2>/dev/null || return 1
        if "$python_bin" - "$WEB_PORT" <<'PY'
import sys
import urllib.error
import urllib.request

try:
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        f"http://127.0.0.1:{sys.argv[1]}/", timeout=0.5
    )
except urllib.error.HTTPError as error:
    raise SystemExit(0 if error.code == 503 else 1)
except Exception:
    raise SystemExit(1)
PY
        then
            return 0
        fi
        sleep 0.2
    done
    return 1
}

preflight_target() {
    local staged_checkout="$STAGING_DIR/checkout" staged_head
    log "Cloning the target into an isolated preflight checkout."
    git clone --depth 1 --branch "$UPDATE_BRANCH" "https://github.com/StreamHome/StreamHome.git" "$staged_checkout" || return 1
    staged_head="$(git -C "$staged_checkout" rev-parse HEAD 2>/dev/null || true)"
    [[ "$staged_head" == "$TARGET_COMMIT" ]] || {
        log "The remote branch changed during preflight; refusing a stale target."
        return 1
    }
    (
        cd "$staged_checkout"
        ./setup.sh --no-start --skip-system-packages
        ./test.sh --syntax-only
        ./venv/bin/python -m compileall -q server
        PYTHONPATH=server ./venv/bin/python server/scratch/test_update_system.py
    ) || return 1
    log "Isolated dependency, syntax, and production-build preflight passed."
}

wait_for_idle_handoff() {
    local code handoff_token
    handoff_token="$(cat "$HANDOFF_FILE" 2>/dev/null || true)"
    [[ -n "$handoff_token" ]] || return 1
    write_state "waiting_for_idle" "Preflight passed. Waiting for the server to become idle again."
    while true; do
        code="$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
            -H "X-StreamHome-Update-Handoff: $handoff_token" \
            -X POST "http://127.0.0.1:8000/api/update/handoff" 2>/dev/null || true)"
        case "$code" in
            200)
                rm -f -- "$HANDOFF_FILE"
                log "The backend reserved an idle maintenance cutover."
                return 0
                ;;
            403)
                rm -f -- "$HANDOFF_FILE"
                log "The backend rejected the update handoff authorization."
                return 1
                ;;
            409)
                sleep 30
                ;;
            *)
                log "The backend became unavailable before approving the cutover."
                return 1
                ;;
        esac
    done
}

create_database_checkpoint() {
    local database="$ROOT_DIR/server/database.db" checkpoint="$RUN_DIR/pre-update-database.db"
    [[ -f "$database" ]] || {
        log "The standardized database is missing."
        return 1
    }
    python3 - "$database" "$checkpoint" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
target_connection = sqlite3.connect(temporary)
try:
    source_connection.backup(target_connection)
    result = target_connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("database checkpoint integrity check failed")
finally:
    target_connection.close()
    source_connection.close()
os.replace(temporary, target)
PY
}

restore_database_checkpoint() {
    local database="$ROOT_DIR/server/database.db" checkpoint="$RUN_DIR/pre-update-database.db"
    [[ -f "$checkpoint" ]] || return 1
    python3 - "$checkpoint" "$database" <<'PY'
import os
import shutil
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
try:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("database checkpoint integrity check failed")
finally:
    connection.close()
temporary = target.with_name(f"{target.name}.{os.getpid()}.rollback")
shutil.copy2(source, temporary)
os.replace(temporary, target)
for suffix in ("-wal", "-shm"):
    try:
        Path(f"{target}{suffix}").unlink()
    except FileNotFoundError:
        pass
PY
}

rollback_release() {
    log "Rolling back to known-working commit ${OLD_COMMIT:0:12}."
    write_state "rolling_back" "The update failed. Restoring the previous StreamHome release." "update_failed" "$TARGET_COMMIT"
    "$ROOT_DIR/stop.sh" --quiet || true
    start_maintenance || true
    git -C "$ROOT_DIR" reset --hard "$OLD_COMMIT" || return 1
    restore_database_checkpoint || return 1
    (
        cd "$ROOT_DIR"
        ./setup.sh --no-start --skip-system-packages
    ) || return 1
    stop_maintenance
    if ! "$ROOT_DIR/start.sh" --update-recovery-complete; then
        start_maintenance || true
        return 1
    fi
    write_state "rolled_back" "The update failed, and the previous healthy release was restored." "update_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
    return 0
}

record_rollback_failure() {
    write_state "rollback_failed" "The update and automatic rollback both failed. Review update.log." "rollback_failed" "$TARGET_COMMIT"
    PRESERVE_MAINTENANCE=true
}

recover_interrupted_release() {
    local phase current_head
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    UPDATE_LOCK="$RUN_DIR/update.lock"
    [[ -f "$STATUS_FILE" ]] || return 10
    mapfile -t recovery_state < <(python3 - "$STATUS_FILE" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
print(payload.get("phase", ""))
print(payload.get("previous_commit", ""))
print(payload.get("target_commit", ""))
PY
    )
    phase="${recovery_state[0]:-}"
    OLD_COMMIT="${recovery_state[1]:-}"
    TARGET_COMMIT="${recovery_state[2]:-}"
    case "$phase" in
        preflight|waiting_for_idle|stopping|installing|starting|rolling_back)
            ;;
        *)
            return 10
            ;;
    esac
    [[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ && "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || return 1
    if ! acquire_update_lock; then
        return 11
    fi
    trap cleanup EXIT
    current_head="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$current_head" == "$OLD_COMMIT" && ! -f "$RUN_DIR/pre-update-database.db" ]]; then
        write_state "failed" "An interrupted preflight was cleared; the installed release was unchanged." "update_interrupted" "$TARGET_COMMIT" "$OLD_COMMIT"
        return 10
    fi
    write_state "rolling_back" "Recovering an update interrupted before health verification." "update_interrupted" "$TARGET_COMMIT" "$current_head"
    git -C "$ROOT_DIR" reset --hard "$OLD_COMMIT" || {
        write_state "rollback_failed" "Interrupted-update source recovery failed. Review update.log." "rollback_failed" "$TARGET_COMMIT" "$current_head"
        return 1
    }
    if [[ -f "$RUN_DIR/pre-update-database.db" ]]; then
        restore_database_checkpoint || {
            write_state "rollback_failed" "Interrupted-update database recovery failed. Review update.log." "rollback_failed" "$TARGET_COMMIT" "$OLD_COMMIT"
            return 1
        }
    fi
    (
        cd "$ROOT_DIR"
        ./setup.sh --no-start --skip-system-packages
    ) || {
        write_state "rollback_failed" "Interrupted-update dependency recovery failed. Review update.log." "rollback_failed" "$TARGET_COMMIT" "$OLD_COMMIT"
        return 1
    }
    rm -f -- "$RUN_DIR/pre-update-database.db"
    write_state "rolled_back" "An interrupted update was restored before startup." "update_interrupted_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
    return 0
}

execute_update() {
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    UPDATE_LOCK="$RUN_DIR/update.lock"
    UPDATE_BRANCH="$(read_env "$ROOT_DIR/server/.env" UPDATE_BRANCH main)"
    WEB_PORT="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    trap cleanup EXIT

    if ! acquire_update_lock; then
        write_state "failed" "Another update controller already owns the update lock." "update_in_progress" "$TARGET_COMMIT"
        return 1
    fi
    STAGING_DIR="$(mktemp -d "$(dirname "$ROOT_DIR")/.streamhome-update.XXXXXX")" || {
        write_state "failed" "A temporary preflight checkout could not be created." "preflight_workspace_failed" "$TARGET_COMMIT"
        return 1
    }
    write_state "preflight" "Validating dependencies, server code, scripts, and production web assets."
    if ! preflight_target; then
        write_state "failed" "The candidate failed isolated preflight. The running installation was not changed." "preflight_failed" "$TARGET_COMMIT"
        return 1
    fi
    if ! wait_for_idle_handoff; then
        write_state "failed" "The updater could not reserve a verified-idle cutover." "idle_handoff_failed" "$TARGET_COMMIT"
        return 1
    fi

    write_state "stopping" "Stopping StreamHome and creating a verified recovery checkpoint."
    if ! "$ROOT_DIR/stop.sh" --quiet; then
        write_state "failed" "StreamHome did not stop cleanly. The update was not applied." "shutdown_failed" "$TARGET_COMMIT"
        return 1
    fi
    if ! create_database_checkpoint; then
        "$ROOT_DIR/start.sh" --update-recovery-complete || true
        write_state "failed" "The database recovery checkpoint failed. The update was not applied." "database_checkpoint_failed" "$TARGET_COMMIT"
        return 1
    fi
    if ! start_maintenance; then
        "$ROOT_DIR/start.sh" --update-recovery-complete || true
        write_state "failed" "The maintenance responder could not start. The update was not applied." "maintenance_start_failed" "$TARGET_COMMIT"
        return 1
    fi

    write_state "installing" "Installing the exact preflighted commit and rebuilding locked assets."
    if ! git -C "$ROOT_DIR" status --porcelain --untracked-files=normal | grep -q .; then
        :
    else
        log "The checkout became dirty before cutover."
        rollback_release || record_rollback_failure
        return 1
    fi
    if ! git -C "$ROOT_DIR" fetch origin "$UPDATE_BRANCH" \
        || ! git -C "$ROOT_DIR" cat-file -e "${TARGET_COMMIT}^{commit}" \
        || ! git -C "$ROOT_DIR" merge-base --is-ancestor "$OLD_COMMIT" "$TARGET_COMMIT" \
        || ! git -C "$ROOT_DIR" merge --ff-only "$TARGET_COMMIT" \
        || ! (
            cd "$ROOT_DIR"
            ./setup.sh --no-start --skip-system-packages
        )
    then
        rollback_release || record_rollback_failure
        return 1
    fi

    write_state "starting" "Starting and health-checking the updated API and web client." "" "" "$TARGET_COMMIT"
    stop_maintenance
    if ! "$ROOT_DIR/start.sh" --update-recovery-complete; then
        start_maintenance || true
        rollback_release || record_rollback_failure
        return 1
    fi
    if [[ "$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)" != "$TARGET_COMMIT" ]]; then
        start_maintenance || true
        rollback_release || record_rollback_failure
        return 1
    fi
    rm -f -- "$RUN_DIR/pre-update-database.db"
    write_state "succeeded" "Update installed successfully; both StreamHome services passed health checks." "" "" "$TARGET_COMMIT"
    log "Update completed successfully at ${TARGET_COMMIT:0:12}."
}

queue_update() {
    local controller
    RUN_DIR="$ROOT_DIR/.run"
    mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    controller="$RUN_DIR/update-controller.$$.sh"
    cp -- "$ROOT_DIR/update.sh" "$controller"
    chmod 700 "$controller"
    printf '[StreamHome Update] Detached update handoff queued at %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$ROOT_DIR/update.log"
    nohup bash "$controller" --execute "$TARGET_COMMIT" "$OLD_COMMIT" "$AUTOMATIC" "$HANDOFF_FILE" "$ROOT_DIR" \
        >> "$ROOT_DIR/update.log" 2>&1 < /dev/null &
}

case "${1:-}" in
    --queue)
        [[ $# -eq 5 ]] || exit 2
        TARGET_COMMIT="$2"
        OLD_COMMIT="$3"
        AUTOMATIC="$4"
        HANDOFF_FILE="$5"
        [[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ && "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 2
        [[ "$AUTOMATIC" == "true" || "$AUTOMATIC" == "false" ]] || exit 2
        [[ "$HANDOFF_FILE" == "$ROOT_DIR"/.run/update-handoff.*.token ]] || exit 2
        queue_update
        ;;
    --execute)
        [[ $# -eq 6 ]] || exit 2
        TARGET_COMMIT="$2"
        OLD_COMMIT="$3"
        AUTOMATIC="$4"
        HANDOFF_FILE="$5"
        ROOT_DIR="$(cd "$6" && pwd -P)"
        [[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ && "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 2
        [[ "$HANDOFF_FILE" == "$ROOT_DIR"/.run/update-handoff.*.token ]] || exit 2
        execute_update
        ;;
    --recover-interrupted)
        [[ $# -eq 2 ]] || exit 2
        ROOT_DIR="$(cd "$2" && pwd -P)"
        recover_interrupted_release
        ;;
    --help|-h)
        printf 'StreamHome detached, preflighted, health-gated update controller.\n'
        ;;
    *)
        printf 'Use the StreamHome admin center to manage updates.\n' >&2
        exit 2
        ;;
esac
