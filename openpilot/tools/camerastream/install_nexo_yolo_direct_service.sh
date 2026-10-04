#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
SERVICE_NAME="nexo-yolo.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
TARGET_USER="${SUDO_USER:-$(id -un)}"
COMMA_IP="${COMMA_IP:-192.168.100.127}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "User not found: $TARGET_USER" >&2
  exit 1
fi

chmod +x "$SCRIPT_DIR/start_nexo_yolo_direct.sh" "$SCRIPT_DIR/run_nexo_yolo_direct.sh"

if [[ -f "$SERVICE_FILE" ]]; then
  BACKUP="${SERVICE_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "$SERVICE_FILE" "$BACKUP"
  echo "Backed up existing service: $BACKUP"
fi

systemctl stop "$SERVICE_NAME" 2>/dev/null || true

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=NEXO Jetson Direct roadEncodeData YOLO
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$ROOT_DIR
Environment=COMMA_IP=$COMMA_IP
Environment=YOLO_RESULT_PORT=8769
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/bash $SCRIPT_DIR/start_nexo_yolo_direct.sh
Restart=always
RestartSec=2
KillMode=control-group
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

echo
echo "Installed direct Jetson pipeline."
echo "comma IP: $COMMA_IP"
echo "service: $SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" || true
