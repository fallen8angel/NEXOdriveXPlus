#!/usr/bin/env bash
set -euo pipefail

ROOT=${OPENPILOT_ROOT:-/data/openpilot}
MARKER=/data/nexo_jetlink_enabled
STATUS=/dev/shm/nexo-jetlink.json
MODEL_STATUS=/dev/shm/nexo-jetlink-model.json
SPEC=/dev/shm/nexo-jetlink-spec.json
PY=${PYTHON_BIN:-$ROOT/.venv/bin/python}

is_onroad() {
  [[ "$(cat /data/params/d/IsOnroad 2>/dev/null || echo 0)" == "1" ]]
}

set_display_usb_off() {
  PYTHONPATH="$ROOT" "$PY" - <<'PY'
from openpilot.common.params import Params
Params().put_bool('NexoJetsonUsb', False)
PY
}

case "${1:-status}" in
  enable)
    if is_onroad; then
      echo "ERROR: park the vehicle and go offroad before enabling NEXO Jetlink" >&2
      exit 2
    fi
    set_display_usb_off
    touch "$MARKER"
    echo "NEXO Jetlink ENABLED for the next modeld start. NexoJetsonUsb display mode is OFF."
    ;;
  disable)
    if is_onroad; then
      echo "ERROR: park the vehicle and go offroad before disabling NEXO Jetlink" >&2
      exit 2
    fi
    rm -f "$MARKER" "$SPEC"
    pkill -f 'openpilot.selfdrive.modeld.jetlink.daemon' 2>/dev/null || true
    echo "NEXO Jetlink DISABLED. Normal local modeld remains the default."
    ;;
  status)
    echo "enabled=$([[ -e "$MARKER" ]] && echo 1 || echo 0)"
    echo "nexo_display_usb=$(cat /data/params/d/NexoJetsonUsb 2>/dev/null || echo unknown)"
    echo "daemon=$(pgrep -af 'openpilot.selfdrive.modeld.jetlink.daemon' || true)"
    [[ -f "$STATUS" ]] && { echo "usb_status="; cat "$STATUS"; echo; }
    [[ -f "$MODEL_STATUS" ]] && { echo "model_status="; cat "$MODEL_STATUS"; echo; }
    [[ -f "$SPEC" ]] && { echo "model_contract="; cat "$SPEC"; echo; }
    ;;
  *)
    echo "usage: $0 {enable|disable|status}" >&2
    exit 1
    ;;
esac
