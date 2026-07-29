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
MAINTENANCE_PID_FILE=""
MAINTENANCE_START_FILE=""
LOCK_ACQUIRED=false
UPDATE_HEARTBEAT_PID=""
PRESERVE_MAINTENANCE=false
PRESERVE_RECOVERY_ARTIFACTS=false
CUTOVER_STARTED=false
RUNTIME_HEALTHY=true
RECOVERY_IN_PROGRESS=false
WEB_ARTIFACTS_SWAPPED=false
WEB_DEPENDENCIES_SWAPPED=false
PYTHON_DEPENDENCIES_CHANGED=true
WEB_DEPENDENCIES_CHANGED=true
WEB_BUILD_REQUIRED=true
TRANSACTION_ID=""
LEASE_FILE=""
CONTROLLER_START_TICKS=""
TARGET_COMMIT=""
OLD_COMMIT=""
AUTOMATIC=false
HANDOFF_FILE=""
UPDATE_BRANCH="main"
WEB_PORT=3000
MANUAL_CUTOVER=false
START_AFTER_UPDATE=true
PUBLIC_URL=""
PUBLIC_ORIGIN_WAS_READY=false

log() {
    printf '[StreamHome Update] %s\n' "$1"
}

process_start_ticks() {
    local pid="$1"
    if [[ -r "/proc/$pid/stat" ]]; then
        python3 - "$pid" <<'PY'
import sys
from pathlib import Path

try:
    line = Path(f"/proc/{int(sys.argv[1])}/stat").read_text(encoding="utf-8")
    print(line[line.rfind(")") + 2:].split()[19])
except (IndexError, OSError, ValueError):
    pass
PY
        return
    fi
    ps -p "$pid" -o lstart= 2>/dev/null | awk '{$1=$1; print}' || true
}

process_command() {
    ps -p "$1" -o command= 2>/dev/null || true
}

process_cwd() {
    if [[ -L "/proc/$1/cwd" ]]; then
        readlink "/proc/$1/cwd" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
        lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
    fi
}

maintenance_process_matches() {
    local pid="$1" expected_start="" command cwd actual_start
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
    command="$(process_command "$pid")"
    cwd="$(process_cwd "$pid")"
    [[ "$command" == *"/server/scratch/maintenance_server.py"* ]] || return 1
    case "$cwd" in
        "$ROOT_DIR"|"$ROOT_DIR/"*) ;;
        *) return 1 ;;
    esac
    expected_start="$(cat "$MAINTENANCE_START_FILE" 2>/dev/null || true)"
    if [[ -n "$expected_start" ]]; then
        actual_start="$(process_start_ticks "$pid")"
        [[ -n "$actual_start" && "$actual_start" == "$expected_start" ]] || return 1
    fi
}

new_transaction_id() {
    python3 - <<'PY'
import uuid
print(uuid.uuid4().hex)
PY
}

write_update_lease() {
    [[ -n "$LEASE_FILE" && -n "$TRANSACTION_ID" ]] || return 0
    python3 - "$LEASE_FILE" "$TRANSACTION_ID" "$$" "$CONTROLLER_START_TICKS" "$TARGET_COMMIT" "$OLD_COMMIT" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "transaction_id": sys.argv[2],
    "controller_pid": int(sys.argv[3]),
    "controller_start_ticks": sys.argv[4],
    "target_commit": sys.argv[5],
    "previous_commit": sys.argv[6],
    "heartbeat_at": time.time(),
}
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
}

remove_owned_lease() {
    [[ -n "$LEASE_FILE" && -f "$LEASE_FILE" ]] || return 0
    python3 - "$LEASE_FILE" "$TRANSACTION_ID" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, TypeError, ValueError):
    payload = {}
if not sys.argv[2] or payload.get("transaction_id") == sys.argv[2]:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
PY
}

record_diagnostics() {
    local phase="$1" error="$2"
    [[ -n "$RUN_DIR" ]] || return 0
    python3 - "$RUN_DIR/update-diagnostics.json" "$STATUS_FILE" "$ROOT_DIR" "$phase" "$error" "$WEB_PORT" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

destination = Path(sys.argv[1])
state_path = Path(sys.argv[2])
root = Path(sys.argv[3])
phase = sys.argv[4]
error = sys.argv[5]
web_port = sys.argv[6]

try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except (OSError, TypeError, ValueError):
    state = {}

def tail(name: str, lines: int = 80) -> list[str]:
    try:
        return (root / name).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []

pid_records = {}
for name in ("maintenance", "backend", "web"):
    path = root / ".run" / f"{name}.pid"
    try:
        pid_records[name] = path.read_text(encoding="utf-8").strip()
    except OSError:
        pid_records[name] = ""

diagnostic_id = f"{str(state.get('transaction_id') or 'update')[:12]}-{int(time.time())}"
payload = {
    "diagnostic_id": diagnostic_id,
    "captured_at": time.time(),
    "phase": phase,
    "error": error,
    "web_port": web_port,
    "controller_pid": os.getppid(),
    "state": state,
    "pid_records": pid_records,
    "backend_log": tail("backend.log"),
    "frontend_log": tail("frontend.log"),
    "update_log": tail("update.log", 120),
}
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, destination)
state["diagnostic_id"] = diagnostic_id
state_temporary = state_path.with_name(f"{state_path.name}.{os.getpid()}.diagnostic.tmp")
state_temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(state_temporary, state_path)
PY
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

