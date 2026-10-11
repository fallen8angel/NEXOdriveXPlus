#!/usr/bin/env bash
# Minimal NEXO adaptation of Carrot/Jetlink setup_gadget.sh.
# The comma is the USB device; Jetson is the USB host.
#
# This script owns only the experimental `jetlink` gadget. It may also release
# the legacy NEXO display-only gadget when the caller explicitly authorizes the
# offroad mode handoff with JETLINK_TAKEOVER_NEXO_DISPLAY=1. It never unbinds an
# unrelated USB gadget.
set -euo pipefail

GADGET=/sys/kernel/config/usb_gadget/jetlink
DISPLAY_GADGET=/sys/kernel/config/usb_gadget/nexo-display
FFS_MOUNT=/dev/ffs-jetlink
FFS_NAME=jetlink
CONFIGFS=/sys/kernel/config
STATUS_FILE=/dev/shm/nexo-jetlink-gadget
VID=${JETLINK_VID:-0x1209}
PID=${JETLINK_PID:-0x0001}

status() {
  { echo "$1" > "$STATUS_FILE" && chmod 0644 "$STATUS_FILE"; } 2>/dev/null || true
}

fail() {
  echo "nexo-jetlink: $1" >&2
  status "error: $1"
  exit 1
}

[[ $EUID -eq 0 ]] || fail "setup_gadget.sh must run as root"

if [[ "${1:-}" == "--teardown" ]]; then
  if [[ -e "$GADGET/UDC" ]]; then
    echo "" > "$GADGET/UDC" 2>/dev/null || true
  fi
  umount -l "$FFS_MOUNT" 2>/dev/null || umount "$FFS_MOUNT" 2>/dev/null || true
  if [[ -d "$GADGET" ]]; then
    rm -f "$GADGET/configs/c.1/ffs.$FFS_NAME" 2>/dev/null || true
    rmdir "$GADGET/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET/functions/ffs.$FFS_NAME" 2>/dev/null || true
    rmdir "$GADGET/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET" 2>/dev/null || true
  fi
  rmdir "$FFS_MOUNT" 2>/dev/null || true
  status "disabled"
  echo "nexo-jetlink gadget torn down"
  exit 0
fi

# Avoid silent `set -e` exits that previously surfaced only as exit status 1.
trap 'fail "line $LINENO: $BASH_COMMAND failed"' ERR

if [[ -d "/lib/modules/$(uname -r)" ]]; then
  for module in configfs libcomposite usb_f_fs; do
    modprobe "$module" 2>/dev/null || true
  done
fi

mountpoint -q "$CONFIGFS" || mount -t configfs none "$CONFIGFS" 2>/dev/null || true
mountpoint -q "$CONFIGFS" || fail "no configfs at $CONFIGFS"
[[ -d "$CONFIGFS/usb_gadget" ]] || fail "kernel has no USB gadget support"

shopt -s nullglob
udcs=(/sys/class/udc/*)
shopt -u nullglob
[[ ${#udcs[@]} -gt 0 ]] || fail "no USB device controller"

# A daemon crash or abrupt modeld restart can leave our own gadget bound even
# though no process still owns the FunctionFS endpoints. Unbind only ourselves
# before rebuilding the same gadget. This is safe and makes retries idempotent.
if [[ -e "$GADGET/UDC" ]]; then
  self_bound=$(cat "$GADGET/UDC" 2>/dev/null || true)
  if [[ -n "$self_bound" ]]; then
    echo "" > "$GADGET/UDC" || fail "could not release stale jetlink UDC $self_bound"
    sleep 0.2
  fi
fi

# The display-only NEXO USB mode and inference Jetlink are intentionally
# mutually exclusive. The daemon sets this flag only after it has verified that
# NexoJetsonUsb is OFF. This handles a stale display gadget left by a prior
# process without touching any unrelated gadget.
if [[ "${JETLINK_TAKEOVER_NEXO_DISPLAY:-0}" == "1" && -e "$DISPLAY_GADGET/UDC" ]]; then
  display_bound=$(cat "$DISPLAY_GADGET/UDC" 2>/dev/null || true)
  if [[ -n "$display_bound" ]]; then
    echo "" > "$DISPLAY_GADGET/UDC" || fail "could not release stale NEXO display UDC $display_bound"
    sleep 0.2
  fi
fi

# Never steal a controller from AGNOS, adb, networking, or another unknown
# gadget. If one remains bound, report its exact owner so diagnostics can show
# the real conflict.
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

status "ok"
echo "nexo-jetlink gadget ready"
echo "available UDCs: $(ls /sys/class/udc | tr '\n' ' ')"
