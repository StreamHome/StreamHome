#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/StreamHome/StreamHome.git"
INSTALL_DIR="${STREAMHOME_INSTALL_DIR:-${HOME:-}/StreamHome}"
INSTALL_REF="${STREAMHOME_REF:-main}"
NO_START=false
SKIP_SYSTEM_PACKAGES=false
INSTALL_PARENT=""
INSTALL_LOCK=""
STAGING_DIR=""
EXISTING_UPDATE_COMPLETE=false
EXISTING_ALREADY_CURRENT=false

usage() {
    cat <<'EOF'
StreamHome bootstrap installer

Usage:
  install.sh [--no-start] [--skip-system-packages] [--help]

Options:
  --no-start             Install and build without starting StreamHome.
  --skip-system-packages Do not install missing operating-system packages.
  --help                 Show this help text.

Environment overrides:
  STREAMHOME_INSTALL_DIR Installation directory (default: ~/StreamHome)
  STREAMHOME_REF         Git branch or tag (default: main)

The installer clones or safely fast-forwards StreamHome, grants executable
permissions to its shell entry points, and runs setup.sh.
EOF
}

log() {
    printf '\n[StreamHome] %s\n' "$1"
}

fail() {
    printf '\n[StreamHome] ERROR: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
        case "$STAGING_DIR" in
            "$INSTALL_PARENT"/.streamhome-install.*)
                rm -rf -- "$STAGING_DIR"
                ;;
        esac
    fi
    if [[ -n "$INSTALL_LOCK" && -d "$INSTALL_LOCK" ]]; then
        rm -f -- "$INSTALL_LOCK/owner.pid"
        rmdir -- "$INSTALL_LOCK" 2>/dev/null || true
    fi
}
trap cleanup EXIT

run_privileged() {
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail "Administrator privileges are required to install Git. Install Git manually and run this command again."
    fi
}

install_git() {
    command -v git >/dev/null 2>&1 && return 0
    log "Git is missing; attempting installation"
    if command -v apt-get >/dev/null 2>&1; then
        run_privileged apt-get update
        run_privileged apt-get install -y git ca-certificates
    elif command -v dnf >/dev/null 2>&1; then
        run_privileged dnf install -y git ca-certificates
    elif command -v yum >/dev/null 2>&1; then
        run_privileged yum install -y git ca-certificates
    elif command -v pacman >/dev/null 2>&1; then
        run_privileged pacman -Sy --needed --noconfirm git ca-certificates
    else
        fail "Git is required. Install Git with your Linux distribution package manager and retry."
    fi
    command -v git >/dev/null 2>&1 || fail "Git installation completed but the git command is still unavailable. Open a new terminal and retry."
}

