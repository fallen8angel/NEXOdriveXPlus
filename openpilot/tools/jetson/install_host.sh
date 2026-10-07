#!/usr/bin/env bash
# Explicit installation on Jetson only. No JetPack, model or vehicle updates.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run this installer with sudo.' >&2; exit 1; }
ROOT=$(realpath "${1:?repository path required}")
OWNER=${2:?service user required}
PY=$(realpath "${3:?Python executable with built cereal and HUD dependencies required}")
COMMA=${4:--}  # '-' is USB only; do not invent a fallback network address.
VIDEO=${5:-0}
[[ "$VIDEO" == 0 || "$VIDEO" == 1 ]] || { echo 'video must be 0 or 1' >&2; exit 1; }
[[ -f "$ROOT/openpilot/tools/jetson/host.py" && -x "$PY" ]]
[[ -r /etc/nv_tegra_release ]] || { echo 'Jetson L4T installation required; no board is assumed.' >&2; exit 1; }
# Use the actual desktop session, not a guessed JetPack/GDM authorization path.
: "${DISPLAY:?Pass the actual Jetson desktop DISPLAY to sudo}"
: "${XAUTHORITY:?Pass the actual Jetson desktop XAUTHORITY to sudo}"
sudo -u "$OWNER" test -r "$XAUTHORITY"
sudo -u "$OWNER" env PYTHONPATH="$ROOT" "$PY" -m openpilot.tools.jetson.diagnose --role host
command -v ffmpeg >/dev/null
[[ "$(ffmpeg -hide_banner -encoders 2>/dev/null)" == *libx264* ]] || { echo 'ffmpeg libx264 encoder required.' >&2; exit 1; }
id "$OWNER" >/dev/null
for value in "$ROOT" "$PY" "$XAUTHORITY" "$COMMA" "$OWNER"; do
  [[ "$value" != *$'\n'* && "$value" != *'"'* && "$value" != *'%'* && "$value" != *'\\'* ]] || exit 1
done
if [[ "$VIDEO" == 1 ]]; then
  systemctl cat nexo-yolo.service >/dev/null || { echo 'Install the existing direct YOLO service first.' >&2; exit 1; }
  [[ "$(systemctl show -p User --value nexo-yolo.service)" == "$OWNER" ]] || { echo 'YOLO service user must match OWNER.' >&2; exit 1; }
  [[ "$(systemctl show -p WorkingDirectory --value nexo-yolo.service)" == "$ROOT" ]] || { echo 'YOLO service must use this checkout.' >&2; exit 1; }
fi
install -d -o "$OWNER" -g "$(id -gn "$OWNER")" -m 0750 /dev/shm/nexo-jetson
# tmpfiles recreates the shared runtime directory after a reboot.
printf 'd /dev/shm/nexo-jetson 0750 %s %s -\n' "$OWNER" "$(id -gn "$OWNER")" > /etc/tmpfiles.d/nexo-jetson.conf
cat > /etc/udev/rules.d/71-nexo-display.rules <<EOF
SUBSYSTEM=="usb", ATTR{idVendor}=="1209", ATTR{idProduct}=="0001", ATTR{product}=="NEXO display link", OWNER="$OWNER", MODE="0600"
SUBSYSTEM=="usb", ATTR{idVendor}=="1cbe", ATTR{idProduct}=="0092", OWNER="$OWNER", MODE="0600"
SUBSYSTEM=="usb", ATTR{idVendor}=="1cbe", ATTR{idProduct}=="0123", OWNER="$OWNER", MODE="0600"
EOF
VIDEO_ARG=''
[[ "$VIDEO" == 0 ]] || VIDEO_ARG='--video'
for service in host hud; do
  unit="/etc/systemd/system/nexo-jetson-$service.service"
  [[ ! -f "$unit" ]] || cp -- "$unit" "$unit.backup-$(date +%Y%m%d-%H%M%S)"
  ARGS=''
  if [[ "$service" == host ]]; then
    ARGS="$VIDEO_ARG"
    [[ "$COMMA" == '-' ]] || ARGS="--comma $COMMA $VIDEO_ARG"
  fi
  cat > "$unit" <<EOF
[Unit]
Description=NEXO optional Jetson $service
After=network-online.target display-manager.service
StartLimitIntervalSec=0

[Service]
User=$OWNER
WorkingDirectory=$ROOT
Environment="PYTHONPATH=$ROOT"
Environment="DISPLAY=$DISPLAY"
Environment="XAUTHORITY=$XAUTHORITY"
Environment=PYTHONUNBUFFERED=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
ExecStart="$PY" -m openpilot.tools.jetson.$service $ARGS
Restart=always
RestartSec=2
TimeoutStopSec=5
KillMode=control-group
Nice=10
UMask=0027

[Install]
WantedBy=multi-user.target
EOF
done
if [[ "$VIDEO" == 1 ]]; then
  # Keep the current model, inference options and venv selection. Only switch
  # the existing decoder's input to the single local USB/Wi-Fi selector.
  OWNER_HOME=$(getent passwd "$OWNER" | cut -d: -f6)
  CONFIG="$OWNER_HOME/.config/nexo-yolo.env"
  install -d -o "$OWNER" -g "$(id -gn "$OWNER")" "$OWNER_HOME/.config"
  [[ ! -f "$CONFIG" ]] || cp -- "$CONFIG" "$CONFIG.backup-$(date +%Y%m%d-%H%M%S)"
  touch "$CONFIG"
  sed -i '/^[[:space:]]*\(export[[:space:]]\+\)\?NEXO_USB_VIDEO=/d' "$CONFIG"
  printf '\nNEXO_USB_VIDEO=1\n' >> "$CONFIG"
  chown "$OWNER:$(id -gn "$OWNER")" "$CONFIG"
fi
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
systemctl daemon-reload
systemctl enable --now nexo-jetson-host.service nexo-jetson-hud.service
[[ "$VIDEO" == 0 ]] || systemctl restart nexo-yolo.service
echo 'Vehicle NexoJetsonUsb remains opt-in. No vehicle setting was changed.'
