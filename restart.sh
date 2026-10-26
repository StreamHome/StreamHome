#!/usr/bin/env bash
# StreamHome compatibility entry: scripts/runtime/restart.sh
set -Eeuo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$SCRIPT_ROOT/scripts/runtime/restart.sh" "$@"
