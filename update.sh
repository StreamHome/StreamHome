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
ACTIVATION_STARTED=false
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
COMMIT_TOKEN=""
COMMIT_FILE=""
CANCEL_FILE=""
LIFECYCLE_ENABLED=false
LIFECYCLE_OPERATION_ID="${STREAMHOME_LIFECYCLE_OPERATION_ID:-}"
LIFECYCLE_GENERATION="${STREAMHOME_LIFECYCLE_GENERATION:-}"
LIFECYCLE_HANDOFF_REQUESTED=false
if [[ -n "$LIFECYCLE_OPERATION_ID" || -n "$LIFECYCLE_GENERATION" ]]; then
    LIFECYCLE_HANDOFF_REQUESTED=true
fi
LIFECYCLE_PHASE="${STREAMHOME_LIFECYCLE_PHASE:-reserved}"
LIFECYCLE_PRESERVE=false
LIFECYCLE_CONTROLLER_OWNED="${STREAMHOME_LIFECYCLE_CONTROLLER_OWNED:-false}"
NO_ROLLBACK_BOUNDARY=false
UPDATE_COMMAND_TIMEOUT_SECONDS="${STREAMHOME_UPDATE_COMMAND_TIMEOUT_SECONDS:-1800}"

usage() {
    cat <<'EOF'
StreamHome update

Usage:
  ./update.sh [--no-start]
  ./update.sh --help

The public command fetches the configured official branch and runs the same
isolated, preflighted, health-gated update path as the bootstrap installer.

Options:
  --no-start  Repair the current release without starting; newer releases must pass guarded startup health.
  --help      Show this help text.
EOF
}

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
print(uuid.uuid4())
PY
}

new_secret_token() {
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

lifecycle_cli() {
    local python_bin="$ROOT_DIR/venv/bin/python"
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
        "$python_bin" -m lifecycle.cli --root "$ROOT_DIR" "$@"
}

update_controller_cli() {
    local python_bin="$ROOT_DIR/venv/bin/python"
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
        "$python_bin" -m lifecycle.update_controller "$@"
}

lifecycle_generation_from_json() {
    local python_bin="$ROOT_DIR/venv/bin/python"
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    "$python_bin" -c 'import json,sys; print(json.load(sys.stdin)["state"]["generation"])'
}

lifecycle_phase_from_json() {
    local python_bin="$ROOT_DIR/venv/bin/python"
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    "$python_bin" -c 'import json,sys; print(json.load(sys.stdin)["state"]["active_operation"]["phase"])'
}

installed_release_lifecycle_support() {
    local listing state_present=false cli_present=false
    if [[ ! "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
        || ! git -C "$ROOT_DIR" cat-file -e "$OLD_COMMIT^{commit}" 2>/dev/null; then
        printf '%s\n' "INDETERMINATE"
        return 0
    fi
    if ! listing="$(git -C "$ROOT_DIR" ls-tree -r --name-only "$OLD_COMMIT" -- \
        server/lifecycle/state.py server/lifecycle/cli.py 2>/dev/null)"; then
        printf '%s\n' "INDETERMINATE"
        return 0
    fi
    while IFS= read -r path; do
        case "$path" in
            server/lifecycle/state.py) state_present=true ;;
            server/lifecycle/cli.py) cli_present=true ;;
        esac
    done <<< "$listing"
    if [[ "$state_present" == true && "$cli_present" == true ]]; then
        printf '%s\n' "PHASE1_CONFIRMED"
    elif [[ "$state_present" == false && "$cli_present" == false ]]; then
        printf '%s\n' "PRE_PHASE1_CONFIRMED"
    else
        printf '%s\n' "INDETERMINATE"
    fi
}

record_lifecycle_phase_in_update_journal() {
    local phase="$1"
    if [[ -f "$ROOT_DIR/server/lifecycle/update_controller.py" ]]; then
        printf '{}\n' | update_controller_cli write-journal \
            --root "$ROOT_DIR" \
            --operation-id "$TRANSACTION_ID" \
            --generation "$LIFECYCLE_GENERATION" \
            --expected-lifecycle-phase "$phase"
        return
    fi
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
        python3 - "$STATUS_FILE" "$ROOT_DIR" "$TRANSACTION_ID" "$LIFECYCLE_GENERATION" "$phase" <<'PY'
import json
import os
import sys
import time
from pathlib import Path
from lifecycle import state

path = Path(sys.argv[1])
root = Path(sys.argv[2])
transaction_id = sys.argv[3]
generation = int(sys.argv[4])
phase = sys.argv[5]
authority = state.validate_operation_with_credential(
    root,
    operation_id=transaction_id,
    generation=generation,
    expected_phase=phase,
)
if authority.active_operation is None \
        or authority.active_operation.operation_type is not state.OperationType.UPDATE:
    raise SystemExit(1)
lock_path = path.with_name("update-state.lock")
with lock_path.open("a+b") as lock_file:
    if os.name == "posix":
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(1)
    if payload.get("transaction_id") != transaction_id \
            or payload.get("lifecycle_generation") != generation:
        raise SystemExit(1)
    payload["lifecycle_phase"] = phase
    payload["updated_at"] = time.time()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as state_file:
            os.chmod(temporary, 0o600)
            state_file.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
PY
}

initialize_lifecycle_authority() {
    local claim_output support authoritative_phase
    if [[ "$LIFECYCLE_HANDOFF_REQUESTED" == true ]]; then
        [[ "$LIFECYCLE_OPERATION_ID" == "$TRANSACTION_ID" \
            && "$LIFECYCLE_GENERATION" =~ ^[1-9][0-9]*$ ]] || return 1
        lifecycle_cli validate-owner \
            --operation-id "$LIFECYCLE_OPERATION_ID" \
            --generation "$LIFECYCLE_GENERATION" \
            --expected-phase "$LIFECYCLE_PHASE" >/dev/null || return 1
        [[ "$LIFECYCLE_PHASE" != "recovery_required" ]] || return 1
        LIFECYCLE_ENABLED=true
        return 0
    fi
    if [[ -n "$LIFECYCLE_OPERATION_ID" || -n "$LIFECYCLE_GENERATION" ]]; then
        [[ "$LIFECYCLE_OPERATION_ID" == "$TRANSACTION_ID" \
            && "$LIFECYCLE_GENERATION" =~ ^[1-9][0-9]*$ ]] || return 1
        claim_output="$(lifecycle_cli validate-owner \
            --operation-id "$LIFECYCLE_OPERATION_ID" \
            --generation "$LIFECYCLE_GENERATION")" || return 1
        authoritative_phase="$(printf '%s' "$claim_output" | lifecycle_phase_from_json)" || return 1
        [[ -n "$authoritative_phase" ]] || return 1
        [[ "$authoritative_phase" != "recovery_required" ]] || return 1
        LIFECYCLE_PHASE="$authoritative_phase"
        LIFECYCLE_ENABLED=true
        if ! record_lifecycle_phase_in_update_journal "$LIFECYCLE_PHASE"; then
            log "Shared lifecycle ownership was recovered, but the update journal phase could not be reconciled."
        fi
        return 0
    fi
    support="$(installed_release_lifecycle_support)" || return 1
    case "$support" in
        PRE_PHASE1_CONFIRMED)
            log "The installed release predates shared lifecycle authority; this adoption update remains protected by the existing update controller and writes no shared lifecycle state."
            return 0
            ;;
        PHASE1_CONFIRMED) ;;
        *)
            log "Shared lifecycle support in the installed release could not be determined safely."
            return 1
            ;;
    esac
    claim_output="$(lifecycle_cli claim \
        --operation-type update \
        --owner-kind updater \
        --operation-id "$TRANSACTION_ID")" || return 1
    LIFECYCLE_OPERATION_ID="$TRANSACTION_ID"
    LIFECYCLE_GENERATION="$(printf '%s' "$claim_output" | lifecycle_generation_from_json)" || return 1
    [[ "$LIFECYCLE_GENERATION" =~ ^[1-9][0-9]*$ ]] || return 1
    LIFECYCLE_ENABLED=true
}