public_origin_matches() {
    local expected_commit="$1" attempts="${2:-1}"
    [[ "$PUBLIC_URL" == http://* || "$PUBLIC_URL" == https://* ]] || return 1
    python3 - "$PUBLIC_URL" "${expected_commit:0:12}" "$attempts" <<'PY'
import json
import sys
import time
import urllib.parse
import urllib.request

public_url = sys.argv[1].rstrip("/")
expected = sys.argv[2]
attempts = max(1, int(sys.argv[3]))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(attempts):
    query = urllib.parse.urlencode({"update_probe": str(time.time_ns())})
    request = urllib.request.Request(
        f"{public_url}/api/health?{query}",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
            web_build = str(response.headers.get("X-StreamHome-Web-Build") or "")
            api_build = str(payload.get("buildId") or "")
            if response.status < 400 and payload.get("status") == "ready" and web_build == expected and api_build == expected:
                raise SystemExit(0)
    except Exception:
        pass
    if attempt + 1 < attempts:
        time.sleep(2)
raise SystemExit(1)
PY
}

write_state() {
    local phase="$1" message="$2" error="${3:-}" failed_target="${4:-}" current_commit="${5:-$OLD_COMMIT}"
    python3 - "$STATUS_FILE" "$phase" "$message" "$error" "$failed_target" "$current_commit" "$TARGET_COMMIT" "$AUTOMATIC" "$TRANSACTION_ID" "$STAGING_DIR" "$WEB_ARTIFACTS_SWAPPED" "$WEB_DEPENDENCIES_SWAPPED" "$PYTHON_DEPENDENCIES_CHANGED" "$WEB_DEPENDENCIES_CHANGED" "$WEB_BUILD_REQUIRED" "$$" "$CONTROLLER_START_TICKS" <<'PY'
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
(
    phase,
    message,
    error,
    failed_target,
    current,
    target,
    automatic,
    transaction_id,
    staging_dir,
    web_artifacts_swapped,
    web_dependencies_swapped,
    python_dependencies_changed,
    web_dependencies_changed,
    web_build_required,
    controller_pid,
    controller_start_ticks,
) = sys.argv[2:]
now = time.time()
previous_transaction = str(payload.get("transaction_id") or "")
if transaction_id and transaction_id != previous_transaction:
    payload.update({
        "started_at": now,
        "finished_at": None,
        "recovery_requested_at": None,
        "diagnostic_id": "",
    })
payload.update({
    "phase": phase,
    "message": message,
    "error": error,
    "failed_target": failed_target,
    "current_commit": current,
    "target_commit": target,
    "automatic": automatic == "true",
    "previous_commit": current if transaction_id and transaction_id != previous_transaction else (payload.get("previous_commit") or current),
    "update_available": phase in {"update_available", "queued"} and not failed_target,
    "updated_at": now,
})
if transaction_id:
    payload.update({
        "transaction_id": transaction_id,
        "staging_dir": staging_dir,
        "web_artifacts_swapped": web_artifacts_swapped == "true",
        "web_dependencies_swapped": web_dependencies_swapped == "true",
        "python_dependencies_changed": python_dependencies_changed == "true",
        "web_dependencies_changed": web_dependencies_changed == "true",
        "web_build_required": web_build_required == "true",
        "controller_pid": int(controller_pid),
        "controller_start_ticks": controller_start_ticks,
    })
if phase in {"preflight", "waiting_for_idle", "stopping", "installing", "starting", "rolling_back", "recovering"}:
    payload["started_at"] = payload.get("started_at") or now
if phase in {"succeeded", "failed", "rolled_back", "rollback_failed"}:
    payload["finished_at"] = now
if phase == "succeeded":
    payload["last_success_at"] = now
    payload["queued_at"] = None
path_tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
path_tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(path_tmp, path)
PY
    if [[ -n "$error" && ( "$phase" == "failed" || "$phase" == "rolled_back" || "$phase" == "rollback_failed" ) ]]; then
        record_diagnostics "$phase" "$error" || true
    fi
}

release_lock() {
    if [[ "$UPDATE_HEARTBEAT_PID" =~ ^[0-9]+$ ]] && kill -0 "$UPDATE_HEARTBEAT_PID" 2>/dev/null; then
        kill "$UPDATE_HEARTBEAT_PID" 2>/dev/null || true
        wait "$UPDATE_HEARTBEAT_PID" 2>/dev/null || true
    fi
    UPDATE_HEARTBEAT_PID=""
    if [[ "$LOCK_ACQUIRED" == true && -d "$UPDATE_LOCK" ]]; then
        rm -f -- "$UPDATE_LOCK"/heartbeat.tmp.*
        rm -f -- "$UPDATE_LOCK/heartbeat"
        rm -f -- "$UPDATE_LOCK/owner.pid"
        rm -f -- "$UPDATE_LOCK/owner.start"
        rmdir -- "$UPDATE_LOCK" 2>/dev/null || true
        LOCK_ACQUIRED=false
    fi
    remove_owned_lease || true
}

update_process_is_controller() {
    local pid="$1" command expected_start actual_start
    kill -0 "$pid" 2>/dev/null || return 1
    expected_start="$(cat "$UPDATE_LOCK/owner.start" 2>/dev/null || true)"
    if [[ -n "$expected_start" ]]; then
        actual_start="$(process_start_ticks "$pid")"
        [[ -n "$actual_start" && "$actual_start" == "$expected_start" ]] || return 1
    fi
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$command" == *"update-controller"*"--execute"* \
        || "$command" == *"update-controller"*"--manual-execute"* \
        || "$command" == *"update.sh"*"--execute"* \
        || "$command" == *"update.sh"*"--manual-execute"* \
        || "$command" == *"update.sh"*"--recover-interrupted"* \
        || "$command" == *"update.sh"*"--finalize-recovery"* ]]
}

start_update_heartbeat() {
    local owner="$$"
    [[ -n "$CONTROLLER_START_TICKS" ]] || CONTROLLER_START_TICKS="$(process_start_ticks "$owner")"
    write_update_lease || true
    (
        while update_process_is_controller "$owner"; do
            date +%s > "$UPDATE_LOCK/heartbeat.tmp.$BASHPID" 2>/dev/null || exit 0
            mv -f -- "$UPDATE_LOCK/heartbeat.tmp.$BASHPID" "$UPDATE_LOCK/heartbeat" 2>/dev/null || exit 0
            write_update_lease 2>/dev/null || exit 0
            sleep 5
        done
    ) &
    UPDATE_HEARTBEAT_PID=$!
}

stop_maintenance() {
    if [[ ! "$MAINTENANCE_PID" =~ ^[0-9]+$ && -n "$MAINTENANCE_PID_FILE" ]]; then
        MAINTENANCE_PID="$(cat "$MAINTENANCE_PID_FILE" 2>/dev/null || true)"
    fi
    if [[ "$MAINTENANCE_PID" =~ ^[0-9]+$ ]] && maintenance_process_matches "$MAINTENANCE_PID"; then
        kill "$MAINTENANCE_PID" 2>/dev/null || true
        for _ in {1..50}; do
            kill -0 "$MAINTENANCE_PID" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$MAINTENANCE_PID" 2>/dev/null; then
            kill -9 "$MAINTENANCE_PID" 2>/dev/null || true
        fi
        wait "$MAINTENANCE_PID" 2>/dev/null || true
    fi
    [[ -n "$MAINTENANCE_PID_FILE" ]] && rm -f -- "$MAINTENANCE_PID_FILE"
    [[ -n "$MAINTENANCE_START_FILE" ]] && rm -f -- "$MAINTENANCE_START_FILE"
    MAINTENANCE_PID=""
}

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM HUP
    if [[ "$CUTOVER_STARTED" == true && "$RUNTIME_HEALTHY" == false && "$RECOVERY_IN_PROGRESS" == false ]]; then
        RECOVERY_IN_PROGRESS=true
        log "The update controller exited during cutover; starting emergency rollback."
        if ! rollback_release; then
            record_rollback_failure
        fi
    fi
    if [[ "$PRESERVE_MAINTENANCE" == false ]]; then
        stop_maintenance
    fi
    if [[ -n "$HANDOFF_FILE" && "$HANDOFF_FILE" == "$RUN_DIR"/update-handoff.*.token ]]; then
        rm -f -- "$HANDOFF_FILE"
    fi
    if [[ "$PRESERVE_RECOVERY_ARTIFACTS" == false && -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
        rm -rf -- "$STAGING_DIR"
    fi
    release_lock
    if [[ "${BASH_SOURCE[0]}" == "$RUN_DIR"/update-controller.*.sh ]]; then
        rm -f -- "${BASH_SOURCE[0]}"
    fi
    exit "$exit_code"
}

acquire_update_lock() {
    local owner=""
    mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    if mkdir "$UPDATE_LOCK" 2>/dev/null; then
        CONTROLLER_START_TICKS="$(process_start_ticks "$$")"
        printf '%s\n' "$$" > "$UPDATE_LOCK/owner.pid"
        printf '%s\n' "$CONTROLLER_START_TICKS" > "$UPDATE_LOCK/owner.start"
        LOCK_ACQUIRED=true
        start_update_heartbeat
        return 0
    fi
    owner="$(cat "$UPDATE_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && update_process_is_controller "$owner"; then
        return 1
    fi
    rm -f -- "$UPDATE_LOCK/owner.pid"
    rm -f -- "$UPDATE_LOCK/owner.start"
    rmdir -- "$UPDATE_LOCK" 2>/dev/null || return 1
    mkdir "$UPDATE_LOCK" || return 1
    CONTROLLER_START_TICKS="$(process_start_ticks "$$")"
    printf '%s\n' "$$" > "$UPDATE_LOCK/owner.pid"
    printf '%s\n' "$CONTROLLER_START_TICKS" > "$UPDATE_LOCK/owner.start"
    LOCK_ACQUIRED=true
    start_update_heartbeat
}

start_maintenance() {
    local python_bin="$ROOT_DIR/venv/bin/python" maintenance_script="$ROOT_DIR/server/scratch/maintenance_server.py" previous_directory
    if [[ "$MAINTENANCE_PID" =~ ^[0-9]+$ ]] && maintenance_process_matches "$MAINTENANCE_PID"; then
        return 0
    fi
    if [[ -n "$STAGING_DIR" && -f "$STAGING_DIR/checkout/server/scratch/maintenance_server.py" ]]; then
        maintenance_script="$STAGING_DIR/checkout/server/scratch/maintenance_server.py"
    fi
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    previous_directory="$PWD"
    cd "$ROOT_DIR" || return 1
    nohup "$python_bin" "$maintenance_script" \
        --port "$WEB_PORT" \
        --root "$ROOT_DIR" \
        --state-file "$STATUS_FILE" \
        --lease-file "$LEASE_FILE" \
        --transaction-id "$TRANSACTION_ID" \
        --controller-silence-seconds 30 \
        >> "$UPDATE_LOG" 2>&1 < /dev/null &
    MAINTENANCE_PID=$!
    cd "$previous_directory" || return 1
    printf '%s\n' "$MAINTENANCE_PID" > "$MAINTENANCE_PID_FILE"
    process_start_ticks "$MAINTENANCE_PID" > "$MAINTENANCE_START_FILE"
    chmod 600 "$MAINTENANCE_PID_FILE" 2>/dev/null || true
    chmod 600 "$MAINTENANCE_START_FILE" 2>/dev/null || true
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

serve_recovery_maintenance() {
    local recovered_transaction
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    LEASE_FILE="$RUN_DIR/update-lease.json"
    MAINTENANCE_PID_FILE="$RUN_DIR/maintenance.pid"
    MAINTENANCE_START_FILE="$RUN_DIR/maintenance.start"
    WEB_PORT="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    recovered_transaction="$(
        python3 - "$STATUS_FILE" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, TypeError, ValueError):
    payload = {}
print(str(payload.get("transaction_id") or "recovery"))
PY
    )"
    TRANSACTION_ID="$recovered_transaction"
    python3 - "$STATUS_FILE" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, TypeError, ValueError):
    payload = {}
payload.update(
    {
        "phase": "rollback_failed",
        "message": "The recovered release did not pass startup health checks. Run ./start.sh after reviewing the lifecycle logs.",
        "error": "recovery_start_failed",
        "finished_at": time.time(),
        "updated_at": time.time(),
    }
)
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(f"{path.name}.{os.getpid()}.recovery.tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
    start_maintenance || return 1
    PRESERVE_MAINTENANCE=true
}

prepare_python_wheelhouse() {
    local source_root="$1" destination="$2" builder_python="$3"
    mkdir -p "$destination"
    "$builder_python" -m pip wheel \
        --wheel-dir "$destination" \
        -c "$source_root/server/requirements.lock" \
        -r "$source_root/server/requirements.txt"
}

install_python_wheelhouse() {
    local source_root="$1" wheelhouse="$2" target_python="${3:-$ROOT_DIR/venv/bin/python}"
    [[ -x "$target_python" ]] || return 1
    [[ -d "$wheelhouse" ]] || return 1
    "$target_python" -m pip install \
        --no-index \
        --find-links "$wheelhouse" \
        -c "$source_root/server/requirements.lock" \
        -r "$source_root/server/requirements.txt" \
        || return 1
    "$target_python" -m pip check
}

validate_current_python_environment() {
    [[ -x "$ROOT_DIR/venv/bin/python" ]] || return 1
    "$ROOT_DIR/venv/bin/python" -m pip check
}

record_prepared_setup_state() {
    if ! "$ROOT_DIR/setup.sh" --record-prepared-state; then
        log "The release is prepared, but setup reuse markers could not be refreshed; a future setup may repeat dependency validation."
    fi
}

detect_release_changes() {
    if git -C "$ROOT_DIR" diff --quiet "$OLD_COMMIT" "$TARGET_COMMIT" -- \
        server/requirements.txt server/requirements.lock
    then
        PYTHON_DEPENDENCIES_CHANGED=false
    else
        PYTHON_DEPENDENCIES_CHANGED=true
    fi
    if git -C "$ROOT_DIR" diff --quiet "$OLD_COMMIT" "$TARGET_COMMIT" -- \
        web/package.json web/package-lock.json
    then
        WEB_DEPENDENCIES_CHANGED=false
    else
        WEB_DEPENDENCIES_CHANGED=true
    fi
    if git -C "$ROOT_DIR" diff --quiet "$OLD_COMMIT" "$TARGET_COMMIT" -- web
    then
        WEB_BUILD_REQUIRED=false
    else
        WEB_BUILD_REQUIRED=true
    fi
    log "Preflight plan: Python dependencies changed=$PYTHON_DEPENDENCIES_CHANGED, web dependencies changed=$WEB_DEPENDENCIES_CHANGED, web build required=$WEB_BUILD_REQUIRED."
}

prepare_candidate_python() {
    local staged_checkout="$1" builder_python
    if [[ "$PYTHON_DEPENDENCIES_CHANGED" == false ]]; then
        validate_current_python_environment || return 1
        ln -s "$ROOT_DIR/venv" "$staged_checkout/venv" || return 1
        log "Python dependency manifests are unchanged; reusing the verified installed environment."
        return 0
    fi

    python3 -m venv "$staged_checkout/venv" || return 1
    builder_python="$staged_checkout/venv/bin/python"
    "$builder_python" -m pip --version >/dev/null || "$builder_python" -m ensurepip --upgrade || return 1
    log "Python dependency manifests changed; preparing candidate and rollback packages."
    prepare_python_wheelhouse "$staged_checkout" "$STAGING_DIR/candidate-wheels" "$builder_python" || return 1
    prepare_python_wheelhouse "$ROOT_DIR" "$STAGING_DIR/rollback-wheels" "$builder_python" || return 1
    install_python_wheelhouse "$staged_checkout" "$STAGING_DIR/candidate-wheels" "$builder_python"
}

prepare_candidate_web() {
    local staged_checkout="$1" candidate_web="$1/web"
    if [[ "$WEB_BUILD_REQUIRED" == false ]]; then
        [[ -d "$ROOT_DIR/web/node_modules" && -s "$ROOT_DIR/web/dist/index.html" ]] || return 1
        log "Web sources are unchanged; reusing installed dependencies and production assets."
        return 0
    fi

    if [[ "$WEB_DEPENDENCIES_CHANGED" == true ]]; then
        log "Web dependency manifests changed; installing the candidate dependency tree once."
        (cd "$candidate_web" && npm ci --prefer-offline --no-audit --no-fund) || return 1
    else
        [[ -d "$ROOT_DIR/web/node_modules" ]] || return 1
        ln -s "$ROOT_DIR/web/node_modules" "$candidate_web/node_modules" || return 1
        log "Web dependency manifests are unchanged; building with the verified installed dependency tree."
    fi

    if ! (cd "$candidate_web" && npm run build); then
        [[ "$WEB_DEPENDENCIES_CHANGED" == false ]] && rm -f -- "$candidate_web/node_modules"
        return 1
    fi
    if [[ "$WEB_DEPENDENCIES_CHANGED" == false ]]; then
        rm -f -- "$candidate_web/node_modules"
    fi
    [[ -s "$candidate_web/dist/index.html" ]]
}

activate_prepared_web() {
    local candidate_web="$STAGING_DIR/checkout/web"
    if [[ "$WEB_BUILD_REQUIRED" == false ]]; then
        write_state "installing" "Web sources are unchanged; keeping the existing production assets."
        return 0
    fi
    [[ -s "$candidate_web/dist/index.html" ]] || return 1
    if [[ "$WEB_DEPENDENCIES_CHANGED" == true ]]; then
        [[ -d "$candidate_web/node_modules" ]] || return 1
        WEB_DEPENDENCIES_SWAPPED=true
    fi
    WEB_ARTIFACTS_SWAPPED=true
    write_state "installing" "Activating preflighted web runtime and production assets."
    if [[ "$WEB_DEPENDENCIES_SWAPPED" == true && -e "$ROOT_DIR/web/node_modules" ]]; then
        mv -- "$ROOT_DIR/web/node_modules" "$STAGING_DIR/previous-node_modules" || return 1
    fi
    if [[ -e "$ROOT_DIR/web/dist" ]]; then
        mv -- "$ROOT_DIR/web/dist" "$STAGING_DIR/previous-dist" || return 1
    fi
    if [[ "$WEB_DEPENDENCIES_SWAPPED" == true ]]; then
        mv -- "$candidate_web/node_modules" "$ROOT_DIR/web/node_modules" || return 1
    fi
    mv -- "$candidate_web/dist" "$ROOT_DIR/web/dist" || return 1
    [[ -s "$ROOT_DIR/web/dist/index.html" ]] || return 1
    write_state "installing" "Preflighted runtime assets are active; verifying the exact release."
}

restore_previous_web() {
    [[ "$WEB_ARTIFACTS_SWAPPED" == true ]] || return 0
    if [[ "$WEB_DEPENDENCIES_SWAPPED" == true && -e "$STAGING_DIR/previous-node_modules" && -e "$ROOT_DIR/web/node_modules" ]]; then
        mv -- "$ROOT_DIR/web/node_modules" "$STAGING_DIR/failed-node_modules" 2>/dev/null || return 1
    fi
    if [[ -e "$STAGING_DIR/previous-dist" && -e "$ROOT_DIR/web/dist" ]]; then
        mv -- "$ROOT_DIR/web/dist" "$STAGING_DIR/failed-dist" 2>/dev/null || return 1
    fi
    if [[ "$WEB_DEPENDENCIES_SWAPPED" == true && -e "$STAGING_DIR/previous-node_modules" ]]; then
        mv -- "$STAGING_DIR/previous-node_modules" "$ROOT_DIR/web/node_modules" || return 1
    fi
    if [[ -e "$STAGING_DIR/previous-dist" ]]; then
        mv -- "$STAGING_DIR/previous-dist" "$ROOT_DIR/web/dist" || return 1
    fi
    WEB_ARTIFACTS_SWAPPED=false
    WEB_DEPENDENCIES_SWAPPED=false
    write_state "rolling_back" "The previous web runtime and production assets were restored."
}

preflight_target() {
    local staged_checkout="$STAGING_DIR/checkout" staged_head
    git -C "$ROOT_DIR" cat-file -e "${TARGET_COMMIT}^{commit}" || return 1
    git -C "$ROOT_DIR" merge-base --is-ancestor "$OLD_COMMIT" "$TARGET_COMMIT" || return 1
    log "Creating an isolated checkout from the already-fetched exact target commit."
    git clone --shared --no-checkout "$ROOT_DIR" "$staged_checkout" || return 1
    git -C "$staged_checkout" checkout --detach "$TARGET_COMMIT" || return 1
    staged_head="$(git -C "$staged_checkout" rev-parse HEAD 2>/dev/null || true)"
    [[ "$staged_head" == "$TARGET_COMMIT" ]] || {
        log "The isolated checkout did not resolve to the verified target commit."
        return 1
    }
    detect_release_changes
    write_state "preflight" "Change analysis complete. Preparing only changed dependencies and required frontend assets while StreamHome stays online."
    prepare_candidate_python "$staged_checkout" || return 1
    prepare_candidate_web "$staged_checkout" || return 1
    (
        cd "$staged_checkout"
        ./test.sh --syntax-only
        ./venv/bin/python -m compileall -q server
        PYTHONPATH=server ./venv/bin/python server/scratch/test_update_system.py
    ) || return 1
    log "Candidate scripts, code, required dependencies, rollback artifacts, and production assets are ready."
}

wait_for_idle_handoff() {
    local code handoff_token install_mode
    handoff_token="$(cat "$HANDOFF_FILE" 2>/dev/null || true)"
    [[ -n "$handoff_token" ]] || return 1
    install_mode="$(python3 - "$STATUS_FILE" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError, TypeError):
    payload = {}
print(payload.get("install_mode", "when_idle"))
PY
)"
    if [[ "$install_mode" == "now" ]]; then
        write_state "waiting_for_idle" "Preflight passed. Requesting immediate protected cutover."
    else
        write_state "waiting_for_idle" "Preflight passed. Waiting for the server to become idle again."
    fi
    while true; do
        code="$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
            -H "X-StreamHome-Update-Handoff: $handoff_token" \
            -X POST "http://127.0.0.1:8000/api/update/handoff" 2>/dev/null || true)"
        case "$code" in
            200)
                rm -f -- "$HANDOFF_FILE"
                log "The backend reserved a protected maintenance cutover."
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
    if [[ -f "$RUN_DIR/pre-update-database.db" ]]; then
        restore_database_checkpoint || return 1
    fi
    restore_previous_web || return 1
    if [[ "$PYTHON_DEPENDENCIES_CHANGED" == true ]]; then
        install_python_wheelhouse "$ROOT_DIR" "$STAGING_DIR/rollback-wheels" || return 1
    else
        validate_current_python_environment || return 1
    fi
    stop_maintenance
    if ! "$ROOT_DIR/start.sh" --update-recovery-complete; then
        start_maintenance || true
        return 1
    fi
    RUNTIME_HEALTHY=true
    CUTOVER_STARTED=false
    write_state "rolled_back" "The update failed, and the previous healthy release was restored." "update_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
    return 0
}

record_rollback_failure() {
    RECOVERY_IN_PROGRESS=true
    write_state "rollback_failed" "The update and automatic rollback both failed. Review update.log." "rollback_failed" "$TARGET_COMMIT"
    start_maintenance || true
    PRESERVE_MAINTENANCE=true
    PRESERVE_RECOVERY_ARTIFACTS=true
}

runtime_endpoints_ready() {
    local configured_port
    configured_port="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    [[ "$configured_port" =~ ^[0-9]+$ ]] || return 1
    python3 - "$configured_port" <<'PY'
import json
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:8000/api/health", timeout=1.5) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if response.status >= 400 or payload.get("status") != "ready":
            raise SystemExit(1)
    with opener.open(f"http://127.0.0.1:{int(sys.argv[1])}/", timeout=1.5) as response:
        raise SystemExit(0 if 200 <= response.status < 400 else 1)
except Exception:
    raise SystemExit(1)
PY
}

recovery_staging_is_safe() {
    local install_parent
    [[ -n "$STAGING_DIR" ]] || return 1
    install_parent="$(cd "$(dirname "$ROOT_DIR")" && pwd -P)"
    case "$STAGING_DIR" in
        "$install_parent"/.streamhome-update.*) [[ -d "$STAGING_DIR" ]] ;;
        *) return 1 ;;
    esac
}

