#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUN_SERVER=true
RUN_WEB=true
SYNTAX_ONLY=false
CURRENT_STEP="release-test initialization"
PYTHON="python3"
[[ -x "$ROOT_DIR/venv/bin/python" ]] && PYTHON="$ROOT_DIR/venv/bin/python"

usage() {
    cat <<'EOF'
StreamHome Linux release checks

Usage:
  ./test.sh [--server-only | --web-only | --syntax-only] [--help]

Options:
  --server-only Run shell checks, server regressions, pip check, and database check.
  --web-only    Run shell checks, frontend tests, TypeScript lint, and build.
  --syntax-only Run Bash syntax and ShellCheck (when installed) only.
  --help        Show this help text.

The StreamHome runtime must be stopped because server checks use the canonical
server/database.db and the production build replaces web/dist.
EOF
}

fail() {
    printf '\n[StreamHome Tests] ERROR: %s\n' "$1" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    printf '\n[StreamHome Tests] ERROR: %s failed near line %s (exit %s).\n' "$CURRENT_STEP" "$1" "$exit_code" >&2
    exit "$exit_code"
}
trap 'on_error $LINENO' ERR

port_is_listening() {
    "$PYTHON" - "$1" <<'PY'
import socket
import sys

with socket.socket() as probe:
    probe.settimeout(0.25)
    raise SystemExit(0 if probe.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

configured_web_port() {
    "$PYTHON" - "$ROOT_DIR/.env" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = "3000"
if path.is_file():
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == "WEB_PORT":
            value = candidate.strip().strip("\"'")
            break
try:
    port = int(value, 10)
except ValueError:
    raise SystemExit(1)
if not 1 <= port <= 65535:
    raise SystemExit(1)
print(port)
PY
}

assert_runtime_stopped() {
    local pid_file pid web_port
    for pid_file in "$ROOT_DIR/.run/backend.pid" "$ROOT_DIR/.run/web.pid"; do
        [[ -f "$pid_file" ]] || continue
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            fail "StreamHome process PID $pid is still running. Run ./stop.sh before release checks."
        fi
    done
    if port_is_listening 8000; then
        fail "Port 8000 is active. Stop StreamHome or the conflicting service before release checks."
    fi
    web_port="$(configured_web_port)" || fail "WEB_PORT is invalid in .env."
    if port_is_listening "$web_port"; then
        fail "Web port $web_port is active. Stop StreamHome or the conflicting service before release checks."
    fi
}

run_shell_checks() {
    local -a scripts=(
        "$ROOT_DIR/install.sh"
        "$ROOT_DIR/restart.sh"
        "$ROOT_DIR/update.sh"
        "$ROOT_DIR/setup.sh"
        "$ROOT_DIR/start.sh"
        "$ROOT_DIR/stop.sh"
        "$ROOT_DIR/test.sh"
    )
    CURRENT_STEP="Bash syntax validation"
    bash -n "${scripts[@]}"
    if command -v shellcheck >/dev/null 2>&1; then
        CURRENT_STEP="ShellCheck validation"
        shellcheck -x "${scripts[@]}"
    else
        printf '[shell] ShellCheck is not installed; Bash syntax validation passed.\n'
    fi

    CURRENT_STEP="generated-artifact tracking validation"
    local tracked_generated
    tracked_generated="$(git -C "$ROOT_DIR" ls-files 'web/*.tsbuildinfo' 'server/system_profile.json')"
    [[ -z "$tracked_generated" ]] \
        || fail "Generated runtime or build metadata must not be tracked: $tracked_generated"
}

run_server_checks() {
    local -a server_checks=(
        scratch/test_2fa.py
        scratch/test_admin_profile_data.py
        scratch/test_auth_security.py
        scratch/test_auth_validation.py
        scratch/test_backup_security.py
        scratch/test_cloud_streaming.py
        scratch/test_drive_setup.py
        scratch/test_ffmpeg_headers.py
        scratch/test_ingest_stream_script.py
        scratch/test_integration_credentials.py
        scratch/test_maintenance_recovery.py
        scratch/test_playback_contract.py
        scratch/test_playback_pipeline.py
        scratch/test_profile_security.py
        scratch/test_queue_failure_handling.py
        scratch/test_rclone_fallback.py
        scratch/test_recommendation_system.py
        scratch/test_search_caching.py
        scratch/test_security_validation.py
        scratch/test_setup_scripts.py
        scratch/test_totp_enrollment.py
        scratch/test_update_system.py
        scratch/test_vibe_analysis.py
    )
    command -v "$PYTHON" >/dev/null 2>&1 || [[ -x "$PYTHON" ]] || fail "Python is unavailable. Run ./setup.sh first."
    cd "$ROOT_DIR/server"
    export PYTHONPATH=.
    local check
    for check in "${server_checks[@]}"; do
        CURRENT_STEP="server check $check"
        printf '[server] %s\n' "$check"
        "$PYTHON" "$check"
    done
    CURRENT_STEP="Python dependency integrity"
    "$PYTHON" -m pip check
    CURRENT_STEP="database and environment check"
    "$PYTHON" scratch/check_db.py
}

run_web_checks() {
    command -v npm >/dev/null 2>&1 || fail "npm is unavailable. Run ./setup.sh first."
    cd "$ROOT_DIR/web"
    CURRENT_STEP="frontend test suite"
    npm run test
    CURRENT_STEP="TypeScript lint"
    npm run lint
    CURRENT_STEP="production web build"
    npm run build
    [[ -s "$ROOT_DIR/web/dist/index.html" ]] || fail "The production web build did not create web/dist/index.html."
}

main() {
    local selected_mode=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --server-only)
                [[ -z "$selected_mode" ]] || fail "Choose only one test scope."
                selected_mode="server"
                RUN_WEB=false
                ;;
            --web-only)
                [[ -z "$selected_mode" ]] || fail "Choose only one test scope."
                selected_mode="web"
                RUN_SERVER=false
                ;;
            --syntax-only)
                [[ -z "$selected_mode" ]] || fail "Choose only one test scope."
                selected_mode="syntax"
                RUN_SERVER=false
                RUN_WEB=false
                SYNTAX_ONLY=true
                ;;
            --help|-h)
                usage
                return 0
                ;;
            *)
                fail "Unknown argument: $1 (use --help for usage)"
                ;;
        esac
        shift
    done

    command -v bash >/dev/null 2>&1 || fail "Bash is required."
    run_shell_checks
    if [[ "$SYNTAX_ONLY" == true ]]; then
        printf 'Shell-script checks passed.\n'
        return 0
    fi

    assert_runtime_stopped
    [[ "$RUN_SERVER" == true ]] && run_server_checks
    [[ "$RUN_WEB" == true ]] && run_web_checks
    printf 'All requested release checks passed.\n'
}

main "$@"