release_lifecycle_authority() {
    [[ "$LIFECYCLE_ENABLED" == true ]] || return 0
    if grep -q -- "--expected-operation-type" "$ROOT_DIR/server/lifecycle/cli.py" 2>/dev/null; then
        lifecycle_cli complete \
            --operation-id "$LIFECYCLE_OPERATION_ID" \
            --generation "$LIFECYCLE_GENERATION" \
            --expected-phase "$LIFECYCLE_PHASE" \
            --expected-operation-type update >/dev/null || return 1
    else
        # Positively confirmed older lifecycle releases lack the typed-complete
        # adapter. Prove the exact current operation type through their canonical
        # Python state API before the exact generation/phase completion CAS.
        PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
            python3 - "$ROOT_DIR" "$LIFECYCLE_OPERATION_ID" "$LIFECYCLE_GENERATION" "$LIFECYCLE_PHASE" <<'PY' || return 1
import sys
from pathlib import Path
from lifecycle import state

current = state.validate_operation_with_credential(
    Path(sys.argv[1]),
    operation_id=sys.argv[2],
    generation=int(sys.argv[3]),
    expected_phase=sys.argv[4],
)
if current.active_operation is None \
        or current.active_operation.operation_type is not state.OperationType.UPDATE:
    raise SystemExit(1)
PY
        lifecycle_cli complete \
            --operation-id "$LIFECYCLE_OPERATION_ID" \
            --generation "$LIFECYCLE_GENERATION" \
            --expected-phase "$LIFECYCLE_PHASE" >/dev/null || return 1
    fi
    LIFECYCLE_ENABLED=false
    LIFECYCLE_PRESERVE=false
}

mark_lifecycle_recovery_required() {
    [[ "$LIFECYCLE_ENABLED" == true ]] || return 0
    if [[ "$LIFECYCLE_PHASE" == "recovery_required" ]]; then
        return 0
    fi
    lifecycle_cli transition \
        --operation-id "$LIFECYCLE_OPERATION_ID" \
        --generation "$LIFECYCLE_GENERATION" \
        --expected-phase "$LIFECYCLE_PHASE" \
        --next-phase recovery_required >/dev/null || return 1
    LIFECYCLE_PHASE="recovery_required"
    LIFECYCLE_PRESERVE=true
    if ! record_lifecycle_phase_in_update_journal "$LIFECYCLE_PHASE"; then
        log "Shared lifecycle recovery ownership is authoritative, but the update journal phase could not be reconciled."
    fi
}

validate_lifecycle_authority() {
    [[ "$LIFECYCLE_ENABLED" == true ]] || return 0
    if [[ "$LIFECYCLE_CONTROLLER_OWNED" == true ]]; then
        if [[ -f "$ROOT_DIR/server/lifecycle/update_controller.py" ]]; then
            update_controller_cli validate-controller \
                --root "$ROOT_DIR" \
                --operation-id "$LIFECYCLE_OPERATION_ID" \
                --generation "$LIFECYCLE_GENERATION" \
                --expected-phase "$LIFECYCLE_PHASE" \
                --supervisor-pid "$PPID" >/dev/null
            return
        fi
        lifecycle_cli validate-owner \
            --operation-id "$LIFECYCLE_OPERATION_ID" \
            --generation "$LIFECYCLE_GENERATION" \
            --expected-phase "$LIFECYCLE_PHASE" >/dev/null || return 1
        PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
            python3 - "$ROOT_DIR" "$LIFECYCLE_OPERATION_ID" "$LIFECYCLE_GENERATION" "$PPID" <<'PY'
import fcntl
import os
import sys
from pathlib import Path
from lifecycle import state

root = Path(sys.argv[1])
operation_id = sys.argv[2]
generation = int(sys.argv[3])
supervisor_pid = int(sys.argv[4])
current = state.inspect_state(root)
if current.active_operation is None \
        or current.active_operation.operation_id != operation_id \
        or current.generation != generation \
        or current.active_operation.operation_type is not state.OperationType.UPDATE:
    raise SystemExit(1)
lease = state.read_controller_lease(root)
if lease is None \
        or lease.operation_id != operation_id \
        or lease.generation != generation \
        or lease.pid != supervisor_pid \
        or not state.controller_lease_is_current(lease):
    raise SystemExit(1)
descriptor = os.open(state.lifecycle_paths(root).controller_lock_path, os.O_RDWR | os.O_CLOEXEC)
try:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        raise SystemExit(1)
finally:
    os.close(descriptor)
PY
        return
    fi
    lifecycle_cli validate-owner \
        --operation-id "$LIFECYCLE_OPERATION_ID" \
        --generation "$LIFECYCLE_GENERATION" \
        --expected-phase "$LIFECYCLE_PHASE" >/dev/null
}

advance_lifecycle_phase() {
    local next_phase="$1"
    [[ "$LIFECYCLE_ENABLED" == true ]] || return 0
    [[ "$LIFECYCLE_PHASE" != "recovery_required" ]] || return 1
    if [[ "$LIFECYCLE_PHASE" == "$next_phase" ]]; then
        validate_lifecycle_authority
        return
    fi
    lifecycle_cli transition \
        --operation-id "$LIFECYCLE_OPERATION_ID" \
        --generation "$LIFECYCLE_GENERATION" \
        --expected-phase "$LIFECYCLE_PHASE" \
        --next-phase "$next_phase" >/dev/null || return 1
    LIFECYCLE_PHASE="$next_phase"
    record_lifecycle_phase_in_update_journal "$LIFECYCLE_PHASE"
}

request_process_local_cutover() {
    local result python_bin="$ROOT_DIR/venv/bin/python"
    [[ "$LIFECYCLE_CONTROLLER_OWNED" == true ]] || return 0
    [[ "$LIFECYCLE_ENABLED" == true && "$LIFECYCLE_PHASE" == "reserved" ]] || return 1
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
    for _ in {1..15}; do
        cancellation_requested && return 20
        if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
            "$python_bin" -m lifecycle.update_controller request-cutover \
                --root "$ROOT_DIR" \
                --operation-id "$LIFECYCLE_OPERATION_ID" \
                --generation "$LIFECYCLE_GENERATION" 198>&-; then
            result=0
        else
            result=$?
        fi
        case "$result" in
            0)
                LIFECYCLE_PHASE="exclusive"
                validate_lifecycle_authority
                return
                ;;
            10)
                sleep 2
                cancellation_requested && return 20
                ;;
            *) return 1 ;;
        esac
    done
    return 1
}

run_bounded() {
    local seconds="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout --signal=TERM --kill-after=30 "$seconds" "$@"
    else
        "$@"
    fi
}

cancellation_requested() {
    [[ -n "$CANCEL_FILE" && -f "$CANCEL_FILE" ]]
}

record_cancelled_update() {
    rm -f -- "$CANCEL_FILE"
    write_state "update_available" "The update was cancelled before protected cutover." "" "" "$OLD_COMMIT"
}

preserve_unexpected_worktree_changes() {
    local patch_path="$RUN_DIR/update-preserved-changes.${TRANSACTION_ID}.patch"
    if git -C "$ROOT_DIR" diff --quiet HEAD --; then
        return 0
    fi
    if git -C "$ROOT_DIR" diff --binary HEAD -- > "$patch_path"; then
        chmod 600 "$patch_path" 2>/dev/null || true
        log "Unexpected tracked changes were preserved at $patch_path before rollback."
        return 0
    fi
    log "Unexpected tracked changes could not be preserved; refusing destructive rollback."
    return 1
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
    local phase="$1" error="$2" diagnostic_id
    [[ -n "$RUN_DIR" ]] || return 0
    diagnostic_id="$(python3 - "$RUN_DIR/update-diagnostics.json" "$STATUS_FILE" "$ROOT_DIR" "$phase" "$error" "$WEB_PORT" <<'PY'
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
print(diagnostic_id)
PY
    )" || return 1
    if [[ "$LIFECYCLE_ENABLED" == true && -f "$ROOT_DIR/server/lifecycle/update_controller.py" ]]; then
        python3 - "$diagnostic_id" <<'PY' \
            | update_controller_cli write-journal \
                --root "$ROOT_DIR" \
                --operation-id "$TRANSACTION_ID" \
                --generation "$LIFECYCLE_GENERATION" \
                --expected-lifecycle-phase "$LIFECYCLE_PHASE"
