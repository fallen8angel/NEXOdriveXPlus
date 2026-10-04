#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
CONFIG_FILE="${NEXO_YOLO_CONFIG:-$HOME/.config/nexo-yolo.env}"

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

COMMA_IP="${COMMA_IP:-192.168.100.127}"
STATUS_FILE="${NEXO_YOLO_STATUS_FILE:-/tmp/nexo-yolo-direct-status.json}"

python_ok() {
  local candidate="$1"
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || return 1
  else
    command -v "$candidate" >/dev/null 2>&1 || return 1
  fi
  PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$candidate" -c 'import av; import ultralytics; import openpilot.cereal.messaging' >/dev/null 2>&1
}

PY=""
if [[ -n "${PYTHON_BIN:-}" ]]; then
  if python_ok "$PYTHON_BIN"; then
    PY="$PYTHON_BIN"
  else
    echo "[nexo-yolo] PYTHON_BIN cannot import av + ultralytics + openpilot.cereal.messaging: $PYTHON_BIN" >&2
    exit 20
  fi
else
  for candidate in "$HOME/jetson-yolo/bin/python" "$ROOT_DIR/.venv/bin/python" python3; do
    if python_ok "$candidate"; then
      PY="$candidate"
      break
    fi
  done
fi

if [[ -z "$PY" ]]; then
  echo "[nexo-yolo] no Python can import all direct-pipeline modules." >&2
  echo "[nexo-yolo] required: av, ultralytics, openpilot.cereal.messaging" >&2
  echo "[nexo-yolo] set PYTHON_BIN in $CONFIG_FILE after installing the missing modules." >&2
  exit 21
fi

if ! ip route get "$COMMA_IP" >/dev/null 2>&1; then
  echo "[nexo-yolo] no route to COMMA_IP=$COMMA_IP" >&2
  exit 22
fi

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export NEXO_YOLO_STATUS_FILE="$STATUS_FILE"
cd "$ROOT_DIR"

echo "[nexo-yolo] mode=direct comma=$COMMA_IP python=$PY status=$STATUS_FILE" >&2

exec "$PY" "$SCRIPT_DIR/orin_yolo_direct.py" "$COMMA_IP" \
  --model "$SCRIPT_DIR/best.pt" \
  --device "${YOLO_DEVICE:-0}" \
  --imgsz "${YOLO_IMGSZ:-640}" \
  --conf "${YOLO_CONF:-0.25}" \
  --skip "${YOLO_SKIP:-2}" \
  --result-port "${YOLO_RESULT_PORT:-8769}" \
  --print-every "${YOLO_PRINT_EVERY:-1}" \
  --status-file "$STATUS_FILE"
