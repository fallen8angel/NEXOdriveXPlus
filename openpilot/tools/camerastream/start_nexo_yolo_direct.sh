#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"

cleanup() {
  [[ -n "${YOLO_PID:-}" ]] && kill "$YOLO_PID" 2>/dev/null || true
  [[ -n "${BEACON_PID:-}" ]] && kill "$BEACON_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

# Carrot-style direct path:
# comma roadEncodeData -> Jetson HEVC decode -> YOLO
# No orin_frame_bridge.py / local pipe / orin_yolo_worker.py is required.
bash "$SCRIPT_DIR/run_nexo_yolo_direct.sh" &
YOLO_PID=$!

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  BEACON_PY="$ROOT_DIR/.venv/bin/python"
else
  BEACON_PY="python3"
fi

PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  "$BEACON_PY" "$SCRIPT_DIR/jetson_status_beacon.py" &
BEACON_PID=$!

wait -n "$YOLO_PID" "$BEACON_PID"
