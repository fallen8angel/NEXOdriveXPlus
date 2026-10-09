#!/usr/bin/env bash
# Minimal NEXO adaptation of Carrot/Jetlink setup_gadget.sh.
# The comma is the USB device; Jetson is the USB host.
set -euo pipefail

GADGET=/sys/kernel/config/usb_gadget/jetlink
FFS_MOUNT=/dev/ffs-jetlink
FFS_NAME=jetlink
CONFIGFS=/sys/kernel/config
STATUS_FILE=/dev/shm/nexo-jetlink-gadget
VID=${JETLINK_VID:-0x1209}
PID=${JETLINK_PID:-0x0001}

fail() {
  echo "nexo-jetlink: $1" >&2
  echo "error: $1" > "$STATUS_FILE" 2>/dev/null || true
  exit 1
}

[[ $EUID -eq 0 ]] || fail "setup_gadget.sh must run as root"

mountpoint -q "$CONFIGFS" || mount -t configfs none "$CONFIGFS" 2>/dev/null || true
[[ -d "$CONFIGFS/usb_gadget" ]] || fail "kernel has no USB gadget support"

shopt -s nullglob
udcs=(/sys/class/udc/*)
shopt -u nullglob
[[ ${#udcs[@]} -gt 0 ]] || fail "no USB device controller"

# Never steal the controller from the existing NEXO display link or any other
# gadget. NexoJetsonUsb must be disabled before this experimental mode starts.
for other in "$CONFIGFS"/usb_gadget/*/UDC; do
  [[ -e "$other" ]] || continue
  owner=$(basename "$(dirname "$other")")
  bound=$(cat "$other" 2>/dev/null || true)
  if [[ "$owner" != "jetlink" && -n "$bound" ]]; then
    fail "USB gadget '$owner' already owns $bound"
  fi
done

mkdir -p "$GADGET"
cd "$GADGET"
echo "$VID" > idVendor
echo "$PID" > idProduct
echo 0x0100 > bcdDevice
echo 0x0320 > bcdUSB
echo 0x00 > bDeviceClass

mkdir -p strings/0x409
echo "NEXOdriveXPlus" > strings/0x409/manufacturer
echo "jetlink" > strings/0x409/product
serial=$(cat /proc/device-tree/serial-number 2>/dev/null | tr -d '\0' || true)
echo "${serial:-0001}" > strings/0x409/serialnumber

mkdir -p configs/c.1/strings/0x409
echo "NEXO Jetlink inference" > configs/c.1/strings/0x409/configuration
echo 0xC0 > configs/c.1/bmAttributes
echo 8 > configs/c.1/MaxPower

mkdir -p "functions/ffs.$FFS_NAME"
[[ -L "configs/c.1/ffs.$FFS_NAME" ]] || ln -s "$GADGET/functions/ffs.$FFS_NAME" "configs/c.1/ffs.$FFS_NAME"

mkdir -p "$FFS_MOUNT"
if ! mountpoint -q "$FFS_MOUNT"; then
  if id -u comma >/dev/null 2>&1; then
    mount -t functionfs -o "uid=$(id -u comma),gid=$(id -g comma)" "$FFS_NAME" "$FFS_MOUNT"
  else
    mount -t functionfs "$FFS_NAME" "$FFS_MOUNT"
  fi
fi
[[ -e "$FFS_MOUNT/ep0" ]] || fail "FunctionFS ep0 is missing"

if id -u comma >/dev/null 2>&1; then
  chown comma "$GADGET/UDC" 2>/dev/null || true
fi

echo ok > "$STATUS_FILE"