valid_remote() {
    case "${1%/}" in
        https://github.com/StreamHome/StreamHome|https://github.com/StreamHome/StreamHome.git|git@github.com:StreamHome/StreamHome.git|https://github.com/WaqSea/StreamHome|https://github.com/WaqSea/StreamHome.git|git@github.com:WaqSea/StreamHome.git)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

normalize_install_path() {
    local requested parent name
    requested="$INSTALL_DIR"
    [[ -n "$requested" ]] || fail "STREAMHOME_INSTALL_DIR may not be empty."
    name="$(basename "$requested")"
    [[ "$name" != "." && "$name" != ".." && "$name" != "/" ]] || fail "Choose a dedicated StreamHome installation directory."
    parent="$(dirname "$requested")"
    [[ -d "$parent" ]] || mkdir -p "$parent"
    INSTALL_PARENT="$(cd "$parent" && pwd -P)"
    INSTALL_DIR="$INSTALL_PARENT/$name"
}

acquire_install_lock() {
    local owner=""
    INSTALL_LOCK="${INSTALL_DIR}.install.lock"
    if mkdir "$INSTALL_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$INSTALL_LOCK/owner.pid"
        return 0
    fi
    owner="$(cat "$INSTALL_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
        fail "Another StreamHome installation or update is already running for $INSTALL_DIR."
    fi
    rm -f -- "$INSTALL_LOCK/owner.pid"
    rmdir -- "$INSTALL_LOCK" 2>/dev/null || fail "The installation lock is stale but could not be removed: $INSTALL_LOCK"
    mkdir "$INSTALL_LOCK" || fail "Could not acquire the installation lock: $INSTALL_LOCK"
    printf '%s\n' "$$" > "$INSTALL_LOCK/owner.pid"
}

prepare_existing_checkout() {
    local remote dirty fetched_commit head_commit controller start_after_update
    remote="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    valid_remote "$remote" || fail "The existing directory is not a StreamHome checkout from $REPOSITORY_URL"
    git -C "$INSTALL_DIR" remote set-url origin "$REPOSITORY_URL"
    dirty="$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=normal)"
    [[ -z "$dirty" ]] || fail "The existing StreamHome checkout has local changes. Commit or move them before updating."

    log "Preparing a health-gated update for the existing StreamHome checkout"
    if [[ "$(git -C "$INSTALL_DIR" rev-parse --is-shallow-repository)" == "true" ]]; then
        git -C "$INSTALL_DIR" fetch --unshallow origin "$INSTALL_REF"
    else
        git -C "$INSTALL_DIR" fetch origin "$INSTALL_REF"
    fi
    fetched_commit="$(git -C "$INSTALL_DIR" rev-parse 'FETCH_HEAD^{commit}')"
    head_commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    if [[ "$fetched_commit" == "$head_commit" ]]; then
        log "The existing StreamHome installation is already at the requested release"
        EXISTING_UPDATE_COMPLETE=true
        EXISTING_ALREADY_CURRENT=true
        return 0
    fi
    git -C "$INSTALL_DIR" merge-base --is-ancestor "$head_commit" "$fetched_commit" \
        || fail "The requested update is not a safe fast-forward from the installed release."

    STAGING_DIR="$(mktemp -d "$INSTALL_PARENT/.streamhome-install.XXXXXX")"
    controller="$STAGING_DIR/update-controller.sh"
    git -C "$INSTALL_DIR" show "$fetched_commit:update.sh" > "$controller"
    chmod 700 "$controller"
    start_after_update=true
    [[ "$NO_START" == true ]] && start_after_update=false
    log "Running the prepared update controller; StreamHome will stay online until cutover"
    bash "$controller" --manual-execute \
        "$fetched_commit" \
        "$head_commit" \
        "$INSTALL_REF" \
        "$start_after_update" \
        "$INSTALL_DIR"
    [[ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" == "$fetched_commit" ]] \
        || fail "The health-gated update did not finish at the requested commit."
    EXISTING_UPDATE_COMPLETE=true
}

existing_runtime_ready() {
    local python_bin="$INSTALL_DIR/venv/bin/python" web_port
    [[ -x "$python_bin" ]] || python_bin="$(command -v python3 || true)"
    [[ -n "$python_bin" ]] || return 1
    web_port="$(
        awk -F= '
            /^[[:space:]]*WEB_PORT[[:space:]]*=/ {
                value=$0
                sub(/^[^=]*=/, "", value)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                gsub(/^["'"'"']|["'"'"']$/, "", value)
                print value
                exit
            }
        ' "$INSTALL_DIR/.env" 2>/dev/null || true
    )"
    [[ -n "$web_port" ]] || web_port=3000
    [[ "$web_port" =~ ^[0-9]+$ ]] || return 1
    "$python_bin" - "$web_port" <<'PY'
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

recover_current_installation() {
    [[ "$NO_START" == false && "$EXISTING_ALREADY_CURRENT" == true ]] || return 0
    if existing_runtime_ready; then
        log "The existing StreamHome services are already healthy"
        return 0
    fi
    log "The checkout is current but its services are unhealthy; starting recovery"
    "$INSTALL_DIR/start.sh" \
        || fail "The installed release is current, but StreamHome could not be recovered. Review backend.log, frontend.log, update.log, and restart.log."
    existing_runtime_ready \
        || fail "StreamHome started without restoring both local health endpoints."
    log "The current StreamHome release was recovered successfully"
}

prepare_new_checkout() {
    local checkout
    if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        fail "The installation directory is not empty and is not a StreamHome checkout: $INSTALL_DIR"
    fi

    STAGING_DIR="$(mktemp -d "$INSTALL_PARENT/.streamhome-install.XXXXXX")"
    checkout="$STAGING_DIR/checkout"
    log "Cloning StreamHome into a temporary checkout"
    git clone --depth 1 --branch "$INSTALL_REF" "$REPOSITORY_URL" "$checkout"
    [[ -d "$checkout/.git" ]] || fail "The temporary StreamHome checkout is incomplete."

    if [[ -d "$INSTALL_DIR" ]]; then
        rmdir -- "$INSTALL_DIR" || fail "The installation directory must remain empty during bootstrap: $INSTALL_DIR"
    fi
    mv -- "$checkout" "$INSTALL_DIR"
    rmdir -- "$STAGING_DIR"
    STAGING_DIR=""
    log "Installed StreamHome into $INSTALL_DIR"
}

prepare_checkout() {
    if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ]]; then
        fail "The installation path exists and is not a directory: $INSTALL_DIR"
    fi
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        prepare_existing_checkout
    else
        prepare_new_checkout
    fi
}

main() {
    local -a setup_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-start)
                NO_START=true
                ;;
            --skip-system-packages)
                SKIP_SYSTEM_PACKAGES=true
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

    [[ -n "${HOME:-}" || -n "${STREAMHOME_INSTALL_DIR:-}" ]] || fail "HOME is unavailable; set STREAMHOME_INSTALL_DIR explicitly."
    [[ "$(uname -s)" == "Linux" ]] || fail "The alpha server installer supports Linux only."
    [[ "$INSTALL_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "STREAMHOME_REF contains unsupported characters."
    [[ "$INSTALL_REF" != *".."* ]] || fail "STREAMHOME_REF may not contain '..'."

    normalize_install_path
    acquire_install_lock
    install_git
    prepare_checkout

    if [[ "$EXISTING_UPDATE_COMPLETE" == true ]]; then
        recover_current_installation
        log "Existing StreamHome installation updated successfully"
        return 0
    fi

    log "Granting executable permissions"
    chmod +x \
        "$INSTALL_DIR/install.sh" \
        "$INSTALL_DIR/restart.sh" \
        "$INSTALL_DIR/update.sh" \
        "$INSTALL_DIR/setup.sh" \
        "$INSTALL_DIR/start.sh" \
        "$INSTALL_DIR/stop.sh" \
        "$INSTALL_DIR/test.sh"

    [[ "$NO_START" == true ]] && setup_args+=(--no-start)
    [[ "$SKIP_SYSTEM_PACKAGES" == true ]] && setup_args+=(--skip-system-packages)

    log "Starting StreamHome setup"
    (
        cd "$INSTALL_DIR"
        ./setup.sh "${setup_args[@]}"
    )
}

main "$@"