import json
import sys

print(json.dumps({"diagnostic_id": sys.argv[1]}, separators=(",", ":")))
PY
        return
    fi
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
        python3 - "$STATUS_FILE" "$diagnostic_id" "$TRANSACTION_ID" "$LIFECYCLE_ENABLED" "$LIFECYCLE_GENERATION" "$LIFECYCLE_PHASE" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
diagnostic_id = sys.argv[2]
transaction_id = sys.argv[3]
lifecycle_enabled = sys.argv[4]
lifecycle_generation = sys.argv[5]
lifecycle_phase = sys.argv[6]
lock_path = path.with_name("update-state.lock")
with lock_path.open("a+b") as lock_file:
    if os.name == "posix":
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("transaction_id") != transaction_id:
            raise SystemExit(1)
        if lifecycle_enabled == "true":
            from lifecycle import state
            authority = state.validate_operation_with_credential(
                path.parent.parent,
                operation_id=transaction_id,
                generation=int(lifecycle_generation),
                expected_phase=lifecycle_phase,
            )
            operation = authority.active_operation
            if operation is None or operation.operation_type is not state.OperationType.UPDATE:
                raise SystemExit(1)
            if payload.get("lifecycle_generation") != int(lifecycle_generation):
                raise SystemExit(1)
        payload["diagnostic_id"] = diagnostic_id
        temporary = path.with_name(f"{path.name}.{os.getpid()}.diagnostic.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    finally:
        if os.name == "posix":
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
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

read_update_branch() {
    local root="$1" value
    value="$(read_env "$root/.env" UPDATE_BRANCH __unset__)"
    if [[ "$value" == "__unset__" ]]; then
        value="$(read_env "$root/server/.env" UPDATE_BRANCH main)"
    fi
    printf '%s' "$value"
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
    if [[ "$LIFECYCLE_ENABLED" == true && -f "$ROOT_DIR/server/lifecycle/update_controller.py" ]]; then
        python3 - "$phase" "$message" "$error" "$failed_target" "$current_commit" "$TARGET_COMMIT" "$AUTOMATIC" "$STAGING_DIR" "$WEB_ARTIFACTS_SWAPPED" "$WEB_DEPENDENCIES_SWAPPED" "$PYTHON_DEPENDENCIES_CHANGED" "$WEB_DEPENDENCIES_CHANGED" "$WEB_BUILD_REQUIRED" "$$" "$CONTROLLER_START_TICKS" <<'PY' \
            | update_controller_cli write-journal \
                --root "$ROOT_DIR" \
                --operation-id "$TRANSACTION_ID" \
                --generation "$LIFECYCLE_GENERATION" \
                --expected-lifecycle-phase "$LIFECYCLE_PHASE"
import json
import sys

(
    phase,
    message,
    error,
    failed_target,
    current,
    target,
    automatic,
    staging_dir,
    web_artifacts_swapped,
    web_dependencies_swapped,
    python_dependencies_changed,
    web_dependencies_changed,
    web_build_required,
    controller_pid,
    controller_start_ticks,
) = sys.argv[1:]
print(json.dumps({
    "phase": phase,
    "message": message,
    "error": error,
    "failed_target": failed_target,
    "current_commit": current,
    "target_commit": target,
    "automatic": automatic == "true",
    "update_available": phase in {"update_available", "queued"} and not failed_target,
    "staging_dir": staging_dir,
    "web_artifacts_swapped": web_artifacts_swapped == "true",
    "web_dependencies_swapped": web_dependencies_swapped == "true",
    "python_dependencies_changed": python_dependencies_changed == "true",
    "web_dependencies_changed": web_dependencies_changed == "true",
    "web_build_required": web_build_required == "true",
    "controller_pid": int(controller_pid),
    "controller_start_ticks": controller_start_ticks,
}, separators=(",", ":")))
PY
        if [[ -n "$error" && ( "$phase" == "failed" || "$phase" == "rolled_back" || "$phase" == "rollback_failed" ) ]]; then
            record_diagnostics "$phase" "$error" || true
        fi
        return
    fi
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR/server" \
        python3 - "$STATUS_FILE" "$phase" "$message" "$error" "$failed_target" "$current_commit" "$TARGET_COMMIT" "$AUTOMATIC" "$TRANSACTION_ID" "$STAGING_DIR" "$WEB_ARTIFACTS_SWAPPED" "$WEB_DEPENDENCIES_SWAPPED" "$PYTHON_DEPENDENCIES_CHANGED" "$WEB_DEPENDENCIES_CHANGED" "$WEB_BUILD_REQUIRED" "$$" "$CONTROLLER_START_TICKS" "$LIFECYCLE_ENABLED" "$LIFECYCLE_GENERATION" "$LIFECYCLE_PHASE" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
lock_path = path.with_name("update-state.lock")
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
    lifecycle_enabled,
    lifecycle_generation,
    lifecycle_phase,
) = sys.argv[2:]
with lock_path.open("a+b") as lock_file:
    if os.name == "posix":
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError, TypeError):
            raise SystemExit(1)
        if not isinstance(payload, dict):
            raise SystemExit(1)
        now = time.time()
        previous_transaction = str(payload.get("transaction_id") or "")
        if transaction_id and transaction_id != previous_transaction:
            if payload.get("runtime_committed") is True:
                raise SystemExit(1)
            payload.update({
                "started_at": now,
                "finished_at": None,
                "recovery_requested_at": None,
                "diagnostic_id": "",
                "runtime_committed": False,
                "runtime_committed_at": None,
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
        if lifecycle_enabled == "true":
            from lifecycle import state
            authority = state.validate_operation_with_credential(
                path.parent.parent,
                operation_id=transaction_id,
                generation=int(lifecycle_generation),
                expected_phase=lifecycle_phase,
            )
            if authority.active_operation is None \
                    or authority.active_operation.operation_type is not state.OperationType.UPDATE:
                raise SystemExit(1)
            payload["lifecycle_generation"] = int(lifecycle_generation)
            payload["lifecycle_phase"] = lifecycle_phase
        if payload.get("runtime_committed") is True:
            payload["runtime_committed"] = True
        if phase in {"preflight", "waiting_for_idle", "stopping", "installing", "starting", "rolling_back", "recovering"}:
            payload["started_at"] = payload.get("started_at") or now
        if phase in {"succeeded", "failed", "rolled_back", "rollback_failed"}:
            payload["finished_at"] = now
        if phase == "succeeded":
            payload["last_success_at"] = now
            payload["queued_at"] = None
        path_tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with path_tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path_tmp, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.name == "posix":
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
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
    if [[ "$CUTOVER_STARTED" == true && "$RUNTIME_HEALTHY" == false \
        && "$RECOVERY_IN_PROGRESS" == false && "$NO_ROLLBACK_BOUNDARY" == false ]]; then
        RECOVERY_IN_PROGRESS=true
        if [[ "$ACTIVATION_STARTED" == true ]]; then
            log "The update controller exited after activation began; starting emergency rollback."
            if ! rollback_release; then
                record_rollback_failure
            fi
        else
            log "The update controller exited before activation; recovering the unchanged installed release."
            recover_unchanged_release \
                "The update stopped before activation. The installed release was not changed." \
                "pre_activation_interrupted" || true
        fi
    fi
    if [[ "$PRESERVE_MAINTENANCE" == false ]]; then
        stop_maintenance
    fi
    if [[ -n "$HANDOFF_FILE" && "$HANDOFF_FILE" == "$RUN_DIR"/update-handoff.*.token ]]; then
        rm -f -- "$HANDOFF_FILE"
    fi
    if [[ -n "$CANCEL_FILE" && "$CANCEL_FILE" == "$RUN_DIR"/update-cancel.*.requested ]]; then
        rm -f -- "$CANCEL_FILE"
    fi
    if [[ "$PRESERVE_RECOVERY_ARTIFACTS" == false && -n "$COMMIT_FILE" && "$COMMIT_FILE" == "$RUN_DIR"/update-commit.*.token ]]; then
        rm -f -- "$COMMIT_FILE"
    fi
    if [[ "$PRESERVE_RECOVERY_ARTIFACTS" == false && -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
        rm -rf -- "$STAGING_DIR"
    fi
    if [[ "$LIFECYCLE_CONTROLLER_OWNED" == true && "$LIFECYCLE_ENABLED" == true ]]; then
        update_controller_cli remove-permit \
                --root "$ROOT_DIR" \
                --operation-id "$LIFECYCLE_OPERATION_ID" \
                --generation "$LIFECYCLE_GENERATION" 198>&- >/dev/null 2>&1 || true
    fi
    release_lock
    if [[ "$LIFECYCLE_ENABLED" == true && "$LIFECYCLE_PRESERVE" == false ]]; then
        if [[ "$RUNTIME_HEALTHY" == true && "$CUTOVER_STARTED" == false ]]; then
            if ! release_lifecycle_authority; then
                log "Exact shared lifecycle ownership could not be released; recovery remains operator-visible."
                mark_lifecycle_recovery_required || true
                PRESERVE_RECOVERY_ARTIFACTS=true
                exit_code=1
            fi
        else
            mark_lifecycle_recovery_required || true
            PRESERVE_RECOVERY_ARTIFACTS=true
        fi
    fi
    if [[ "${BASH_SOURCE[0]}" == "$RUN_DIR"/update-controller.*.sh ]]; then
        rm -f -- "${BASH_SOURCE[0]}"
    fi
    exit "$exit_code"
}

initialize_update_lock() {
    CONTROLLER_START_TICKS="$(process_start_ticks "$$")"
    if ! printf '%s\n' "$$" > "$UPDATE_LOCK/owner.pid" \
        || ! printf '%s\n' "$CONTROLLER_START_TICKS" > "$UPDATE_LOCK/owner.start"; then
        rm -f -- "$UPDATE_LOCK/owner.pid" "$UPDATE_LOCK/owner.start"
        rmdir -- "$UPDATE_LOCK" 2>/dev/null || true
        return 1
    fi
    LOCK_ACQUIRED=true
    start_update_heartbeat
}

remove_quarantined_update_lock() {
    local quarantined_lock="$1"
    [[ "$quarantined_lock" == "$RUN_DIR"/update.lock.stale.* ]] || return 1
    rm -f -- "$quarantined_lock"/heartbeat.tmp.*
    rm -f -- "$quarantined_lock/heartbeat"
    rm -f -- "$quarantined_lock/owner.pid"
    rm -f -- "$quarantined_lock/owner.start"
    rmdir -- "$quarantined_lock" 2>/dev/null || true
}

acquire_update_lock() {
    local owner="" lock_age="0" recovery_fd="" quarantined_lock="" lock_claimed=false
    mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    if mkdir "$UPDATE_LOCK" 2>/dev/null; then
        initialize_update_lock
        return $?
    fi
    command -v flock >/dev/null 2>&1 || return 1
    exec {recovery_fd}> "$RUN_DIR/update-lock-recovery.lock" || return 1
    if ! flock -n "$recovery_fd"; then
        exec {recovery_fd}>&-
        return 1
    fi
    if mkdir "$UPDATE_LOCK" 2>/dev/null; then
        initialize_update_lock
        lock_claimed=$?
        exec {recovery_fd}>&-
        return "$lock_claimed"
    fi
    owner="$(cat "$UPDATE_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && update_process_is_controller "$owner"; then
        exec {recovery_fd}>&-
        return 1
    fi
    lock_age="$(python3 - "$UPDATE_LOCK" <<'PY'
import sys
import time
from pathlib import Path

try:
    print(max(0, int(time.time() - Path(sys.argv[1]).stat().st_mtime)))
except OSError:
    print(0)
PY
)"
    if [[ ! "$lock_age" =~ ^[0-9]+$ ]] || (( lock_age < 30 )); then
        exec {recovery_fd}>&-
        return 1
    fi
    quarantined_lock="$RUN_DIR/update.lock.stale.$$.$RANDOM"
    if ! mv -T -- "$UPDATE_LOCK" "$quarantined_lock" 2>/dev/null; then
        exec {recovery_fd}>&-
        return 1
    fi
    if mkdir "$UPDATE_LOCK" 2>/dev/null; then
        if initialize_update_lock; then
            lock_claimed=true
        fi
    fi
    remove_quarantined_update_lock "$quarantined_lock" || true
    exec {recovery_fd}>&-
    [[ "$lock_claimed" == true ]]
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
        >> "$UPDATE_LOG" 2>&1 < /dev/null 198>&- &
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
    start_maintenance || return 1
    PRESERVE_MAINTENANCE=true
}

prepare_python_wheelhouse() {
    local source_root="$1" destination="$2" builder_python="$3"
    mkdir -p "$destination"
    run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" "$builder_python" -m pip wheel \
        --wheel-dir "$destination" \
        -c "$source_root/server/requirements.lock" \
        -r "$source_root/server/requirements.txt"
}

install_python_wheelhouse() {
    local source_root="$1" wheelhouse="$2" target_python="${3:-$ROOT_DIR/venv/bin/python}"
    [[ -x "$target_python" ]] || return 1
    [[ -d "$wheelhouse" ]] || return 1
    run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" "$target_python" -m pip install \
        --no-index \
        --find-links "$wheelhouse" \
        -c "$source_root/server/requirements.lock" \
        -r "$source_root/server/requirements.txt" \
        || return 1
    run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" "$target_python" -m pip check
}

validate_current_python_environment() {
    [[ -x "$ROOT_DIR/venv/bin/python" ]] || return 1
    run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" "$ROOT_DIR/venv/bin/python" -m pip check
}

installed_web_build_matches() {
    local expected="${1:0:12}" marker="$ROOT_DIR/web/dist/.streamhome-build" actual=""
    [[ -s "$ROOT_DIR/web/dist/index.html" && -f "$marker" ]] || return 1
    actual="$(tr -d '[:space:]' < "$marker")"
    [[ -n "$actual" && "$actual" == "$expected" ]]
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
    if [[ "$OLD_COMMIT" != "$TARGET_COMMIT" ]]; then
        WEB_BUILD_REQUIRED=true
        if git -C "$ROOT_DIR" diff --quiet "$OLD_COMMIT" "$TARGET_COMMIT" -- web; then
            log "Web sources are unchanged; rebuilding production assets to embed the exact target build identity."
        fi
    else
        WEB_BUILD_REQUIRED=false
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

    run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" python3 -m venv "$staged_checkout/venv" || return 1
    builder_python="$staged_checkout/venv/bin/python"
    "$builder_python" -m pip --version >/dev/null || "$builder_python" -m ensurepip --upgrade || return 1
    log "Python dependency manifests changed; preparing candidate and rollback packages."
    prepare_python_wheelhouse "$staged_checkout" "$STAGING_DIR/candidate-wheels" "$builder_python" || return 1
    prepare_python_wheelhouse "$ROOT_DIR" "$STAGING_DIR/rollback-wheels" "$builder_python" || return 1
    install_python_wheelhouse "$staged_checkout" "$STAGING_DIR/candidate-wheels" "$builder_python"
}

prepare_candidate_web() {
    local staged_checkout="$1" candidate_web="$1/web" candidate_build="${TARGET_COMMIT:0:12}"
    if [[ "$WEB_BUILD_REQUIRED" == false ]]; then
        [[ -d "$ROOT_DIR/web/node_modules" ]] || return 1
        installed_web_build_matches "$OLD_COMMIT" || return 1
        log "The release commit is unchanged; reusing its verified production assets."
        return 0
    fi

    if [[ "$WEB_DEPENDENCIES_CHANGED" == true ]]; then
        log "Web dependency manifests changed; installing the candidate dependency tree once."
        (cd "$candidate_web" && run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" npm ci --prefer-offline --no-audit --no-fund) || return 1
    else
        [[ -d "$ROOT_DIR/web/node_modules" ]] || return 1
        ln -s "$ROOT_DIR/web/node_modules" "$candidate_web/node_modules" || return 1
        log "Web dependency manifests are unchanged; building with the verified installed dependency tree."
    fi

    if ! (cd "$candidate_web" \
        && run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" env VITE_BUILD_ID="$candidate_build" STREAMHOME_BUILD_ID="$candidate_build" npm run build); then
        [[ "$WEB_DEPENDENCIES_CHANGED" == false ]] && rm -f -- "$candidate_web/node_modules"
        return 1
    fi
    if [[ "$WEB_DEPENDENCIES_CHANGED" == false ]]; then
        rm -f -- "$candidate_web/node_modules"
    fi
    [[ -s "$candidate_web/dist/index.html" ]] \
        && [[ "$(tr -d '[:space:]' < "$candidate_web/dist/.streamhome-build" 2>/dev/null || true)" == "$candidate_build" ]]
}

activate_prepared_web() {
    local candidate_web="$STAGING_DIR/checkout/web"
    if [[ "$WEB_BUILD_REQUIRED" == false ]]; then
        write_state "installing" "The release commit is unchanged; keeping its existing production assets."
        return 0
    fi
    [[ -s "$candidate_web/dist/index.html" ]] || return 1
    [[ "$(tr -d '[:space:]' < "$candidate_web/dist/.streamhome-build" 2>/dev/null || true)" == "${TARGET_COMMIT:0:12}" ]] \
        || return 1
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
    run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" git clone --shared --no-checkout "$ROOT_DIR" "$staged_checkout" || return 1
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
        run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" ./test.sh --syntax-only
        run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" ./venv/bin/python -m compileall -q server
        run_bounded "$UPDATE_COMMAND_TIMEOUT_SECONDS" env PYTHONPATH=server ./venv/bin/python server/scratch/test_update_system.py
    ) || return 1
    log "Candidate scripts, code, required dependencies, rollback artifacts, and production assets are ready."
}

stop_installed_runtime_with_target_lifecycle() {
    local lifecycle_root="" temporary_root="" result=0
    if [[ -f "$STAGING_DIR/checkout/stop.sh" \
        && -f "$STAGING_DIR/checkout/server/scratch/runtime_control.py" ]]; then
        lifecycle_root="$STAGING_DIR/checkout"
    else
        temporary_root="$(mktemp -d "$RUN_DIR/update-lifecycle.XXXXXX")" || return 1
        mkdir -p "$temporary_root/server/scratch" || {
            rmdir -- "$temporary_root" 2>/dev/null || true
            return 1
        }
        if ! git -C "$ROOT_DIR" show "$TARGET_COMMIT:stop.sh" > "$temporary_root/stop.sh" \
            || ! git -C "$ROOT_DIR" show "$TARGET_COMMIT:server/scratch/runtime_control.py" \
                > "$temporary_root/server/scratch/runtime_control.py"; then
            rm -f -- "$temporary_root/stop.sh"
            rm -f -- "$temporary_root/server/scratch/runtime_control.py"
            rmdir -- "$temporary_root/server/scratch" 2>/dev/null || true
            rmdir -- "$temporary_root/server" 2>/dev/null || true
            rmdir -- "$temporary_root" 2>/dev/null || true
            log "The target release lifecycle controller could not be materialized."
            return 1
        fi
        chmod 700 "$temporary_root/stop.sh"
        lifecycle_root="$temporary_root"
    fi

    log "Stopping the installed runtime with the target release lifecycle controller."
    STREAMHOME_ROOT_OVERRIDE="$ROOT_DIR" bash "$lifecycle_root/stop.sh" --quiet || result=$?

    if [[ -n "$temporary_root" ]]; then
        rm -f -- "$temporary_root/stop.sh"
        rm -f -- "$temporary_root/server/scratch/runtime_control.py"
        rmdir -- "$temporary_root/server/scratch" 2>/dev/null || true
        rmdir -- "$temporary_root/server" 2>/dev/null || true
        rmdir -- "$temporary_root" 2>/dev/null || true
    fi
    return "$result"
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
    validate_lifecycle_authority || return 1
    log "Rolling back to known-working commit ${OLD_COMMIT:0:12}."
    write_state "rolling_back" "The update failed. Restoring the previous StreamHome release." "update_failed" "$TARGET_COMMIT"
    if ! stop_installed_runtime_with_target_lifecycle; then
        log "The failed target runtime could not be stopped; rollback will not mutate code or data while it may still be active."
        return 1
    fi
    start_maintenance || true
    preserve_unexpected_worktree_changes || return 1
    validate_lifecycle_authority || return 1
    git -C "$ROOT_DIR" reset --hard "$OLD_COMMIT" || return 1
    if [[ -f "$RUN_DIR/pre-update-database.db" ]]; then
        validate_lifecycle_authority || return 1
        restore_database_checkpoint || return 1
    fi
    restore_previous_web || return 1
    if [[ "$PYTHON_DEPENDENCIES_CHANGED" == true ]]; then
        install_python_wheelhouse "$ROOT_DIR" "$STAGING_DIR/rollback-wheels" || return 1
    else
        validate_current_python_environment || return 1
    fi
    stop_maintenance
    if ! start_installed_update_runtime; then
        start_maintenance || true
        return 1
    fi
    RUNTIME_HEALTHY=true
    CUTOVER_STARTED=false
    ACTIVATION_STARTED=false
    write_state "rolled_back" "The update failed, and the previous healthy release was restored." "update_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
    promote_recovered_guarded_runtime "$OLD_COMMIT" || return 1
    return 0
}

recover_unchanged_release() {
    local message="$1" error="$2"
    stop_maintenance
    if start_installed_update_runtime; then
        RUNTIME_HEALTHY=true
        CUTOVER_STARTED=false
        ACTIVATION_STARTED=false
        write_state "failed" "$message The existing release remains installed and healthy." "$error" "$TARGET_COMMIT" "$OLD_COMMIT"
        promote_recovered_guarded_runtime "$OLD_COMMIT" || return 1
        rm -f -- "$RUN_DIR/pre-update-database.db"
        return 0
    fi
    RUNTIME_HEALTHY=false
    CUTOVER_STARTED=false
    ACTIVATION_STARTED=false
    mark_lifecycle_recovery_required || true
    write_state "failed" "$message Automatic startup recovery remains available." "$error" "$TARGET_COMMIT" "$OLD_COMMIT"
    if start_maintenance; then
        PRESERVE_MAINTENANCE=true
    fi
    LIFECYCLE_PRESERVE=true
    return 1
}

record_rollback_failure() {
    RECOVERY_IN_PROGRESS=true
    mark_lifecycle_recovery_required || true
    write_state "rollback_failed" "The update and automatic rollback both failed. Review update.log." "rollback_failed" "$TARGET_COMMIT"
    start_maintenance || true
    PRESERVE_MAINTENANCE=true
    PRESERVE_RECOVERY_ARTIFACTS=true
    LIFECYCLE_PRESERVE=true
}

runtime_endpoints_ready() {
    local configured_port expected_commit="${1:-}" expected_transaction="${2:-}"
    configured_port="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    [[ "$configured_port" =~ ^[0-9]+$ && "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || return 1
    python3 - "$configured_port" "${expected_commit:0:12}" "$expected_transaction" <<'PY'
import json
import sys
import time
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(f"http://127.0.0.1:8000/api/health?update_probe={time.time_ns()}", timeout=1.5) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if (
            response.status >= 400
            or payload.get("status") != "ready"
            or str(payload.get("buildId") or "") != sys.argv[2]
            or (sys.argv[3] and str(payload.get("updateTransaction") or "") != sys.argv[3])
        ):
            raise SystemExit(1)
    with opener.open(f"http://127.0.0.1:{int(sys.argv[1])}/?update_probe={time.time_ns()}", timeout=1.5) as response:
        web_build = str(response.headers.get("X-StreamHome-Web-Build") or "")
        raise SystemExit(0 if 200 <= response.status < 400 and web_build == sys.argv[2] else 1)
except Exception:
    raise SystemExit(1)
PY
}

runtime_backend_live() {
    local expected_commit="${1:-}"
    [[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || return 1
    python3 - "${expected_commit:0:12}" <<'PY'
import json
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:8000/api/health/live", timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    raise SystemExit(
        0
        if (
            response.status < 400
            and payload.get("status") == "live"
            and str(payload.get("buildId") or "") == sys.argv[1]
        )
        else 1
    )
except Exception:
    raise SystemExit(1)
PY
}

commit_updated_runtime() {
    local expected_commit="${1:-$TARGET_COMMIT}" code
    validate_lifecycle_authority || return 1
    if [[ -z "$COMMIT_TOKEN" && -n "$COMMIT_FILE" ]]; then
        COMMIT_TOKEN="$(cat "$COMMIT_FILE" 2>/dev/null || true)"
    fi
    [[ -n "$COMMIT_TOKEN" && -n "$TRANSACTION_ID" ]] || return 1
    code="$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
        -H "Content-Type: application/json" \
        -H "X-StreamHome-Update-Commit: $COMMIT_TOKEN" \
        --data "{\"transaction_id\":\"$TRANSACTION_ID\",\"target_commit\":\"$expected_commit\"}" \
        -X POST "http://127.0.0.1:8000/api/update/commit" 2>/dev/null || true)"
    if [[ "$code" == "200" ]]; then
        rm -f -- "$COMMIT_FILE"
        NO_ROLLBACK_BOUNDARY=true
        CUTOVER_STARTED=false
        ACTIVATION_STARTED=false
        LIFECYCLE_PRESERVE=true
        PRESERVE_RECOVERY_ARTIFACTS=true
        return 0
    fi
    if [[ "$code" == "000" || "$code" =~ ^5 ]]; then
        # Delivery or server failure can occur after the journal replacement.
        # The controller must preserve the candidate rather than race a
        # possibly accepted runtime with automatic rollback.
        NO_ROLLBACK_BOUNDARY=true
        CUTOVER_STARTED=false
        ACTIVATION_STARTED=false
        LIFECYCLE_PRESERVE=true
        PRESERVE_RECOVERY_ARTIFACTS=true
        return 2
    fi
    return 1
}

wait_for_committed_runtime() {
    local expected_commit="${1:-$TARGET_COMMIT}"
    [[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || return 1
    python3 - "${expected_commit:0:12}" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

expected = sys.argv[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
unavailable = 0
while True:
    try:
        with opener.open("http://127.0.0.1:8000/api/health/live", timeout=2) as response:
            live = json.loads(response.read().decode("utf-8"))
        if (
            response.status >= 400
            or live.get("status") != "live"
            or str(live.get("buildId") or "") != expected
        ):
            raise OSError("unexpected guarded runtime identity")
        unavailable = 0
        if live.get("recoverySucceeded") is True and live.get("workersStarted") is True:
            raise SystemExit(0)
        if live.get("recoveryCompleted") and not live.get("recoverySucceeded"):
            raise SystemExit(2)
    except Exception:
        unavailable += 1
        if unavailable >= 30:
            raise SystemExit(2)
        time.sleep(1)
        continue

    time.sleep(1)
PY
}

record_post_commit_recovery_failure() {
    mark_lifecycle_recovery_required || true
    write_state \
        "recovering" \
        "The accepted release failed mandatory recovery and remains fail-closed; automatic code/database rollback is disabled." \
        "post_commit_recovery_failed" \
        "$TARGET_COMMIT" \
        "$TARGET_COMMIT"
    RUNTIME_HEALTHY=false
    CUTOVER_STARTED=false
    ACTIVATION_STARTED=false
    NO_ROLLBACK_BOUNDARY=true
    LIFECYCLE_PRESERVE=true
    PRESERVE_RECOVERY_ARTIFACTS=true
}

installed_release_has_guarded_bootstrap() {
    [[ -f "$ROOT_DIR/server/lifecycle/bootstrap.py" ]]
}

prepare_lifecycle_for_guarded_start() {
    [[ "$LIFECYCLE_ENABLED" == true ]] || return 0
    [[ "$LIFECYCLE_PHASE" != "recovery_required" ]] || return 1
    if [[ "$LIFECYCLE_PHASE" == "reserved" ]]; then
        advance_lifecycle_phase exclusive || return 1
    fi
    if [[ "$LIFECYCLE_PHASE" == "exclusive" ]]; then
        advance_lifecycle_phase verifying || return 1
    fi
    [[ "$LIFECYCLE_PHASE" == "verifying" ]] || return 1
    validate_lifecycle_authority
}

ensure_guarded_commit_token() {
    if [[ -z "$COMMIT_TOKEN" ]]; then
        COMMIT_TOKEN="$(new_secret_token)"
    fi
    [[ -n "$COMMIT_TOKEN" ]] || return 1
    COMMIT_FILE="$RUN_DIR/update-commit.${TRANSACTION_ID}.token"
    (umask 077; printf '%s' "$COMMIT_TOKEN" > "$COMMIT_FILE")
}

start_installed_update_runtime() {
    if ! installed_release_has_guarded_bootstrap; then
        "$ROOT_DIR/start.sh" --update-recovery-complete 198>&-
        return
    fi
    prepare_lifecycle_for_guarded_start || return 1
    ensure_guarded_commit_token || return 1
    env \
        STREAMHOME_UPDATE_TRANSACTION="$TRANSACTION_ID" \
        STREAMHOME_UPDATE_COMMIT_TOKEN="$COMMIT_TOKEN" \
        STREAMHOME_LIFECYCLE_STARTUP_MODE="update_verify" \
        STREAMHOME_LIFECYCLE_OPERATION_ID="$LIFECYCLE_OPERATION_ID" \
        STREAMHOME_LIFECYCLE_GENERATION="$LIFECYCLE_GENERATION" \
        STREAMHOME_LIFECYCLE_FIRST_ADOPTION="$([[ "$LIFECYCLE_ENABLED" == true ]] && printf false || printf true)" \
        "$ROOT_DIR/start.sh" --update-recovery-complete 198>&-
}

promote_recovered_guarded_runtime() {
    local expected_commit="$1" commit_result
    installed_release_has_guarded_bootstrap || return 0
    commit_updated_runtime "$expected_commit"
    commit_result=$?
    if [[ "$commit_result" -eq 2 ]]; then
        record_post_commit_recovery_failure
        return 2
    fi
    [[ "$commit_result" -eq 0 ]] || return 1
    write_state "recovering" "The accepted release is completing mandatory recovery without automatic rollback." "" "" "$expected_commit"
    if ! wait_for_committed_runtime "$expected_commit"; then
        record_post_commit_recovery_failure
        return 1
    fi
    LIFECYCLE_PRESERVE=false
    PRESERVE_RECOVERY_ARTIFACTS=false
    if [[ "$LIFECYCLE_ENABLED" == true ]]; then
        release_lifecycle_authority || return 1
    fi
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
    local phase current_head recovered_staging web_swapped runtime_committed commit_result
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
print("true" if payload.get("runtime_committed", False) else "false")
print(str(payload.get("lifecycle_generation") or ""))
print(str(payload.get("lifecycle_phase") or ""))
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
    runtime_committed="${recovery_state[10]:-false}"
    LIFECYCLE_GENERATION="${recovery_state[11]:-}"
    LIFECYCLE_PHASE="${recovery_state[12]:-}"
    [[ -n "$TRANSACTION_ID" ]] || TRANSACTION_ID="$(new_transaction_id)"
    LIFECYCLE_OPERATION_ID="$TRANSACTION_ID"
    COMMIT_FILE="$RUN_DIR/update-commit.${TRANSACTION_ID}.token"
    STAGING_DIR="$recovered_staging"
    WEB_ARTIFACTS_SWAPPED="$web_swapped"
    if [[ -n "$STAGING_DIR" ]] && ! recovery_staging_is_safe; then
        STAGING_DIR=""
    fi
    case "$phase" in
        preflight|waiting_for_idle|stopping|installing|starting|rolling_back|recovering|succeeded|rolled_back)
            ;;
        *)
            return 10
            ;;
    esac
    [[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ && "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || return 1
    if ! acquire_update_lock; then
        return 11
    fi
    initialize_lifecycle_authority || return 1
    trap cleanup EXIT
    current_head="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$runtime_committed" == true ]]; then
        NO_ROLLBACK_BOUNDARY=true
        CUTOVER_STARTED=false
        ACTIVATION_STARTED=false
        LIFECYCLE_PRESERVE=true
        PRESERVE_RECOVERY_ARTIFACTS=true
        if [[ "$current_head" != "$TARGET_COMMIT" ]]; then
            record_post_commit_recovery_failure
            return 1
        fi
        if ! runtime_backend_live "$TARGET_COMMIT"; then
            stop_maintenance
            if ! start_installed_update_runtime; then
                record_post_commit_recovery_failure
                return 1
            fi
            if ! runtime_endpoints_ready "$TARGET_COMMIT" "$TRANSACTION_ID"; then
                record_post_commit_recovery_failure
                return 1
            fi
            commit_updated_runtime
            commit_result=$?
            if [[ "$commit_result" -ne 0 ]]; then
                record_post_commit_recovery_failure
                return 1
            fi
        fi
        write_state "recovering" "The accepted release is completing mandatory recovery without automatic rollback." "" "" "$TARGET_COMMIT"
        if ! wait_for_committed_runtime "$TARGET_COMMIT"; then
            record_post_commit_recovery_failure
            return 1
        fi
        stop_maintenance
        rm -f -- "$RUN_DIR/pre-update-database.db"
        RUNTIME_HEALTHY=true
        LIFECYCLE_PRESERVE=false
        PRESERVE_RECOVERY_ARTIFACTS=false
        write_state "succeeded" "The accepted release completed mandatory recovery; interrupted bookkeeping was reconciled." "" "" "$TARGET_COMMIT"
        return 12
    fi
    if [[ "$current_head" == "$TARGET_COMMIT" ]] \
        && runtime_endpoints_ready "$TARGET_COMMIT" "$TRANSACTION_ID"; then
        COMMIT_TOKEN="$(cat "$COMMIT_FILE" 2>/dev/null || true)"
        commit_updated_runtime
        commit_result=$?
        if [[ "$commit_result" -ne 0 ]]; then
            if [[ "$commit_result" -eq 2 ]]; then
                record_post_commit_recovery_failure
            fi
            return 1
        fi
        write_state "recovering" "The accepted release is completing mandatory recovery without automatic rollback." "" "" "$TARGET_COMMIT"
        if ! wait_for_committed_runtime "$TARGET_COMMIT"; then
            record_post_commit_recovery_failure
            return 1
        fi
        stop_maintenance
        rm -f -- "$RUN_DIR/pre-update-database.db"
        RUNTIME_HEALTHY=true
        CUTOVER_STARTED=false
        LIFECYCLE_PRESERVE=false
        PRESERVE_RECOVERY_ARTIFACTS=false
        write_state "succeeded" "The updated release was already healthy; interrupted bookkeeping was reconciled." "" "" "$TARGET_COMMIT"
        return 12
    fi
    if [[ "$current_head" == "$OLD_COMMIT" ]] && runtime_endpoints_ready "$OLD_COMMIT"; then
        if [[ "$LIFECYCLE_ENABLED" == true && "$LIFECYCLE_PHASE" != "reserved" ]]; then
            log "The previous runtime may retain a process-local cutover fence; replacing it before lifecycle completion."
            RUNTIME_HEALTHY=false
            CUTOVER_STARTED=true
            if ! stop_installed_runtime_with_target_lifecycle; then
                record_rollback_failure
                return 1
            fi
            start_maintenance || true
            prepare_lifecycle_for_guarded_start || {
                record_rollback_failure
                return 1
            }
            write_state "rolled_back" "The previous release remained installed and will be restarted without the abandoned cutover fence." "update_interrupted_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
            stop_maintenance
            if ! start_installed_update_runtime; then
                record_rollback_failure
                return 1
            fi
            RUNTIME_HEALTHY=true
            CUTOVER_STARTED=false
            if ! promote_recovered_guarded_runtime "$OLD_COMMIT"; then
                if [[ "$NO_ROLLBACK_BOUNDARY" == false ]]; then
                    record_rollback_failure
                fi
                return 1
            fi
            rm -f -- "$RUN_DIR/pre-update-database.db"
            PRESERVE_RECOVERY_ARTIFACTS=false
            RECOVERY_IN_PROGRESS=false
            return 12
        fi
        stop_maintenance
        rm -f -- "$RUN_DIR/pre-update-database.db"
        RUNTIME_HEALTHY=true
        CUTOVER_STARTED=false
        write_state "rolled_back" "The previous release was already healthy; interrupted bookkeeping was reconciled." "update_interrupted_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
        return 12
    fi
    if [[ "$current_head" == "$OLD_COMMIT" && ! -f "$RUN_DIR/pre-update-database.db" ]]; then
        stop_maintenance
        write_state "failed" "An interrupted preflight was cleared; the installed release was unchanged." "update_interrupted" "$TARGET_COMMIT" "$OLD_COMMIT"
        return 10
    fi
    RECOVERY_IN_PROGRESS=true
    PRESERVE_RECOVERY_ARTIFACTS=true
    write_state "rolling_back" "Recovering an update interrupted before health verification." "update_interrupted" "$TARGET_COMMIT" "$current_head"
    if ! stop_installed_runtime_with_target_lifecycle; then
        record_rollback_failure
        return 1
    fi
    preserve_unexpected_worktree_changes || {
        record_rollback_failure
        return 1
    }
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
    if installed_release_has_guarded_bootstrap; then
        stop_maintenance
        if ! start_installed_update_runtime; then
            record_rollback_failure
            return 1
        fi
        RUNTIME_HEALTHY=true
        CUTOVER_STARTED=false
        write_state "rolled_back" "The interrupted update was rolled back and the previous release passed guarded startup verification." "update_interrupted_rolled_back" "$TARGET_COMMIT" "$OLD_COMMIT"
        promote_recovered_guarded_runtime "$OLD_COMMIT" || {
            record_rollback_failure
            return 1
        }
        rm -f -- "$RUN_DIR/pre-update-database.db"
        PRESERVE_RECOVERY_ARTIFACTS=false
        RECOVERY_IN_PROGRESS=false
        return 12
    fi
    LIFECYCLE_PRESERVE=true
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
print(str(payload.get("lifecycle_generation") or ""))
print(str(payload.get("lifecycle_phase") or ""))
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
    LIFECYCLE_GENERATION="${recovery_state[9]:-}"
    LIFECYCLE_PHASE="${recovery_state[10]:-}"
    LIFECYCLE_OPERATION_ID="$TRANSACTION_ID"
    STAGING_DIR="$recovered_staging"
    WEB_ARTIFACTS_SWAPPED="$web_swapped"
    [[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ && "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ && -n "$TRANSACTION_ID" ]] || return 1
    if [[ -n "$STAGING_DIR" ]] && ! recovery_staging_is_safe; then
        STAGING_DIR=""
    fi
    acquire_update_lock || return 1
    initialize_lifecycle_authority || return 1
    trap cleanup EXIT
    current_head="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
    [[ "$current_head" == "$OLD_COMMIT" ]] || return 1
    runtime_endpoints_ready "$OLD_COMMIT" || return 1
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
    local commit_result cutover_result
    RUN_DIR="$ROOT_DIR/.run"
    STATUS_FILE="$RUN_DIR/update-state.json"
    UPDATE_LOG="$ROOT_DIR/update.log"
    UPDATE_LOCK="$RUN_DIR/update.lock"
    LEASE_FILE="$RUN_DIR/update-lease.json"
    MAINTENANCE_PID_FILE="$RUN_DIR/maintenance.pid"
    MAINTENANCE_START_FILE="$RUN_DIR/maintenance.start"
    TRANSACTION_ID="${LIFECYCLE_OPERATION_ID:-$(new_transaction_id)}"
    COMMIT_FILE="$RUN_DIR/update-commit.${TRANSACTION_ID}.token"
    CANCEL_FILE="$RUN_DIR/update-cancel.${TRANSACTION_ID}.requested"
    if [[ "$MANUAL_CUTOVER" == false ]]; then
        UPDATE_BRANCH="$(read_update_branch "$ROOT_DIR")"
    fi
    WEB_PORT="$(read_env "$ROOT_DIR/.env" WEB_PORT 3000)"
    PUBLIC_URL="$(read_env "$ROOT_DIR/.env" PUBLIC_URL "")"
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP

    [[ "$UPDATE_COMMAND_TIMEOUT_SECONDS" =~ ^[0-9]+$ \
        && "$UPDATE_COMMAND_TIMEOUT_SECONDS" -ge 60 \
        && "$UPDATE_COMMAND_TIMEOUT_SECONDS" -le 7200 ]] || {
        write_state "failed" "Update timeout configuration is invalid." "invalid_update_timeout" "$TARGET_COMMIT"
        return 1
    }
    if [[ "$START_AFTER_UPDATE" == false ]]; then
        write_state "failed" "Existing installations cannot be updated with --no-start because health-gated rollback would be unavailable." "no_start_update_unsupported" "$TARGET_COMMIT"
        return 1
    fi
    if ! command -v timeout >/dev/null 2>&1; then
        write_state "failed" "The timeout command is required for bounded update operations." "timeout_command_unavailable" "$TARGET_COMMIT"
        return 1
    fi

    if ! acquire_update_lock; then
        write_state "failed" "Another update controller already owns the update lock." "update_in_progress" "$TARGET_COMMIT"
        return 1
    fi
    if ! initialize_lifecycle_authority; then
        write_state "failed" "Shared lifecycle ownership could not be established for this update." "lifecycle_unavailable" "$TARGET_COMMIT"
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
    if cancellation_requested; then
        record_cancelled_update
        return 0
    fi
    if git -C "$ROOT_DIR" status --porcelain --untracked-files=normal | grep -q .; then
        write_state "failed" "The checkout changed during preflight. The running installation was not modified." "dirty_worktree" "$TARGET_COMMIT"
        return 1
    fi
    write_state "waiting_for_idle" "The official update.sh controller is waiting for protected cutover admission."
    log "The official update.sh controller owns cutover; active sessions will be disconnected."
    if public_origin_matches "$OLD_COMMIT"; then
        PUBLIC_ORIGIN_WAS_READY=true
        log "The configured public origin is healthy on the installed build; post-update ingress verification is required."
    else
        log "The configured public origin could not be verified from this host; local health gates remain authoritative."
    fi

    request_process_local_cutover
    cutover_result=$?
    if [[ "$cutover_result" -ne 0 ]]; then
        if [[ "$cutover_result" -eq 20 ]] || cancellation_requested; then
            record_cancelled_update
            return 0
        fi
        write_state "failed" "Protected cutover admission could not be established; the running installation was not changed." "cutover_admission_failed" "$TARGET_COMMIT"
        return 1
    fi
    write_state "stopping" "Prepared release is ready. Stopping StreamHome for a short protected activation."
    CUTOVER_STARTED=true
    RUNTIME_HEALTHY=false
    validate_lifecycle_authority || return 1
    if git -C "$ROOT_DIR" status --porcelain --untracked-files=normal | grep -q .; then
        recover_unchanged_release \
            "The checkout changed before shutdown, so the update was not applied." \
            "dirty_worktree" || true
        return 1
    fi
    if ! stop_installed_runtime_with_target_lifecycle; then
        recover_unchanged_release \
            "StreamHome did not stop cleanly, so the update was not applied." \
            "shutdown_failed" || true
        return 1
    fi
    if ! start_maintenance; then
        recover_unchanged_release \
            "The maintenance responder could not start, so the update was not applied." \
            "maintenance_start_failed" || true
        return 1
    fi
    validate_lifecycle_authority || return 1
    if ! create_database_checkpoint; then
        recover_unchanged_release \
            "The database recovery checkpoint failed, so the update was not applied." \
            "database_checkpoint_failed" || true
        return 1
    fi

    write_state "installing" "Activating the exact preflighted commit and prepared runtime assets."
    validate_lifecycle_authority || return 1
    if ! git -C "$ROOT_DIR" status --porcelain --untracked-files=normal | grep -q .; then
        :
    else
        log "The checkout became dirty before cutover."
        recover_unchanged_release \
            "The checkout changed during shutdown, so the update was not applied." \
            "dirty_worktree" || true
        return 1
    fi
    if ! git -C "$ROOT_DIR" merge --ff-only "$TARGET_COMMIT"; then
        if [[ "$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)" == "$OLD_COMMIT" ]]; then
            recover_unchanged_release "The target commit could not be activated." "activation_failed" || true
        else
            ACTIVATION_STARTED=true
            rollback_release || record_rollback_failure
        fi
        return 1
    fi
    ACTIVATION_STARTED=true
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
    prepare_lifecycle_for_guarded_start || {
        rollback_release || record_rollback_failure
        return 1
    }
    stop_maintenance
    COMMIT_TOKEN="$(new_secret_token)"
    [[ -n "$COMMIT_TOKEN" ]] || {
        start_maintenance || true
        rollback_release || record_rollback_failure
        return 1
    }
    (umask 077; printf '%s' "$COMMIT_TOKEN" > "$COMMIT_FILE")
    if ! start_installed_update_runtime; then
        start_maintenance || true
        rollback_release || record_rollback_failure
        return 1
    fi
    if ! runtime_endpoints_ready "$TARGET_COMMIT" "$TRANSACTION_ID"; then
        log "The local runtime did not expose the exact guarded target build; rolling back."
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
    commit_updated_runtime
    commit_result=$?
    if [[ "$commit_result" -ne 0 ]]; then
        if [[ "$commit_result" -eq 2 ]]; then
            log "The guarded commit result is indeterminate; preserving the target without automatic rollback."
            record_post_commit_recovery_failure
            return 1
        fi
        log "The exact target build could not commit its guarded update transaction; rolling back."
        rollback_release || record_rollback_failure
        return 1
    fi
    write_state "recovering" "The accepted release is completing mandatory recovery without automatic rollback." "" "" "$TARGET_COMMIT"
    if ! wait_for_committed_runtime "$TARGET_COMMIT"; then
        log "The accepted target failed mandatory recovery; preserving it fail-closed without automatic rollback."
        record_post_commit_recovery_failure
        return 1
    fi
    record_prepared_setup_state
    RUNTIME_HEALTHY=true
    CUTOVER_STARTED=false
    ACTIVATION_STARTED=false
    NO_ROLLBACK_BOUNDARY=true
    LIFECYCLE_PRESERVE=false
    PRESERVE_RECOVERY_ARTIFACTS=false
    rm -f -- "$RUN_DIR/pre-update-database.db"
    write_state "succeeded" "Update installed successfully; both StreamHome services passed health checks." "" "" "$TARGET_COMMIT"
    log "Update completed successfully at ${TARGET_COMMIT:0:12}."
}

case "${1:-}" in
    "")
        update_branch="$(read_update_branch "$ORIGINAL_ROOT")"
        exec env \
            STREAMHOME_INSTALL_DIR="$ORIGINAL_ROOT" \
            STREAMHOME_REF="${STREAMHOME_REF:-$update_branch}" \
            bash "$ORIGINAL_ROOT/install.sh"
        ;;
    --no-start)
        [[ $# -eq 1 ]] || exit 2
        update_branch="$(read_update_branch "$ORIGINAL_ROOT")"
        exec env \
            STREAMHOME_INSTALL_DIR="$ORIGINAL_ROOT" \
            STREAMHOME_REF="${STREAMHOME_REF:-$update_branch}" \
            bash "$ORIGINAL_ROOT/install.sh" --no-start
        ;;
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
        [[ "$AUTOMATIC" == "true" || "$AUTOMATIC" == "false" ]] || exit 2
        [[ "$HANDOFF_FILE" == "$ROOT_DIR"/.run/update-handoff.*.token ]] || exit 2
        log "Accepting a legacy managed-update request; update.sh now owns protected cutover directly."
        execute_update
        ;;
    --manual-execute)
        [[ $# -eq 6 ]] || exit 2
        TARGET_COMMIT="$2"
        OLD_COMMIT="$3"
        UPDATE_BRANCH="$4"
        START_AFTER_UPDATE="$5"
        ROOT_DIR="$(cd "$6" && pwd -P)"
        AUTOMATIC="${STREAMHOME_UPDATE_AUTOMATIC:-false}"
        MANUAL_CUTOVER=true
        HANDOFF_FILE=""
        [[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ && "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 2
        [[ "$AUTOMATIC" == "true" || "$AUTOMATIC" == "false" ]] || exit 2
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
        usage
        ;;
    *)
        printf 'Unknown update argument: %s (use --help for usage)\n' "$1" >&2
        exit 2
        ;;
esac
