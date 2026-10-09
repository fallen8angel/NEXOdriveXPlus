#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
CONFIG_FILE="${NEXO_YOLO_CONFIG:-$HOME/.config/nexo-yolo.env}"

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

export COMMA_IP="${COMMA_IP:-192.168.100.127}"
export YOLO_DEVICE="${YOLO_DEVICE:-0}"
export YOLO_IMGSZ="${YOLO_IMGSZ:-640}"
export YOLO_CONF="${YOLO_CONF:-.25}"
export YOLO_SKIP="${YOLO_SKIP:-2}"
export YOLO_PRINT_EVERY="${YOLO_PRINT_EVERY:-1}"
export PYTHON_BIN="${PYTHON_BIN:-/home/nexo/jetson-yolo/bin/python}"
export NEXO_YOLO_MODE="bridge"
export NEXO_YOLO_WORKER_LOG="${NEXO_YOLO_WORKER_LOG:-/tmp/nexo-yolo-worker.log}"
export NEXO_FRAME_BRIDGE_LOG="${NEXO_FRAME_BRIDGE_LOG:-/tmp/nexo-frame-bridge.log}"

BRIDGE_BIN="$ROOT_DIR/openpilot/cereal/messaging/bridge"
FRAME_BRIDGE="$SCRIPT_DIR/orin_frame_bridge.py"
YOLO_WORKER="$SCRIPT_DIR/orin_yolo_worker.py"
FRAME_PY="$ROOT_DIR/.venv/bin/python"
BEACON="$SCRIPT_DIR/jetson_status_beacon.py"

for required in "$BRIDGE_BIN" "$FRAME_BRIDGE" "$YOLO_WORKER" "$FRAME_PY" "$PYTHON_BIN" "$BEACON"; do
  if [[ ! -e "$required" ]]; then
    echo "[nexo-yolo] required file missing: $required" >&2
    exit 2
  fi
done

PID_BRIDGE=""
PID_FRAME=""
PID_YOLO=""
PID_BEACON=""

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  for pid in "${PID_YOLO:-}" "${PID_FRAME:-}" "${PID_BRIDGE:-}" "${PID_BEACON:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${PID_YOLO:-}" "${PID_FRAME:-}" "${PID_BRIDGE:-}" "${PID_BEACON:-}"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
  exit "$rc"
}
trap cleanup EXIT INT TERM

cd "$ROOT_DIR"

# Do not let the old direct subscriber or duplicate manual test pipelines run
# beside the production bridge pipeline.
pkill -f "orin_yolo_direct.py" 2>/dev/null || true
pkill -f "orin_frame_bridge.py" 2>/dev/null || true
pkill -f "orin_yolo_worker.py" 2>/dev/null || true
pkill -f "openpilot/cereal/messaging/bridge .* roadEncodeData" 2>/dev/null || true
pkill -f "jetson_status_beacon.py" 2>/dev/null || true
rm -f /tmp/nexo-yolo-direct-status.json
: > "$NEXO_YOLO_WORKER_LOG"
: > "$NEXO_FRAME_BRIDGE_LOG"

while ! ip route get "$COMMA_IP" >/dev/null 2>&1; do
  echo "[nexo-yolo] waiting for route to COMMA_IP=$COMMA_IP" >&2
  sleep 2
done

echo "[nexo-yolo] mode=bridge comma=$COMMA_IP frame_python=$FRAME_PY yolo_python=$PYTHON_BIN" >&2

PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  "$FRAME_PY" -u "$BEACON" &
PID_BEACON=$!

"$BRIDGE_BIN" "$COMMA_IP" roadEncodeData &
PID_BRIDGE=$!

sleep 1

PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  "$FRAME_PY" -u "$FRAME_BRIDGE" > >(tee -a "$NEXO_FRAME_BRIDGE_LOG") 2>&1 &
PID_FRAME=$!

sleep 1

PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" -u "$YOLO_WORKER" > >(tee -a "$NEXO_YOLO_WORKER_LOG") 2>&1 &
PID_YOLO=$!

set +e
wait -n "$PID_BRIDGE" "$PID_FRAME" "$PID_YOLO" "$PID_BEACON"
rc=$?
set -e

echo "[nexo-yolo] bridge pipeline child exited rc=$rc; systemd will restart the service" >&2
exit "$rc"
