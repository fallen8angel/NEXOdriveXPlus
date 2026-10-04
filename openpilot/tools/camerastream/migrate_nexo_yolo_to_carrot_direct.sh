#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
SERVICE_NAME="nexo-yolo.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
CONFIG_DIR="$RUN_HOME/.config"
CONFIG_FILE="$CONFIG_DIR/nexo-yolo.env"

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

detect_comma_ip() {
  local lines
  lines="$(pgrep -af "orin_yolo_direct.py|orin_yolo.py|orin_yolo_worker.py|openpilot/cereal/messaging/bridge" 2>/dev/null || true)"
  sed -nE \
    -e 's/.*orin_yolo_direct\.py[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/p' \
    -e 's/.*orin_yolo\.py[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/p' \
    -e 's/.*bridge[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)[[:space:]]+roadEncodeData.*/\1/p' \
    <<<"$lines" | head -n1
}

DETECTED_COMMA_IP="$(detect_comma_ip)"
COMMA_IP="${COMMA_IP:-${DETECTED_COMMA_IP:-192.168.100.127}}"

if [[ -f "$SERVICE_PATH" ]]; then
  BACKUP="${SERVICE_PATH}.legacy.$(date +%Y%m%d-%H%M%S)"
  cp -a "$SERVICE_PATH" "$BACKUP"
  echo "[migrate] backed up service: $BACKUP"
fi

install -d -m 0755 -o "$RUN_USER" -g "$RUN_USER" "$CONFIG_DIR"
if [[ ! -f "$CONFIG_FILE" ]]; then
  cat >"$CONFIG_FILE" <<EOF
# NEXO Jetson carrot-style direct YOLO settings.
COMMA_IP=$COMMA_IP
YOLO_DEVICE=${YOLO_DEVICE:-0}
YOLO_IMGSZ=${YOLO_IMGSZ:-640}
YOLO_CONF=${YOLO_CONF:-0.25}
YOLO_SKIP=${YOLO_SKIP:-2}
YOLO_PRINT_EVERY=${YOLO_PRINT_EVERY:-1}
EOF
  chown "$RUN_USER:$RUN_USER" "$CONFIG_FILE"
  chmod 0644 "$CONFIG_FILE"
  echo "[migrate] created config: $CONFIG_FILE (COMMA_IP=$COMMA_IP)"
else
  echo "[migrate] preserving existing config: $CONFIG_FILE"
fi

# Stop the old service first so bridge/frame-worker children cannot remain and
# make the diagnostics look connected while no decoded camera frame exists.
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
pkill -f "orin_frame_bridge.py" 2>/dev/null || true
pkill -f "orin_yolo_worker.py" 2>/dev/null || true
pkill -f "orin_yolo.py" 2>/dev/null || true
pkill -f "openpilot/cereal/messaging/bridge .* roadEncodeData" 2>/dev/null || true
pkill -f "jetson_status_beacon.py" 2>/dev/null || true
rm -f /tmp/nexo-yolo-direct-status.json

# The regular installer now points systemd at start_nexo_yolo_direct.sh, which
# uses the carrot-style remote roadEncodeData subscription directly.
COMMA_IP="$COMMA_IP" bash "$SCRIPT_DIR/install_nexo_yolo_service.sh"

DIRECT_OK=false
for _ in $(seq 1 20); do
  if pgrep -f "orin_yolo_direct.py" >/dev/null 2>&1; then
    DIRECT_OK=true
    break
  fi
  sleep 0.5
done

EXEC_START="$(systemctl show "$SERVICE_NAME" -p ExecStart --value 2>/dev/null || true)"
LEGACY_LEFT="$(pgrep -af "orin_frame_bridge.py|orin_yolo_worker.py|openpilot/cereal/messaging/bridge .* roadEncodeData" 2>/dev/null || true)"

echo
echo "[migrate] service ExecStart: $EXEC_START"
echo "[migrate] comma IP: $COMMA_IP"

if [[ "$EXEC_START" != *"start_nexo_yolo_direct.sh"* ]]; then
  echo "[migrate] ERROR: systemd is not using the direct launcher" >&2
  exit 31
fi

if [[ "$DIRECT_OK" != true ]]; then
  echo "[migrate] ERROR: orin_yolo_direct.py did not start" >&2
  journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
  exit 32
fi

if [[ -n "$LEGACY_LEFT" ]]; then
  echo "[migrate] ERROR: legacy pipeline is still running:" >&2
  echo "$LEGACY_LEFT" >&2
  exit 33
fi

echo "[migrate] OK: carrot-style direct roadEncodeData pipeline is active"
echo "[migrate] status: /tmp/nexo-yolo-direct-status.json"
echo "[migrate] logs: journalctl -u $SERVICE_NAME -f"
