#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUN_DIR="$ROOT_DIR/.run"
NO_START=false
SKIP_SYSTEM_PACKAGES=false
FORCE_REBUILD=false
RECORD_PREPARED_STATE=false
CURRENT_STEP="initialization"
MIN_RCLONE_VERSION="1.68"
RCLONE_INSTALL_VERSION="1.74.4"
SETUP_LOCK=""
RCLONE_TEMP=""
RCLONE_STAGED=""
SETUP_STATE_DIR="$RUN_DIR/setup-state"

usage() {
    cat <<'EOF'
StreamHome Linux setup

Usage:
  ./setup.sh [--no-start] [--skip-system-packages] [--force] [--record-prepared-state] [--help]

Options:
  --no-start             Install and build without starting StreamHome.
  --skip-system-packages Do not install missing operating-system packages.
  --force                Reinstall dependencies and rebuild production assets.
  --record-prepared-state
                         Verify prepared dependencies/assets and refresh reuse markers.
  --help                 Show this help text.
EOF
}

log() {
    printf '\n[StreamHome Setup] %s\n' "$1"
}

fail() {
    printf '\n[StreamHome Setup] ERROR: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    if [[ -n "$RCLONE_STAGED" && -f "$RCLONE_STAGED" ]]; then
        rm -f -- "$RCLONE_STAGED"
    fi
    if [[ -n "$RCLONE_TEMP" && -d "$RCLONE_TEMP" ]]; then
        case "$RCLONE_TEMP" in
            "${TMPDIR:-/tmp}"/tmp.*|"${TMPDIR:-/tmp}"/streamhome-rclone.*)
                rm -rf -- "$RCLONE_TEMP"
                ;;
        esac
    fi
    if [[ -n "$SETUP_LOCK" && -d "$SETUP_LOCK" ]]; then
        rm -f -- "$SETUP_LOCK/owner.pid"
        rmdir -- "$SETUP_LOCK" 2>/dev/null || true
    fi
}
trap cleanup EXIT

on_error() {
    local exit_code=$?
    printf '\n[StreamHome Setup] ERROR: %s failed near line %s (exit %s).\n' "$CURRENT_STEP" "$1" "$exit_code" >&2
    if [[ "$CURRENT_STEP" == "StreamHome startup" ]]; then
        printf '[StreamHome Setup] Dependencies and assets are ready. Fix the startup problem and run ./start.sh; do not repeat setup.\n' >&2
    else
        printf '[StreamHome Setup] Fix the reported problem and run ./setup.sh again; existing data was not removed.\n' >&2
    fi
    exit "$exit_code"
}
trap 'on_error $LINENO' ERR

acquire_setup_lock() {
    local owner=""
    [[ -d "$RUN_DIR" ]] || mkdir -p "$RUN_DIR"
    chmod 700 "$RUN_DIR" 2>/dev/null || true
    SETUP_LOCK="$RUN_DIR/setup.lock"
    if mkdir "$SETUP_LOCK" 2>/dev/null; then
        printf '%s\n' "$$" > "$SETUP_LOCK/owner.pid"
        return 0
    fi
    owner="$(cat "$SETUP_LOCK/owner.pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
        fail "Another StreamHome setup is already running."
    fi
    rm -f -- "$SETUP_LOCK/owner.pid"
    rmdir -- "$SETUP_LOCK" 2>/dev/null || fail "The stale setup lock could not be removed: $SETUP_LOCK"
    mkdir "$SETUP_LOCK" || fail "Could not acquire the setup lock."
    printf '%s\n' "$$" > "$SETUP_LOCK/owner.pid"
}

run_privileged() {
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail "Administrator privileges are required to install missing system packages."
    fi
}

rclone_binary() {
    if [[ -x "$ROOT_DIR/bin/rclone" ]]; then
        printf '%s\n' "$ROOT_DIR/bin/rclone"
    else
        command -v rclone 2>/dev/null || true
    fi
}

rclone_version_supported() {
    local binary version
    binary="$(rclone_binary)"
    [[ -n "$binary" ]] || return 1
    version="$("$binary" version 2>/dev/null | sed -n 's/^rclone v\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | head -n 1)"
    [[ -n "$version" ]] || return 1
    python3 - "$version" "$MIN_RCLONE_VERSION" <<'PY'
import sys
current = tuple(map(int, sys.argv[1].split(".")))
minimum = tuple(map(int, sys.argv[2].split(".")))
raise SystemExit(0 if current >= minimum else 1)
PY
}

