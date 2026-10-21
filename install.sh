#!/usr/bin/env bash
set -Eeuo pipefail

# This frontend always runs unprivileged. The verified Python frontend stages
# and authenticates an independent publisher package before any custom root code.
if [[ "${EUID}" -eq 0 ]]; then
    printf '%s\n' '[StreamHome] Run this installer as your normal user; it requests sudo for verified administrative steps.' >&2
    exit 2
fi
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    cat <<'EOF'
StreamHome managed installer

Usage: install.sh [--request FILE]

Supported platform: Ubuntu 24.04 x86_64 with systemd and Python 3.12.3.
Application: /opt/streamhome; configuration: /etc/streamhome;
state/media: /var/lib/streamhome. Installation paths are fixed.

The interactive wizard reads /dev/tty, including when piped into bash.
A complete explicit JSON request is required without a controlling terminal.
An existing installation is inspected and checked; retrying does not reinstall
or automatically update it. Legacy data is never moved by this installer.
EOF
    exit 0
fi
for executable in /usr/bin/python3 /usr/bin/curl /usr/bin/mktemp /usr/bin/sha256sum; do
    [[ -x "$executable" ]] || { printf '[StreamHome] Missing bootstrap program: %s\n' "$executable" >&2; exit 2; }
done
bootstrap_directory="$(/usr/bin/mktemp -d /tmp/streamhome-bootstrap.XXXXXXXX)"
cleanup() {
    /usr/bin/rm -f -- "$bootstrap_directory/bootstrap_install.py"
    /usr/bin/rmdir -- "$bootstrap_directory" 2>/dev/null || true
}
trap cleanup EXIT
/usr/bin/curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 120 --max-filesize 262144 \
    https://raw.githubusercontent.com/StreamHome/StreamHome/main/scripts/bootstrap_install.py \
    --output "$bootstrap_directory/bootstrap_install.py"
printf '%s  %s\n' 'e034f24121ed89da04a4c508983ab9f4bad1f1645e7b4185731b2151cdd13d9f' "$bootstrap_directory/bootstrap_install.py" | /usr/bin/sha256sum --check --status || {
    printf '%s\n' '[StreamHome] Bootstrap source verification failed. Retry once the official source/cache is consistent; nothing was elevated.' >&2
    exit 2
}
/usr/bin/python3 -I "$bootstrap_directory/bootstrap_install.py" "$@"
