#!/usr/bin/env bash
# Run this on the Jetson while parked. It installs the Carrot Jetlink server
# against NEXOdriveXPlus's own driving_supercombo.onnx; it does not install a
# different driving model.
set -euo pipefail

NEXO_ROOT=${NEXO_ROOT:-$HOME/NEXOdriveXPlus}
CARROT_JETSON=${CARROT_JETSON:-$HOME/carrot-jetson}
CARROT_REV=${CARROT_REV:-d0dc96fa32410403dfb2fefff3e2909c11ec109b}
PY=${PYTHON_BIN:-python3}
PY=$(command -v "$PY")
MODEL="$NEXO_ROOT/openpilot/selfdrive/modeld/models/driving_supercombo.onnx"
SERVICE=/etc/systemd/system/nexo-jetlink-server.service
HUD_SERVICE=/etc/systemd/system/nexo-jetson-hud.service

[[ -r /etc/nv_tegra_release ]] || { echo 'Jetson L4T installation required.' >&2; exit 2; }
: "${DISPLAY:?Set the actual Jetson desktop DISPLAY before installation}"
: "${XAUTHORITY:?Set the actual Jetson desktop XAUTHORITY before installation}"
[[ -r "$XAUTHORITY" ]] || { echo 'Desktop XAUTHORITY is not readable.' >&2; exit 2; }
for value in "$NEXO_ROOT" "$CARROT_JETSON" "$PY" "$DISPLAY" "$XAUTHORITY" "$USER"; do
  [[ "$value" != *$'\n'* && "$value" != *'"'* && "$value" != *'%'* && "$value" != *'\\'* ]] || exit 2
done

[[ -f "$MODEL" ]] || { echo "missing NEXO model: $MODEL" >&2; exit 2; }

if [[ ! -d "$CARROT_JETSON/.git" ]]; then
  git clone https://github.com/ajouatom/carrot-jetson.git "$CARROT_JETSON"
fi
git -C "$CARROT_JETSON" fetch origin "$CARROT_REV"
git -C "$CARROT_JETSON" checkout --detach "$CARROT_REV"

export PYTHONPATH="$NEXO_ROOT:$CARROT_JETSON/third_party/jetlink${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m openpilot.tools.jetson.diagnose --role host --native-jetlink
command -v ffmpeg >/dev/null
[[ "$(ffmpeg -hide_banner -encoders 2>/dev/null)" == *libx264* ]] || { echo 'ffmpeg libx264 required.' >&2; exit 2; }

"$PY" - <<'PY'
import usb1
import numpy
import tensorrt
import jetlink
print('Jetlink Python dependencies: OK')
PY

# Import the actual composed server before touching any existing service.
"$PY" - "$CARROT_JETSON" "$NEXO_ROOT" <<'PY'
import importlib.util
import sys
from pathlib import Path
from openpilot.tools.jetson.native_host import session_class
source = Path(sys.argv[1]) / 'tools/jetlink/server.py'
spec = importlib.util.spec_from_file_location('nexo_carrot_server', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert issubclass(session_class(module.CarrotSession), module.CarrotSession)
print('Combined inference/HUD server: OK')
sys.path.insert(0, str(Path(sys.argv[2]) / 'openpilot/selfdrive/carrot/cluster'))
from openpilot.tools.jetson.hud import install_adapters
install_adapters(native=True)
import main as cluster
assert callable(cluster.main)
print('NEXO native HUD runtime: OK')
PY

# Build/cache the exact NEXO model before the service is allowed to start.
"$PY" -m jetlink.server.main --backend trt --build "$MODEL"

sudo install -d -o "$USER" -g "$(id -gn)" -m 0750 /dev/shm/nexo-jetson
printf 'd /dev/shm/nexo-jetson 0750 %s %s -\n' "$USER" "$(id -gn)" | sudo tee /etc/tmpfiles.d/nexo-jetson.conf >/dev/null
sudo tee /etc/udev/rules.d/71-nexo-jetlink-hud.rules >/dev/null <<EOF
SUBSYSTEM=="usb", ATTR{idVendor}=="1209", ATTR{idProduct}=="0001", OWNER="$USER", MODE="0600"
SUBSYSTEM=="usb", ATTR{idVendor}=="1cbe", ATTR{idProduct}=="0092", OWNER="$USER", MODE="0600"
SUBSYSTEM=="usb", ATTR{idVendor}=="1cbe", ATTR{idProduct}=="0123", OWNER="$USER", MODE="0600"
EOF
for unit in "$SERVICE" "$HUD_SERVICE"; do
  [[ ! -f "$unit" ]] || sudo cp -- "$unit" "$unit.backup-$(date +%Y%m%d-%H%M%S)"
done

sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=NEXO Jetlink TensorRT inference server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CARROT_JETSON
Environment="PYTHONPATH=$NEXO_ROOT:$CARROT_JETSON/third_party/jetlink"
Environment="CARROT_JETSON=$CARROT_JETSON"
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
ExecStart="$PY" -m openpilot.tools.jetson.native_host --backend trt --transport usb --vid 0x1209 --pid 0x0001 --control-socket "$CARROT_JETSON/control.sock"
Restart=always
RestartSec=2
KillMode=control-group
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

sudo tee "$HUD_SERVICE" >/dev/null <<EOF
[Unit]
Description=NEXO HUD on Jetson alongside native Jetlink inference
After=display-manager.service nexo-jetlink-server.service
StartLimitIntervalSec=0

[Service]
User=$USER
LogsDirectory=nexo-jetson-hud
WorkingDirectory=/var/log/nexo-jetson-hud
Environment="PYTHONPATH=$NEXO_ROOT"
Environment="CARROT_JETSON=$CARROT_JETSON"
Environment="DISPLAY=$DISPLAY"
Environment="XAUTHORITY=$XAUTHORITY"
Environment=PYTHONUNBUFFERED=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
ExecStart="$PY" -m openpilot.tools.jetson.hud --native-jetlink
Nice=19
Restart=always
RestartSec=2
TimeoutStopSec=5
KillMode=control-group
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

# One USB owner and one panel renderer. Stop only the alternative companion
# services after all preflight/build checks pass; backups above retain units.
for alternative in nexo-jetson-host.service carrot-jetlink.service carrot-jetlink-hud.service; do
  if sudo systemctl cat "$alternative" >/dev/null 2>&1; then
    sudo systemctl disable --now "$alternative"
  fi
done
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb
sudo systemctl daemon-reload
sudo systemctl enable nexo-jetlink-server.service nexo-jetson-hud.service
sudo systemctl restart nexo-jetlink-server.service nexo-jetson-hud.service

echo "Installed combined NEXO inference + Jetson-attached HUD services. Vehicle NexoJetsonUsb must remain OFF."
echo "Check: systemctl status nexo-jetlink-server.service nexo-jetson-hud.service --no-pager"