rclone_archive_details() {
    local machine
    machine="$(uname -m)"
    case "$machine" in
        x86_64|amd64)
            printf '%s %s\n' "amd64" "fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"
            ;;
        aarch64|arm64)
            printf '%s %s\n' "arm64" "97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419"
            ;;
        armv7l)
            printf '%s %s\n' "arm-v7" "75844809d25d2534da96220727e7746a300e30ec8c676ca98c47affe5a752e7b"
            ;;
        armv6l)
            printf '%s %s\n' "arm-v6" "c9e1048feb597938884c0fff314d5d9a002599933cb94ce17fee19599cbfa3f1"
            ;;
        i386|i686)
            printf '%s %s\n' "386" "7feee086d7ff72652c5a91ef4b4a576941ccd33b2929772a2d70471904e516f0"
            ;;
        *)
            fail "Unsupported Rclone architecture: $machine."
            ;;
    esac
}

install_app_rclone() {
    CURRENT_STEP="Rclone compatibility installation"
    local arch expected_hash archive_name archive_url extracted actual_hash binary
    read -r arch expected_hash < <(rclone_archive_details)
    archive_name="rclone-v${RCLONE_INSTALL_VERSION}-linux-${arch}.zip"
    archive_url="https://downloads.rclone.org/v${RCLONE_INSTALL_VERSION}/${archive_name}"
    RCLONE_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/streamhome-rclone.XXXXXX")"

    log "Installing application-owned Rclone v$RCLONE_INSTALL_VERSION"
    curl --fail --silent --show-error --location \
        --retry 3 --retry-delay 1 --connect-timeout 15 \
        "$archive_url" \
        -o "$RCLONE_TEMP/$archive_name"
    actual_hash="$(python3 - "$RCLONE_TEMP/$archive_name" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as archive:
    for block in iter(lambda: archive.read(1024 * 1024), b""):
        digest.update(block)
print(digest.hexdigest())
PY
)"
    [[ "$actual_hash" == "$expected_hash" ]] || fail "The Rclone archive checksum did not match the pinned official release."

    python3 -m zipfile -e "$RCLONE_TEMP/$archive_name" "$RCLONE_TEMP/unpacked"
    extracted="$(find "$RCLONE_TEMP/unpacked" -type f -name rclone -print -quit)"
    [[ -n "$extracted" ]] || fail "The official Rclone archive did not contain the expected executable."

    mkdir -p "$ROOT_DIR/bin"
    binary="$ROOT_DIR/bin/rclone"
    RCLONE_STAGED="$(mktemp "$ROOT_DIR/bin/.rclone.XXXXXX")"
    install -m 0755 "$extracted" "$RCLONE_STAGED"
    "$RCLONE_STAGED" version >/dev/null
    mv -f -- "$RCLONE_STAGED" "$binary"
    RCLONE_STAGED=""

    rm -rf -- "$RCLONE_TEMP"
    RCLONE_TEMP=""
}

missing_commands() {
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=(python)
    command -v node >/dev/null 2>&1 || missing+=(node)
    command -v npm >/dev/null 2>&1 || missing+=(npm)
    command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
    command -v ffprobe >/dev/null 2>&1 || missing+=(ffprobe)
    command -v git >/dev/null 2>&1 || missing+=(git)
    command -v curl >/dev/null 2>&1 || missing+=(curl)
    if ! command -v setsid >/dev/null 2>&1 || ! command -v flock >/dev/null 2>&1; then
        missing+=(util-linux)
    fi
    if ! command -v lsof >/dev/null 2>&1 \
        && ! command -v ss >/dev/null 2>&1 \
        && ! command -v fuser >/dev/null 2>&1; then
        missing+=(listener-inspector)
    fi
    printf '%s\n' "${missing[@]}"
}