recover_interrupted_release() {
    local phase current_head recovered_staging web_swapped
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    UPDATE_LOCK="$RUN_DIR/update.lock"
    LEASE_FILE="$RUN_DIR/update-lease.json"
    MAINTENANCE_PID_FILE="$RUN_DIR/maintenance.pid"
    MAINTENANCE_START_FILE="$RUN_DIR/maintenance.start"
    WEB_PORT="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
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
print(payload.get("transaction_id", ""))
print(payload.get("staging_dir", ""))
print("true" if payload.get("web_artifacts_swapped") else "false")
web_dependencies_swapped = payload.get("web_dependencies_swapped")
if web_dependencies_swapped is None:
    web_dependencies_swapped = payload.get("web_artifacts_swapped", False)
print("true" if web_dependencies_swapped else "false")
print("true" if payload.get("python_dependencies_changed", True) else "false")
print("true" if payload.get("web_dependencies_changed", True) else "false")
print("true" if payload.get("web_build_required", True) else "false")
PY
    )
    phase="${recovery_state[0]:-}"
    OLD_COMMIT="${recovery_state[1]:-}"
    TARGET_COMMIT="${recovery_state[2]:-}"
    TRANSACTION_ID="${recovery_state[3]:-}"
    recovered_staging="${recovery_state[4]:-}"
    web_swapped="${recovery_state[5]:-false}"
    WEB_DEPENDENCIES_SWAPPED="${recovery_state[6]:-$web_swapped}"
    PYTHON_DEPENDENCIES_CHANGED="${recovery_state[7]:-true}"
    WEB_DEPENDENCIES_CHANGED="${recovery_state[8]:-true}"
    WEB_BUILD_REQUIRED="${recovery_state[9]:-true}"
    [[ -n "$TRANSACTION_ID" ]] || TRANSACTION_ID="$(new_transaction_id)"
    STAGING_DIR="$recovered_staging"
    WEB_ARTIFACTS_SWAPPED="$web_swapped"
    if [[ -n "$STAGING_DIR" ]] && ! recovery_staging_is_safe; then
        STAGING_DIR=""
    fi
    case "$phase" in
        preflight|waiting_for_idle|stopping|installing|starting|rolling_back|recovering)
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
    if runtime_endpoints_ready; then
        stop_maintenance
        rm -f -- "$RUN_DIR/pre-update-database.db"
        if [[ "$current_head" == "$TARGET_COMMIT" ]]; then
            RUNTIME_HEALTHY=true
            CUTOVER_STARTED=false
            write_state "succeeded" "The updated release was already healthy; interrupted bookkeeping was reconciled." "" "" "$TARGET_COMMIT"
            return 12
        fi
        if [[ "$current_head" == "$OLD_COMMIT" ]]; then
            RUNTIME_HEALTHY=true
            CUTOVER_STARTED=false
            write_state "rolled_back" "The previous release was already healthy; interrupted bookkeeping was reconciled." "update_interrupted_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
            return 12
        fi
    fi
    if [[ "$current_head" == "$OLD_COMMIT" && ! -f "$RUN_DIR/pre-update-database.db" ]]; then
        stop_maintenance
        write_state "failed" "An interrupted preflight was cleared; the installed release was unchanged." "update_interrupted" "$TARGET_COMMIT" "$OLD_COMMIT"
        return 10
    fi
    RECOVERY_IN_PROGRESS=true
    PRESERVE_RECOVERY_ARTIFACTS=true
    write_state "rolling_back" "Recovering an update interrupted before health verification." "update_interrupted" "$TARGET_COMMIT" "$current_head"
    "$ROOT_DIR/stop.sh" --quiet || true
    git -C "$ROOT_DIR" reset --hard "$OLD_COMMIT" || {
        record_rollback_failure
        return 1
    }
    if [[ -f "$RUN_DIR/pre-update-database.db" ]]; then
        restore_database_checkpoint || {
            record_rollback_failure
            return 1
        }
    fi
    if [[ "$WEB_ARTIFACTS_SWAPPED" == true ]]; then
        recovery_staging_is_safe || {
            record_rollback_failure
            return 1
        }
        restore_previous_web || {
            record_rollback_failure
            return 1
        }
    fi
    if recovery_staging_is_safe && [[ -d "$STAGING_DIR/rollback-wheels" ]]; then
        install_python_wheelhouse "$ROOT_DIR" "$STAGING_DIR/rollback-wheels" || {
            record_rollback_failure
            return 1
        }
    elif [[ "$PYTHON_DEPENDENCIES_CHANGED" == false ]]; then
        validate_current_python_environment || {
            record_rollback_failure
            return 1
        }
    else
        (
            cd "$ROOT_DIR"
            ./setup.sh --no-start --skip-system-packages
        ) || {
            record_rollback_failure
            return 1
        }
    fi
    write_state "recovering" "The previous release is restored and will now pass normal startup health checks." "update_interrupted" "$TARGET_COMMIT" "$OLD_COMMIT"
    return 0
}

