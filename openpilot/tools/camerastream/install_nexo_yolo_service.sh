#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SERVICE_PATH="/etc/systemd/system/nexo-yolo.service"
CONFIG_DIR="$RUN_HOME/.config"
CONFIG_FILE="$CONFIG_DIR/nexo-yolo.env"

if [[ $EUID -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

detect_comma_ip() {
  local line=""
  line="$(pgrep -af "orin_yolo_direct.py|orin_yolo.py|openpilot/cereal/messaging/bridge" 2>/dev/null || true)"
  sed -nE \
    -e 's/.*orin_yolo_direct\.py[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/p' \
    -e 's/.*orin_yolo\.py[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+).*/\1/p' \
    -e 's/.*bridge[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)[[:space:]]+roadEncodeData.*/\1/p' \
    <<<"$line" | head -n1
}

install -d -m 0755 -o "$RUN_USER" -g "$RUN_USER" "$CONFIG_DIR"

if [[ ! -f "$CONFIG_FILE" ]]; then
  DETECTED_COMMA_IP="$(detect_comma_ip)"
  DEFAULT_COMMA_IP="${COMMA_IP:-${DETECTED_COMMA_IP:-192.168.100.127}}"
  cat >"$CONFIG_FILE" <<EOF
# NEXO Jetson direct YOLO settings.
# Change COMMA_IP when the comma address changes.
COMMA_IP=$DEFAULT_COMMA_IP
YOLO_DEVICE=${YOLO_DEVICE:-0}
YOLO_IMGSZ=${YOLO_IMGSZ:-640}
YOLO_CONF=${YOLO_CONF:-0.25}
YOLO_SKIP=${YOLO_SKIP:-2}
YOLO_PRINT_EVERY=${YOLO_PRINT_EVERY:-1}
EOF
  chown "$RUN_USER:$RUN_USER" "$CONFIG_FILE"
  chmod 0644 "$CONFIG_FILE"
fi

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=NEXO Jetson direct roadEncodeData YOLO
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$ROOT_DIR
Environment=HOME=$RUN_HOME
Environment=NEXO_YOLO_CONFIG=$CONFIG_FILE
ExecStart=/bin/bash $SCRIPT_DIR/start_nexo_yolo_direct.sh
Restart=always
RestartSec=2
KillMode=control-group
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable nexo-yolo.service
systemctl restart nexo-yolo.service

echo "Installed: $SERVICE_PATH"
echo "Config:    $CONFIG_FILE"
echo "ExecStart: $SCRIPT_DIR/start_nexo_yolo_direct.sh"
systemctl --no-pager --full status nexo-yolo.service || true
