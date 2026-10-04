#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
COMMA_IP="${COMMA_IP:-192.168.100.127}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PY="$PYTHON_BIN"
elif [[ -x "$HOME/jetson-yolo/bin/python" ]]; then
  PY="$HOME/jetson-yolo/bin/python"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PY="$ROOT_DIR/.venv/bin/python"
else
  PY="python3"
fi

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT_DIR"

exec "$PY" "$SCRIPT_DIR/orin_yolo_direct.py" "$COMMA_IP" \
  --model "$SCRIPT_DIR/best.pt" \
  --device "${YOLO_DEVICE:-0}" \
  --imgsz "${YOLO_IMGSZ:-640}" \
  --conf "${YOLO_CONF:-0.25}" \
  --skip "${YOLO_SKIP:-2}" \
  --result-port "${YOLO_RESULT_PORT:-8769}" \
  --print-every "${YOLO_PRINT_EVERY:-1}"
