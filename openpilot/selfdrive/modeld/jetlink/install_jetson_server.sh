#!/usr/bin/env bash
# Run this on the Jetson while parked. It installs the Carrot Jetlink server
# against NEXOdriveXPlus's own driving_supercombo.onnx; it does not install a
# different driving model.
set -euo pipefail

NEXO_ROOT=${NEXO_ROOT:-$HOME/NEXOdriveXPlus}
CARROT_JETSON=${CARROT_JETSON:-$HOME/carrot-jetson}
CARROT_REV=${CARROT_REV:-d0dc96fa32410403dfb2fefff3e2909c11ec109b}
PY=${PYTHON_BIN:-python3}
MODEL="$NEXO_ROOT/openpilot/selfdrive/modeld/models/driving_supercombo.onnx"
SERVICE=/etc/systemd/system/nexo-jetlink-server.service

[[ -f "$MODEL" ]] || { echo "missing NEXO model: $MODEL" >&2; exit 2; }

if [[ ! -d "$CARROT_JETSON/.git" ]]; then
  git clone https://github.com/ajouatom/carrot-jetson.git "$CARROT_JETSON"
fi
git -C "$CARROT_JETSON" fetch origin "$CARROT_REV"
git -C "$CARROT_JETSON" checkout --detach "$CARROT_REV"

export PYTHONPATH="$CARROT_JETSON/third_party/jetlink${PYTHONPATH:+:$PYTHONPATH}"

"$PY" - <<'PY'
import usb1
import numpy
import tensorrt
import jetlink
print('Jetlink Python dependencies: OK')
PY

# Build/cache the exact NEXO model before the service is allowed to start.
"$PY" -m jetlink.server.main --backend trt --build "$MODEL"

sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=NEXO Jetlink TensorRT inference server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CARROT_JETSON
Environment=PYTHONPATH=$CARROT_JETSON/third_party/jetlink
ExecStart=$(command -v "$PY") -m jetlink.server.main --backend trt --transport usb --vid 0x1209 --pid 0x0001
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nexo-jetlink-server.service
sudo systemctl restart nexo-jetlink-server.service

echo "Installed nexo-jetlink-server.service"
echo "Check: systemctl status nexo-jetlink-server.service --no-pager"