finalize_interrupted_recovery() {
    local current_head recovered_staging web_swapped
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    UPDATE_LOCK="$RUN_DIR/update.lock"
    LEASE_FILE="$RUN_DIR/update-lease.json"
    MAINTENANCE_PID_FILE="$RUN_DIR/maintenance.pid"
    MAINTENANCE_START_FILE="$RUN_DIR/maintenance.start"
    WEB_PORT="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    [[ -f "$STATUS_FILE" ]] || return 1
    mapfile -t recovery_state < <(python3 - "$STATUS_FILE" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, TypeError, ValueError):
    raise SystemExit(1)
print(payload.get("previous_commit", ""))
print(payload.get("target_commit", ""))
print(payload.get("transaction_id", ""))
print(payload.get("staging_dir", ""))
print("true" if payload.get("web_artifacts_swapped") else "false")
web_dependencies_swapped = payload.get("web_dependencies_swapped")
if web_dependencies_swapped is None:
    web_dependencies_swapped = payload.get("web_artifacts_swapped", False)
print("true" if web_dependencies_swapped else "false")
print("true" if payload.get("python_dependencies_changed", True) else "false")
print("true" if payload.get("web_dependencies_changed", True) else "false")
print("true" if payload.get("web_build_required", True) else "false")
PY
    )
    OLD_COMMIT="${recovery_state[0]:-}"
    TARGET_COMMIT="${recovery_state[1]:-}"
    TRANSACTION_ID="${recovery_state[2]:-}"
    recovered_staging="${recovery_state[3]:-}"
    web_swapped="${recovery_state[4]:-false}"
    WEB_DEPENDENCIES_SWAPPED="${recovery_state[5]:-$web_swapped}"
    PYTHON_DEPENDENCIES_CHANGED="${recovery_state[6]:-true}"
    WEB_DEPENDENCIES_CHANGED="${recovery_state[7]:-true}"
    WEB_BUILD_REQUIRED="${recovery_state[8]:-true}"
    STAGING_DIR="$recovered_staging"
    WEB_ARTIFACTS_SWAPPED="$web_swapped"
    [[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ && "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ && -n "$TRANSACTION_ID" ]] || return 1
    if [[ -n "$STAGING_DIR" ]] && ! recovery_staging_is_safe; then
        STAGING_DIR=""
    fi
    acquire_update_lock || return 1
    trap cleanup EXIT
    current_head="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
    [[ "$current_head" == "$OLD_COMMIT" ]] || return 1
    runtime_endpoints_ready || return 1
    stop_maintenance
    rm -f -- "$RUN_DIR/pre-update-database.db"
    rm -f -- "$RUN_DIR/update-recovery.${TRANSACTION_ID}.requested"
    rm -f -- "$RUN_DIR/update-recovery.${TRANSACTION_ID:0:12}.requested"
    RUNTIME_HEALTHY=true
    CUTOVER_STARTED=false
    RECOVERY_IN_PROGRESS=false
    PRESERVE_RECOVERY_ARTIFACTS=false
    if ! recovery_staging_is_safe; then
        STAGING_DIR=""
    fi
    write_state "rolled_back" "The interrupted update was rolled back and the previous release passed API and web health checks." "update_interrupted_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
}

execute_update() {
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    UPDATE_LOCK="$RUN_DIR/update.lock"
    LEASE_FILE="$RUN_DIR/update-lease.json"
    MAINTENANCE_PID_FILE="$RUN_DIR/maintenance.pid"
    MAINTENANCE_START_FILE="$RUN_DIR/maintenance.start"
    TRANSACTION_ID="$(new_transaction_id)"
    if [[ "$MANUAL_CUTOVER" == false ]]; then
        UPDATE_BRANCH="$(read_env "$ROOT_DIR/server/.env" UPDATE_BRANCH main)"
    fi
    WEB_PORT="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    PUBLIC_URL="$(read_env "$ROOT_DIR/.env" PUBLIC_URL "")"
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP

    if ! acquire_update_lock; then
        write_state "failed" "Another update controller already owns the update lock." "update_in_progress" "$TARGET_COMMIT"
        return 1
    fi
    STAGING_DIR="$(mktemp -d "$(dirname "$ROOT_DIR")/.streamhome-update.XXXXXX")" || {
        write_state "failed" "A temporary preflight checkout could not be created." "preflight_workspace_failed" "$TARGET_COMMIT"
        return 1
    }
    write_state "preflight" "Comparing the installed and target releases before preparing only the dependencies and assets that changed."
    if ! preflight_target; then
        write_state "failed" "The candidate failed isolated preflight. The running installation was not changed." "preflight_failed" "$TARGET_COMMIT"
        return 1
    fi
    if [[ "$MANUAL_CUTOVER" == true ]]; then
        write_state "waiting_for_idle" "Manual terminal update approved. Beginning protected cutover."
        log "Manual terminal update approved; active sessions will be disconnected."
    else
        if ! wait_for_idle_handoff; then
            write_state "failed" "The updater could not reserve a verified-idle cutover." "idle_handoff_failed" "$TARGET_COMMIT"
            return 1
        fi
    fi
    if public_origin_matches "$OLD_COMMIT"; then
        PUBLIC_ORIGIN_WAS_READY=true
        log "The configured public origin is healthy on the installed build; post-update ingress verification is required."
    else
        log "The configured public origin could not be verified from this host; local health gates remain authoritative."
    fi

    write_state "stopping" "Prepared release is ready. Stopping StreamHome for a short protected activation."
    CUTOVER_STARTED=true
    RUNTIME_HEALTHY=false
    if ! "$ROOT_DIR/stop.sh" --quiet; then
        if "$ROOT_DIR/start.sh" --update-recovery-complete; then
            RUNTIME_HEALTHY=true
            CUTOVER_STARTED=false
        fi
        write_state "failed" "StreamHome did not stop cleanly. The update was not applied." "shutdown_failed" "$TARGET_COMMIT"
        return 1
    fi
    if ! start_maintenance; then
        if "$ROOT_DIR/start.sh" --update-recovery-complete; then
            RUNTIME_HEALTHY=true
            CUTOVER_STARTED=false
        fi
        write_state "failed" "The maintenance responder could not start. The update was not applied." "maintenance_start_failed" "$TARGET_COMMIT"
        return 1
    fi
    if ! create_database_checkpoint; then
        stop_maintenance
        if "$ROOT_DIR/start.sh" --update-recovery-complete; then
            RUNTIME_HEALTHY=true
            CUTOVER_STARTED=false
        fi
        write_state "failed" "The database recovery checkpoint failed. The update was not applied." "database_checkpoint_failed" "$TARGET_COMMIT"
        return 1
    fi

    write_state "installing" "Activating the exact preflighted commit and prepared runtime assets."
    if ! git -C "$ROOT_DIR" status --porcelain --untracked-files=normal | grep -q .; then
        :
    else
        log "The checkout became dirty before cutover."
        rollback_release || record_rollback_failure
        return 1
    fi
    if ! git -C "$ROOT_DIR" merge --ff-only "$TARGET_COMMIT"; then
        rollback_release || record_rollback_failure
        return 1
    fi
    if [[ "$PYTHON_DEPENDENCIES_CHANGED" == true ]]; then
        write_state "installing" "Installing the changed preflighted Python dependency set offline."
        if ! install_python_wheelhouse "$ROOT_DIR" "$STAGING_DIR/candidate-wheels"; then
            rollback_release || record_rollback_failure
            return 1
        fi
    else
        write_state "installing" "Python dependencies are unchanged; keeping the verified installed environment."
    fi
    if ! activate_prepared_web; then
        rollback_release || record_rollback_failure
        return 1
    fi

    if [[ "$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)" != "$TARGET_COMMIT" ]]; then
        rollback_release || record_rollback_failure
        return 1
    fi
    write_state "starting" "Starting and health-checking the updated API and web client." "" "" "$TARGET_COMMIT"
    stop_maintenance
    if [[ "$START_AFTER_UPDATE" == false ]]; then
        record_prepared_setup_state
        rm -f -- "$RUN_DIR/pre-update-database.db"
        RUNTIME_HEALTHY=true
        CUTOVER_STARTED=false
        write_state "succeeded" "Update installed successfully. StreamHome was left stopped by explicit --no-start request." "" "" "$TARGET_COMMIT"
        log "Update completed successfully at ${TARGET_COMMIT:0:12}; startup was skipped by request."
        return 0
    fi
    if ! "$ROOT_DIR/start.sh" --update-recovery-complete; then
        start_maintenance || true
        rollback_release || record_rollback_failure
        return 1
    fi
    if [[ "$PUBLIC_ORIGIN_WAS_READY" == true ]]; then
        write_state "starting" "Local services are healthy. Verifying the exact updated build through the configured public origin." "" "" "$TARGET_COMMIT"
        if ! public_origin_matches "$TARGET_COMMIT" 15; then
            log "The public origin stopped serving the expected build after cutover; rolling back."
            rollback_release || record_rollback_failure
            return 1
        fi
    fi
    record_prepared_setup_state
    RUNTIME_HEALTHY=true
    CUTOVER_STARTED=false
    rm -f -- "$RUN_DIR/pre-update-database.db"
    write_state "succeeded" "Update installed successfully; both StreamHome services passed health checks." "" "" "$TARGET_COMMIT"
    log "Update completed successfully at ${TARGET_COMMIT:0:12}."
}

case "${1:-}" in
    --classify-changes)
        [[ $# -eq 4 ]] || exit 2
        OLD_COMMIT="$2"
        TARGET_COMMIT="$3"
        ROOT_DIR="$(cd "$4" && pwd -P)"
        [[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ && "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 2
        detect_release_changes
        printf 'python_dependencies_changed=%s\n' "$PYTHON_DEPENDENCIES_CHANGED"
        printf 'web_dependencies_changed=%s\n' "$WEB_DEPENDENCIES_CHANGED"
        printf 'web_build_required=%s\n' "$WEB_BUILD_REQUIRED"
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
    --manual-execute)
        [[ $# -eq 6 ]] || exit 2
        TARGET_COMMIT="$2"
        OLD_COMMIT="$3"
        UPDATE_BRANCH="$4"
        START_AFTER_UPDATE="$5"
        ROOT_DIR="$(cd "$6" && pwd -P)"
        AUTOMATIC=false
        MANUAL_CUTOVER=true
        HANDOFF_FILE=""
        [[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ && "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 2
        [[ "$UPDATE_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$UPDATE_BRANCH" != *".."* ]] || exit 2
        [[ "$START_AFTER_UPDATE" == "true" || "$START_AFTER_UPDATE" == "false" ]] || exit 2
        execute_update
        ;;
    --recover-interrupted)
        [[ $# -eq 2 ]] || exit 2
        ROOT_DIR="$(cd "$2" && pwd -P)"
        recover_interrupted_release
        ;;
    --finalize-recovery)
        [[ $# -eq 2 ]] || exit 2
        ROOT_DIR="$(cd "$2" && pwd -P)"
        finalize_interrupted_recovery
        ;;
    --serve-maintenance)
        [[ $# -eq 2 ]] || exit 2
        ROOT_DIR="$(cd "$2" && pwd -P)"
        serve_recovery_maintenance
        ;;
    --help|-h)
        printf 'StreamHome detached, preflighted, health-gated update controller.\n'
        ;;
    *)
        printf 'Use the StreamHome admin center to manage updates.\n' >&2
        exit 2
        ;;
esac
