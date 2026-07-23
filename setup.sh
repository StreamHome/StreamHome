#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NO_START=false
SKIP_SYSTEM_PACKAGES=false
CURRENT_STEP="initialization"
MIN_RCLONE_VERSION="1.68"

usage() {
    cat <<'EOF'
StreamHome Unix setup

Usage:
  ./setup.sh [--no-start] [--skip-system-packages] [--help]

Options:
  --no-start             Install and build without starting StreamHome.
  --skip-system-packages Do not install missing operating-system packages.
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

install_app_rclone() {
    CURRENT_STEP="Rclone compatibility installation"
    local os_name arch archive_name archive_url temporary checksum_file extracted binary
    case "$(uname -s)" in
        Linux) os_name="linux" ;;
        Darwin) os_name="osx" ;;
        *) fail "Rclone $MIN_RCLONE_VERSION or newer must be installed manually on this operating system." ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        armv7l) arch="arm-v7" ;;
        armv6l) arch="arm-v6" ;;
        i386|i686) arch="386" ;;
        *) fail "Unsupported Rclone architecture: $(uname -m)." ;;
    esac
    archive_name="rclone-current-${os_name}-${arch}.zip"
    archive_url="https://downloads.rclone.org/${archive_name}"
    temporary="$(mktemp -d)"
    checksum_file="$temporary/${archive_name}.sha256"
    log "Installing a current application-owned Rclone"
    curl -fsSL "$archive_url" -o "$temporary/$archive_name"
    curl -fsSL "${archive_url}.sha256" -o "$checksum_file"
    (
        cd "$temporary"
        if command -v sha256sum >/dev/null 2>&1; then
            sha256sum -c "$(basename "$checksum_file")"
        else
            shasum -a 256 -c "$(basename "$checksum_file")"
        fi
    )
    python3 -m zipfile -e "$temporary/$archive_name" "$temporary/unpacked"
    extracted="$(find "$temporary/unpacked" -type f -name rclone -print -quit)"
    [[ -n "$extracted" ]] || fail "The official Rclone archive did not contain the expected executable."
    mkdir -p "$ROOT_DIR/bin"
    binary="$ROOT_DIR/bin/rclone"
    install -m 0755 "$extracted" "$binary"
    rm -rf "$temporary"
}

missing_commands() {
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=(python)
    command -v node >/dev/null 2>&1 || missing+=(node)
    command -v npm >/dev/null 2>&1 || missing+=(npm)
    command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
    command -v ffprobe >/dev/null 2>&1 || missing+=(ffprobe)
    [[ -n "$(rclone_binary)" ]] || missing+=(rclone)
    command -v git >/dev/null 2>&1 || missing+=(git)
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
        packages=(ca-certificates curl git python3 python3-pip python3-venv nodejs npm ffmpeg rclone lsof)
        run_privileged apt-get update
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
    elif command -v dnf >/dev/null 2>&1; then
        packages=(ca-certificates curl git python3 python3-pip nodejs npm ffmpeg rclone lsof)
        run_privileged dnf install -y "${packages[@]}"
    elif command -v yum >/dev/null 2>&1; then
        packages=(ca-certificates curl git python3 python3-pip nodejs npm ffmpeg rclone lsof)
        run_privileged yum install -y "${packages[@]}"
    elif command -v pacman >/dev/null 2>&1; then
        packages=(ca-certificates curl git python python-pip nodejs npm ffmpeg rclone lsof)
        run_privileged pacman -Sy --needed --noconfirm "${packages[@]}"
    elif command -v brew >/dev/null 2>&1; then
        packages=(git python node ffmpeg rclone lsof)
        brew install "${packages[@]}"
    else
        fail "No supported package manager was found. Install Python 3.11+, Node.js 18+, FFmpeg, FFprobe, and rclone manually."
    fi
}

validate_versions() {
    CURRENT_STEP="runtime version validation"
    command -v python3 >/dev/null 2>&1 || fail "python3 is unavailable after dependency installation."
    command -v node >/dev/null 2>&1 || fail "node is unavailable after dependency installation."
    command -v npm >/dev/null 2>&1 || fail "npm is unavailable after dependency installation."
    command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg is unavailable after dependency installation."
    command -v ffprobe >/dev/null 2>&1 || fail "ffprobe is unavailable after dependency installation."
    [[ -n "$(rclone_binary)" ]] || fail "rclone is unavailable after dependency installation."
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

prepare_virtual_environment() {
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

    CURRENT_STEP="server dependency installation"
    "$ROOT_DIR/venv/bin/python" -m pip install --upgrade pip
    "$ROOT_DIR/venv/bin/python" -m pip install -r "$ROOT_DIR/server/requirements.txt"
}

prepare_web() {
    CURRENT_STEP="web dependency installation"
    (cd "$ROOT_DIR/web" && npm ci)
    CURRENT_STEP="production web build"
    (cd "$ROOT_DIR/web" && npm run build)
}

prepare_environment() {
    CURRENT_STEP="environment initialization"
    if [[ ! -f "$ROOT_DIR/.env" ]]; then
        if [[ -f "$ROOT_DIR/.env.example" ]]; then
            cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
        else
            printf 'SETUP=false\nWEB_PORT=3000\n' > "$ROOT_DIR/.env"
        fi
        chmod 600 "$ROOT_DIR/.env" 2>/dev/null || true
        log "Created .env with first-run setup enabled"
    else
        log "Preserving the existing .env configuration"
    fi

    chmod +x \
        "$ROOT_DIR/install.sh" \
        "$ROOT_DIR/setup.sh" \
        "$ROOT_DIR/start.sh" \
        "$ROOT_DIR/stop.sh" \
        "$ROOT_DIR/test.sh"
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-start) NO_START=true ;;
            --skip-system-packages) SKIP_SYSTEM_PACKAGES=true ;;
            --help|-h) usage; return 0 ;;
            *) fail "Unknown argument: $1 (use --help for usage)" ;;
        esac
        shift
    done

    cd "$ROOT_DIR"
    log "Preparing StreamHome in $ROOT_DIR"
    CURRENT_STEP="system dependency detection"
    missing=()
    while IFS= read -r command_name; do
        [[ -n "$command_name" ]] && missing+=("$command_name")
    done < <(missing_commands)
    install_system_packages "${missing[@]}"
    validate_versions
    prepare_virtual_environment
    prepare_web
    prepare_environment

    log "Setup dependencies and production assets are ready"
    if [[ "$NO_START" == true ]]; then
        printf '[StreamHome Setup] Start later with: ./start.sh\n'
        return 0
    fi

    CURRENT_STEP="StreamHome startup"
    exec "$ROOT_DIR/start.sh"
}

main "$@"
