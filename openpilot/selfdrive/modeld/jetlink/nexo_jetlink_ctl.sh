#!/usr/bin/env bash
set -euo pipefail

ROOT=${OPENPILOT_ROOT:-/data/openpilot}
MARKER=/data/nexo_jetlink_enabled
MODE_FILE=/data/nexo_jetlink_transport
STATUS=/dev/shm/nexo-jetlink.json
MODEL_STATUS=/dev/shm/nexo-jetlink-model.json
SPEC=/dev/shm/nexo-jetlink-spec.json
DISPLAY_GADGET=/sys/kernel/config/usb_gadget/nexo-display
DISPLAY_MOUNT=/dev/ffs-nexo-display
DISPLAY_SETUP="$ROOT/openpilot/tools/jetson/setup_gadget.sh"
JETLINK_SETUP="$ROOT/openpilot/selfdrive/modeld/jetlink/setup_gadget.sh"

resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    printf '%s\n' "$PYTHON_BIN"
  elif [[ -x /usr/local/venv/bin/python3 ]]; then
    printf '%s\n' /usr/local/venv/bin/python3
  elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    return 1
  fi
}

is_onroad() {
  [[ "$(cat /data/params/d/IsOnroad 2>/dev/null || echo 0)" == "1" ]]
}

set_display_usb_off() {
  local py
  if ! py="$(resolve_python)"; then
    echo "ERROR: Python interpreter not found for NEXO Jetlink control" >&2
    return 1
  fi
  PYTHONPATH="$ROOT" "$py" - <<'PY'
from openpilot.common.params import Params
Params().put_bool('NexoJetsonUsb', False)
PY
}

release_display_usb() {
  # Jetlink inference and the legacy display-only gadget are mutually exclusive.
  # When switching modes offroad, release only our own old display gadget. Never
  # unbind an unrelated AGNOS/USB gadget.
  if [[ ! -d "$DISPLAY_GADGET" ]] && ! mountpoint -q "$DISPLAY_MOUNT" 2>/dev/null; then
    return 0
  fi
  if [[ ! -f "$DISPLAY_SETUP" ]]; then
    echo "ERROR: display USB teardown script not found: $DISPLAY_SETUP" >&2
    return 1
  fi
  if ! sudo -n bash "$DISPLAY_SETUP" --teardown >/dev/null 2>&1; then
    echo "ERROR: could not release stale NEXO display USB gadget; Jetlink was not enabled" >&2
    return 1
  fi
}

release_jetlink_usb_best_effort() {
  if [[ -f "$JETLINK_SETUP" ]]; then
    sudo -n bash "$JETLINK_SETUP" --teardown >/dev/null 2>&1 || true
  fi
}

case "${1:-status}" in
  enable)
    if is_onroad; then
      echo "ERROR: park the vehicle and go offroad before enabling NEXO Jetlink" >&2
      exit 2
    fi
    set_display_usb_off
    release_display_usb
    printf 'usb\n' > "$MODE_FILE"
    touch "$MARKER"
    echo "NEXO Jetlink USB mode ENABLED for the next modeld start. ADB/NCM g1 is never unbound."
    ;;
  enable-ncm-tcp)
    # A deliberately explicit, parked/offroad opt-in to reuse AGNOS g1's NCM
    # interface. This does NOT provision a network, change the UDC or start a
    # Jetson service. Never enable on a guessed/wireless route.
    if is_onroad || [[ "$(cat /data/params/d/IsOffroad 2>/dev/null || echo 0)" != "1" ]]; then
      echo "ERROR: parked/offroad state required before enabling experimental NCM-TCP" >&2
      exit 2
    fi
    if ! ip -o -4 addr show dev usb0 | grep -q '192[.]168[.]100[.]127/24'; then
      echo "ERROR: usb0 needs 192.168.100.127/24 before enabling NCM-TCP" >&2
      exit 2
    fi
    route=$(ip -4 route get 192.168.100.250 from 192.168.100.127 2>/dev/null || true)
    if [[ " $route " != *" dev usb0 "* ]]; then
      echo "ERROR: Jetson NCM path is not via usb0; refusing Wi-Fi/cellular fallback" >&2
      exit 2
    fi
    set_display_usb_off
    release_display_usb
    printf 'ncm-tcp\n' > "$MODE_FILE"
    touch "$MARKER"
    echo "EXPERIMENTAL NCM-TCP selected for the next modeld start. Jetson must listen on USB 192.168.100.250:5599."
    ;;
  disable)
    if is_onroad; then
      echo "ERROR: park the vehicle and go offroad before disabling NEXO Jetlink" >&2
      exit 2
    fi
    rm -f "$MARKER" "$SPEC" "$MODE_FILE"
    pkill -f 'openpilot.selfdrive.modeld.jetlink.daemon' 2>/dev/null || true
    sleep 0.2
    release_jetlink_usb_best_effort
    echo "NEXO Jetlink DISABLED. Normal local modeld remains the default."
    ;;
  status)
    echo "enabled=$([[ -e "$MARKER" ]] && echo 1 || echo 0)"
    echo "transport=$(cat "$MODE_FILE" 2>/dev/null || echo usb)"
    echo "nexo_display_usb=$(cat /data/params/d/NexoJetsonUsb 2>/dev/null || echo unknown)"
    echo "daemon=$(pgrep -af 'openpilot.selfdrive.modeld.jetlink.daemon' || true)"
    [[ -f "$STATUS" ]] && { echo "usb_status="; cat "$STATUS"; echo; }
    [[ -f "$MODEL_STATUS" ]] && { echo "model_status="; cat "$MODEL_STATUS"; echo; }
    [[ -f "$SPEC" ]] && { echo "model_contract="; cat "$SPEC"; echo; }
    ;;
  *)
    echo "usage: $0 {enable|enable-ncm-tcp|disable|status}" >&2
    exit 1
    ;;
esac
