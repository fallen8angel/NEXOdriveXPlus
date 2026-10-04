#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
STATUS_FILE="${NEXO_YOLO_STATUS_FILE:-/tmp/nexo-yolo-direct-status.json}"

cleanup() {
  [[ -n "${YOLO_PID:-}" ]] && kill "$YOLO_PID" 2>/dev/null || true
  [[ -n "${BEACON_PID:-}" ]] && kill "$BEACON_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

# Carrot-style direct path:
# comma roadEncodeData -> Jetson HEVC decode -> YOLO
# Do not leave the old bridge/frame-worker path running in parallel.
pkill -f "orin_frame_bridge.py" 2>/dev/null || true
pkill -f "orin_yolo_worker.py" 2>/dev/null || true
pkill -f "openpilot/cereal/messaging/bridge .* roadEncodeData" 2>/dev/null || true
pkill -f "jetson_status_beacon.py" 2>/dev/null || true
rm -f "$STATUS_FILE"

bash "$SCRIPT_DIR/run_nexo_yolo_direct.sh" &
YOLO_PID=$!

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  BEACON_PY="$ROOT_DIR/.venv/bin/python"
else
  BEACON_PY="python3"
fi

PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  NEXO_YOLO_STATUS_FILE="$STATUS_FILE" \
  "$BEACON_PY" "$SCRIPT_DIR/jetson_status_beacon.py" &
BEACON_PID=$!

set +e
wait -n "$YOLO_PID" "$BEACON_PID"
rc=$?
set -e
echo "[nexo-yolo] child exited rc=$rc; systemd will restart the service" >&2
exit "$rc"
