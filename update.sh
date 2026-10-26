#!/usr/bin/env bash
# StreamHome compatibility entry: scripts/runtime/update.sh
set -Eeuo pipefail

# Older protected updaters copy the target's root entry into .run before the
# target checkout is active. Resolve the implementation from that exact commit,
# preserving the controller PID and its inherited canonical handoff descriptors.
if [[ "${BASH_SOURCE[0]}" == */.run/update-controller.*.sh && "${1:-}" == --manual-execute ]]; then
    [[ $# -eq 6 && "$2" =~ ^[0-9a-f]{40}$ ]] || exit 2
    SCRIPT_ROOT="$(cd "$6" && pwd -P)"
    COMPAT_CONTROLLER="$(mktemp "$SCRIPT_ROOT/.run/update-controller.compat.XXXXXXXX.sh")"
    trap 'rm -f -- "$COMPAT_CONTROLLER"' EXIT
    if ! git -C "$SCRIPT_ROOT" show "$2:scripts/runtime/update.sh" > "$COMPAT_CONTROLLER"; then
        printf 'The exact target update controller is unavailable.\n' >&2
        exit 1
    fi
    IFS= read -r COMPAT_HEADER < "$COMPAT_CONTROLLER"
    [[ "$COMPAT_HEADER" == '#!/usr/bin/env bash' ]] || exit 1
    chmod 700 "$COMPAT_CONTROLLER"
    # The implementation removes its materialized controller on exit.
    trap - EXIT
    exec bash "$COMPAT_CONTROLLER" "$@"
fi

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$SCRIPT_ROOT/scripts/runtime/update.sh" "$@"
