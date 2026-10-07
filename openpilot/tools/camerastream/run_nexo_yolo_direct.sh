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
BASE_PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

resolve_python() {
  local candidate="$1"
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || return 1
    printf '%s' "$candidate"
  else
    command -v "$candidate" 2>/dev/null
  fi
}

site_packages() {
  local candidate="$1"
  "$candidate" -c 'import site; print(next((p for p in site.getsitepackages() if p.endswith(("site-packages","dist-packages"))), ""))' 2>/dev/null || true
}

python_ok() {
  local candidate="$1"
  local extra_path="${2:-}"
  local test_path="$BASE_PYTHONPATH"
  [[ -n "$extra_path" ]] && test_path="$extra_path:$test_path"
  PYTHONPATH="$test_path" \
    "$candidate" -c 'import av; import ultralytics; import openpilot.cereal.messaging' >/dev/null 2>&1
}

PY=""
EXTRA_SITE=""

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PY="$(resolve_python "$PYTHON_BIN" || true)"
  if [[ -z "$PY" ]] || ! python_ok "$PY"; then
    echo "[nexo-yolo] PYTHON_BIN cannot import av + ultralytics + openpilot.cereal.messaging: $PYTHON_BIN" >&2
    exit 20
  fi
else
  JETSON_PY="$(resolve_python "$HOME/jetson-yolo/bin/python" || true)"
  OPENPILOT_PY="$(resolve_python "$ROOT_DIR/.venv/bin/python" || true)"
  SYSTEM_PY="$(resolve_python python3 || true)"

  for candidate in "$JETSON_PY" "$OPENPILOT_PY" "$SYSTEM_PY"; do
    [[ -n "$candidate" ]] || continue
    if python_ok "$candidate"; then
      PY="$candidate"
      break
    fi
  done

  if [[ -z "$PY" && -n "$JETSON_PY" && -n "$OPENPILOT_PY" ]]; then
    JETSON_SITE="$(site_packages "$JETSON_PY")"
    OPENPILOT_SITE="$(site_packages "$OPENPILOT_PY")"

    if [[ -n "$JETSON_SITE" ]] && python_ok "$OPENPILOT_PY" "$JETSON_SITE"; then
      PY="$OPENPILOT_PY"
      EXTRA_SITE="$JETSON_SITE"
    elif [[ -n "$OPENPILOT_SITE" ]] && python_ok "$JETSON_PY" "$OPENPILOT_SITE"; then
      PY="$JETSON_PY"
      EXTRA_SITE="$OPENPILOT_SITE"
    fi
  fi
fi

if [[ -z "$PY" ]]; then
  echo "[nexo-yolo] no Python environment can load the direct pipeline." >&2
  echo "[nexo-yolo] required together: av, ultralytics, openpilot.cereal.messaging" >&2
  echo "[nexo-yolo] checked jetson-yolo, openpilot .venv, system python and cross-venv site-packages." >&2
  echo "[nexo-yolo] set PYTHON_BIN in $CONFIG_FILE if a working interpreter exists elsewhere." >&2
  exit 21
fi

INPUT_ARGS=()
if [[ "${NEXO_USB_VIDEO:-0}" == "1" ]]; then
  INPUT_ARGS+=(--usb-input)
elif ! ip route get "$COMMA_IP" >/dev/null 2>&1; then
  echo "[nexo-yolo] no route to COMMA_IP=$COMMA_IP" >&2
  exit 22
fi

export PYTHONPATH="$BASE_PYTHONPATH"
if [[ -n "$EXTRA_SITE" ]]; then
  export PYTHONPATH="$EXTRA_SITE:$PYTHONPATH"
fi
export NEXO_YOLO_STATUS_FILE="$STATUS_FILE"
cd "$ROOT_DIR"

echo "[nexo-yolo] mode=direct comma=$COMMA_IP python=$PY extra_site=${EXTRA_SITE:-none} status=$STATUS_FILE" >&2

exec "$PY" "$SCRIPT_DIR/orin_yolo_direct.py" "$COMMA_IP" \
  "${INPUT_ARGS[@]}" \
  --model "$SCRIPT_DIR/best.pt" \
  --device "${YOLO_DEVICE:-0}" \
  --imgsz "${YOLO_IMGSZ:-640}" \
  --conf "${YOLO_CONF:-0.25}" \
  --skip "${YOLO_SKIP:-2}" \
  --result-port "${YOLO_RESULT_PORT:-8769}" \
  --print-every "${YOLO_PRINT_EVERY:-1}" \
  --status-file "$STATUS_FILE"
