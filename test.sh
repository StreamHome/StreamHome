#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="python3"
[[ -x "$ROOT_DIR/venv/bin/python" ]] && PYTHON="$ROOT_DIR/venv/bin/python"

SERVER_CHECKS=(
  scratch/test_cloud_streaming.py
  scratch/test_drive_setup.py
  scratch/test_ffmpeg_headers.py
  scratch/test_ingest_stream_script.py
  scratch/test_playback_contract.py
  scratch/test_playback_pipeline.py
  scratch/test_queue_failure_handling.py
  scratch/test_rclone_fallback.py
  scratch/test_recommendation_system.py
  scratch/test_search_caching.py
  scratch/test_vibe_analysis.py
)

cd "$ROOT_DIR/server"
export PYTHONPATH=.
for check in "${SERVER_CHECKS[@]}"; do
  echo "[server] $check"
  "$PYTHON" "$check"
done
"$PYTHON" scratch/check_db.py

cd "$ROOT_DIR/web"
npm run test
npm run lint
npm run build

echo "All non-security release checks passed."
