#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/WaqSea/StreamHome.git"
INSTALL_DIR="${STREAMHOME_INSTALL_DIR:-${HOME}/StreamHome}"
INSTALL_REF="${STREAMHOME_REF:-main}"

usage() {
    cat <<'EOF'
StreamHome bootstrap installer

Usage:
  install.sh [--help]

Environment overrides:
  STREAMHOME_INSTALL_DIR  Installation directory (default: ~/StreamHome)
  STREAMHOME_REF          Git branch or tag (default: main)

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
    elif command -v brew >/dev/null 2>&1; then
        brew install git
    else
        fail "Git is required. Install Git with your operating system package manager and retry."
    fi
    command -v git >/dev/null 2>&1 || fail "Git installation completed but the git command is still unavailable. Open a new terminal and retry."
}

valid_remote() {
    case "${1%/}" in
        https://github.com/WaqSea/StreamHome|https://github.com/WaqSea/StreamHome.git|git@github.com:WaqSea/StreamHome.git)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

prepare_checkout() {
    local parent remote dirty
    parent="$(dirname "$INSTALL_DIR")"
    [[ -d "$parent" ]] || mkdir -p "$parent"

    if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ]]; then
        fail "The installation path exists and is not a directory: $INSTALL_DIR"
    fi

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        remote="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
        valid_remote "$remote" || fail "The existing directory is not a StreamHome checkout from $REPOSITORY_URL"
        dirty="$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=normal)"
        [[ -z "$dirty" ]] || fail "The existing StreamHome checkout has local changes. Commit or move them before updating."

        log "Updating the existing StreamHome checkout"
        git -C "$INSTALL_DIR" fetch --depth 1 origin "$INSTALL_REF"
        if git -C "$INSTALL_DIR" show-ref --verify --quiet "refs/heads/$INSTALL_REF"; then
            git -C "$INSTALL_DIR" checkout "$INSTALL_REF"
            git -C "$INSTALL_DIR" merge --ff-only FETCH_HEAD
        else
            git -C "$INSTALL_DIR" checkout --detach FETCH_HEAD
        fi
        return
    fi

    if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        fail "The installation directory is not empty and is not a StreamHome checkout: $INSTALL_DIR"
    fi

    log "Cloning StreamHome into $INSTALL_DIR"
    git clone --depth 1 --branch "$INSTALL_REF" "$REPOSITORY_URL" "$INSTALL_DIR"
}

main() {
    if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
        usage
        return 0
    fi
    [[ $# -eq 0 ]] || fail "Unknown argument: $1 (use --help for usage)"
    [[ "$INSTALL_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "STREAMHOME_REF contains unsupported characters."
    [[ "$INSTALL_REF" != *".."* ]] || fail "STREAMHOME_REF may not contain '..'."

    install_git
    prepare_checkout

    log "Granting executable permissions"
    chmod +x \
        "$INSTALL_DIR/install.sh" \
        "$INSTALL_DIR/setup.sh" \
        "$INSTALL_DIR/start.sh" \
        "$INSTALL_DIR/stop.sh" \
        "$INSTALL_DIR/test.sh"

    log "Starting StreamHome setup"
    cd "$INSTALL_DIR"
    exec ./setup.sh
}

main "$@"