install_system_packages() {
    local -a missing=("$@") packages=()
    [[ ${#missing[@]} -gt 0 ]] || return 0
    if [[ "$SKIP_SYSTEM_PACKAGES" == true ]]; then
        fail "Missing required commands: ${missing[*]}. Install them manually or omit --skip-system-packages."
    fi

    log "Installing missing system dependencies: ${missing[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        packages=(ca-certificates curl git python3 python3-pip python3-venv nodejs npm ffmpeg lsof util-linux)
        run_privileged apt-get update
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
    elif command -v dnf >/dev/null 2>&1; then
        packages=(ca-certificates curl git python3 python3-pip nodejs npm ffmpeg lsof util-linux)
        run_privileged dnf install -y "${packages[@]}"
    elif command -v yum >/dev/null 2>&1; then
        packages=(ca-certificates curl git python3 python3-pip nodejs npm ffmpeg lsof util-linux)
        run_privileged yum install -y "${packages[@]}"
    elif command -v pacman >/dev/null 2>&1; then
        packages=(ca-certificates curl git python python-pip nodejs npm ffmpeg lsof util-linux)
        run_privileged pacman -Sy --needed --noconfirm "${packages[@]}"
    else
        fail "No supported Linux package manager was found. Install Python 3.11+, Node.js 18+, FFmpeg, FFprobe, curl, Git, and a listener inspector manually."
    fi
}

validate_versions() {
    CURRENT_STEP="runtime version validation"
    command -v python3 >/dev/null 2>&1 || fail "python3 is unavailable after dependency installation."
    command -v node >/dev/null 2>&1 || fail "node is unavailable after dependency installation."
    command -v npm >/dev/null 2>&1 || fail "npm is unavailable after dependency installation."
    command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg is unavailable after dependency installation."
    command -v ffprobe >/dev/null 2>&1 || fail "ffprobe is unavailable after dependency installation."
    command -v git >/dev/null 2>&1 || fail "git is unavailable after dependency installation."
    command -v curl >/dev/null 2>&1 || fail "curl is unavailable after dependency installation."
    command -v setsid >/dev/null 2>&1 || fail "setsid from util-linux is required for isolated StreamHome service groups."
    command -v flock >/dev/null 2>&1 || fail "flock from util-linux is required for update-lock recovery."
    if ! command -v lsof >/dev/null 2>&1 \
        && ! command -v ss >/dev/null 2>&1 \
        && ! command -v fuser >/dev/null 2>&1; then
        fail "A listener inspector (lsof, ss, or fuser) is required for safe process recovery."
    fi

    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        || fail "Python 3.11 or newer is required."
    local node_major
    node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
    [[ "$node_major" =~ ^[0-9]+$ && "$node_major" -ge 18 ]] \
        || fail "Node.js 18 or newer is required."
    if ! rclone_version_supported; then
        install_app_rclone
    fi
    rclone_version_supported || fail "Rclone $MIN_RCLONE_VERSION or newer is required."
}

stop_existing_runtime() {
    CURRENT_STEP="existing StreamHome shutdown"
    if [[ -x "$ROOT_DIR/stop.sh" ]]; then
        "$ROOT_DIR/stop.sh" --quiet
    fi
}

content_fingerprint() {
    local seed="$1"
    shift
    python3 - "$seed" "$@" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

digest = hashlib.sha256(sys.argv[1].encode("utf-8"))
excluded_directories = {".git", "dist", "node_modules", "__pycache__"}
for raw_path in sys.argv[2:]:
    path = Path(raw_path)
    if path.is_dir():
        candidates = sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and not excluded_directories.intersection(candidate.relative_to(path).parts)
            and candidate.suffix != ".tsbuildinfo"
        )
    else:
        candidates = [path]
    for candidate in candidates:
        digest.update(os.fsencode(str(candidate)))
        try:
            with candidate.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            digest.update(b"<missing>")
print(digest.hexdigest())
PY
}

stamp_matches() {
    local stamp="$1" expected="$2"
    [[ "$FORCE_REBUILD" == false && -f "$stamp" ]] || return 1
    [[ "$(cat "$stamp" 2>/dev/null || true)" == "$expected" ]]
}

write_stamp() {
    local stamp="$1" value="$2" temporary
    mkdir -p "$SETUP_STATE_DIR"
    chmod 700 "$SETUP_STATE_DIR" 2>/dev/null || true
    temporary="$stamp.$$"
    printf '%s\n' "$value" > "$temporary"
    mv -f -- "$temporary" "$stamp"
}

python_dependency_fingerprint() {
    content_fingerprint "$(python3 -VV 2>&1)|$(uname -s)|$(uname -m)" \
        "$ROOT_DIR/server/requirements.txt" \
        "$ROOT_DIR/server/requirements.lock"
}

web_dependency_fingerprint() {
    content_fingerprint "$(node --version)|$(npm --version)|$(uname -s)|$(uname -m)" \
        "$ROOT_DIR/web/package.json" \
        "$ROOT_DIR/web/package-lock.json"
}

web_build_fingerprint() {
    content_fingerprint "$1" "$ROOT_DIR/web"
}

current_build_id() {
    git -C "$ROOT_DIR" rev-parse --short=12 HEAD 2>/dev/null || printf 'dev'
}

web_build_marker_matches() {
    local expected="$1" marker="$ROOT_DIR/web/dist/.streamhome-build" actual=""
    [[ -f "$marker" ]] || return 1
    actual="$(tr -d '[:space:]' < "$marker")"
    [[ -n "$actual" && "$actual" == "$expected" ]]
}

record_prepared_state() {
    local python_fingerprint dependency_fingerprint build_fingerprint build_id
    [[ -x "$ROOT_DIR/venv/bin/python" ]] || fail "The prepared Python environment is missing."
    "$ROOT_DIR/venv/bin/python" -m pip check
    [[ -d "$ROOT_DIR/web/node_modules" && -x "$ROOT_DIR/web/node_modules/.bin/vite" ]] \
        || fail "The prepared web dependency tree is missing or incomplete."
    [[ -s "$ROOT_DIR/web/dist/index.html" ]] || fail "The prepared production web assets are missing."
    build_id="$(current_build_id)"
    web_build_marker_matches "$build_id" \
        || fail "The prepared production web assets do not match release $build_id. Run ./setup.sh --force."
    python_fingerprint="$(python_dependency_fingerprint)"
    dependency_fingerprint="$(web_dependency_fingerprint)"
    build_fingerprint="$(web_build_fingerprint "$dependency_fingerprint")"
    write_stamp "$SETUP_STATE_DIR/python.fingerprint" "$python_fingerprint"
    write_stamp "$SETUP_STATE_DIR/web-dependencies.fingerprint" "$dependency_fingerprint"
    write_stamp "$SETUP_STATE_DIR/web-build.fingerprint" "$build_fingerprint"
    log "Verified prepared dependencies and refreshed setup reuse markers"
}

prepare_virtual_environment() {
    local python_fingerprint python_stamp="$SETUP_STATE_DIR/python.fingerprint"
    CURRENT_STEP="Python virtual environment creation"
    if [[ -d "$ROOT_DIR/venv" && ! -x "$ROOT_DIR/venv/bin/python" ]]; then
        local recovery="$ROOT_DIR/venv.broken.$(date +%Y%m%d%H%M%S)"
        log "Moving the incomplete virtual environment to $recovery"
        mv "$ROOT_DIR/venv" "$recovery"
    fi
    if [[ ! -x "$ROOT_DIR/venv/bin/python" ]]; then
        if ! python3 -m venv "$ROOT_DIR/venv"; then
            if command -v apt-get >/dev/null 2>&1 && [[ "$SKIP_SYSTEM_PACKAGES" == false ]]; then
                run_privileged apt-get install -y python3-venv
                python3 -m venv "$ROOT_DIR/venv"
            else
                fail "Python could not create a virtual environment. Install the Python venv package and retry."
            fi
        fi
    fi

    python_fingerprint="$(python_dependency_fingerprint)"
    if stamp_matches "$python_stamp" "$python_fingerprint" \
        && "$ROOT_DIR/venv/bin/python" -m pip check >/dev/null 2>&1
    then
        log "Python dependency manifests are unchanged; keeping the verified virtual environment"
        return 0
    fi

    CURRENT_STEP="server dependency installation"
    "$ROOT_DIR/venv/bin/python" -m pip --version >/dev/null \
        || "$ROOT_DIR/venv/bin/python" -m ensurepip --upgrade
    "$ROOT_DIR/venv/bin/python" -m pip install \
        -c "$ROOT_DIR/server/requirements.lock" \
        -r "$ROOT_DIR/server/requirements.txt"
    "$ROOT_DIR/venv/bin/python" -m pip check
    write_stamp "$python_stamp" "$python_fingerprint"
}

prepare_web() {
    local dependency_fingerprint build_fingerprint build_id
    local dependency_stamp="$SETUP_STATE_DIR/web-dependencies.fingerprint"
    local build_stamp="$SETUP_STATE_DIR/web-build.fingerprint"
    local web_dependencies_rebuilt=false
    dependency_fingerprint="$(web_dependency_fingerprint)"
    build_id="$(current_build_id)"
    if stamp_matches "$dependency_stamp" "$dependency_fingerprint" \
        && [[ -d "$ROOT_DIR/web/node_modules" ]] \
        && [[ -x "$ROOT_DIR/web/node_modules/.bin/vite" ]]
    then
        log "Web dependency manifests are unchanged; keeping the installed node modules"
    else
        CURRENT_STEP="web dependency installation"
        (cd "$ROOT_DIR/web" && npm ci --prefer-offline --no-audit --no-fund)
        write_stamp "$dependency_stamp" "$dependency_fingerprint"
        web_dependencies_rebuilt=true
    fi

    build_fingerprint="$(web_build_fingerprint "$dependency_fingerprint")"
    if [[ "$web_dependencies_rebuilt" == false ]] \
        && stamp_matches "$build_stamp" "$build_fingerprint" \
        && [[ -s "$ROOT_DIR/web/dist/index.html" ]] \
        && web_build_marker_matches "$build_id"
    then
        log "Frontend build inputs are unchanged; keeping the existing production assets"
        return 0
    fi
    CURRENT_STEP="production web build"
    (cd "$ROOT_DIR/web" && env VITE_BUILD_ID="$build_id" STREAMHOME_BUILD_ID="$build_id" npm run build)
    [[ -s "$ROOT_DIR/web/dist/index.html" ]] || fail "The production web build did not create web/dist/index.html."
    web_build_marker_matches "$build_id" || fail "The production web build identity does not match release $build_id."
    write_stamp "$build_stamp" "$build_fingerprint"
}

prepare_environment() {
    CURRENT_STEP="environment initialization"
    if [[ ! -f "$ROOT_DIR/.env" ]]; then
        if [[ -f "$ROOT_DIR/.env.example" ]]; then
            cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
        else
            printf 'SETUP=false\nWEB_PORT=3000\n' > "$ROOT_DIR/.env"
        fi
        log "Created .env with first-run setup enabled"
    else
        log "Preserving the existing .env configuration"
    fi
    chmod 600 "$ROOT_DIR/.env" 2>/dev/null || true

    chmod +x \
        "$ROOT_DIR/install.sh" \
        "$ROOT_DIR/restart.sh" \
        "$ROOT_DIR/update.sh" \
        "$ROOT_DIR/setup.sh" \
        "$ROOT_DIR/start.sh" \
        "$ROOT_DIR/stop.sh" \
        "$ROOT_DIR/test.sh"
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-start)
                NO_START=true
                ;;
            --skip-system-packages)
                SKIP_SYSTEM_PACKAGES=true
                ;;
            --force)
                FORCE_REBUILD=true
                ;;
            --record-prepared-state)
                RECORD_PREPARED_STATE=true
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

    [[ "$(uname -s)" == "Linux" ]] || fail "The alpha server setup supports Linux only."
    cd "$ROOT_DIR"
    acquire_setup_lock
    if [[ "$RECORD_PREPARED_STATE" == true ]]; then
        CURRENT_STEP="prepared dependency state verification"
        record_prepared_state
        return 0
    fi
    log "Preparing StreamHome in $ROOT_DIR"

    CURRENT_STEP="system dependency detection"
    local -a missing=()
    while IFS= read -r command_name; do
        [[ -n "$command_name" ]] && missing+=("$command_name")
    done < <(missing_commands)
    install_system_packages "${missing[@]}"
    validate_versions
    stop_existing_runtime
    prepare_virtual_environment
    prepare_web
    prepare_environment

    log "Setup dependencies and production assets are ready"
    if [[ "$NO_START" == true ]]; then
        printf '[StreamHome Setup] Start later with: ./start.sh\n'
        return 0
    fi

    CURRENT_STEP="StreamHome startup"
    "$ROOT_DIR/start.sh"
}

main "$@"
