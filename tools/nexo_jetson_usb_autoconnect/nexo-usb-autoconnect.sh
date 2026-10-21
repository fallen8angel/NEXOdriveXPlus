#!/usr/bin/env bash
# Restore the existing Jetson USB-NCM profile if comma4 changes its interface name.
set -euo pipefail
PROFILE="Wired connection 2"
ADDRESS="192.168.100.250/24"
TAG="nexo-usb-autoconnect"

is_comma_usb() {
  local parent
  parent="$(readlink -f "/sys/class/net/$1/device" 2>/dev/null)" || return 1
  while [[ "$parent" != "/" ]]; do
    if [[ -r "$parent/idVendor" && -r "$parent/idProduct" ]]; then
      [[ "$(<"$parent/idVendor")" == "04d8" && "$(<"$parent/idProduct")" == "1234" ]] && return 0
    fi
    parent="$(dirname "$parent")"
  done
  return 1
}

interface=""
for entry in /sys/class/net/*; do
  name="${entry##*/}"
  [[ "$name" == lo ]] && continue
  if is_comma_usb "$name"; then
    interface="$name"
    break
  fi
done

if [[ -z "$interface" ]]; then
  logger -t "$TAG" "USB gadget 04d8:1234 not present; waiting"
  exit 0
fi

if ip -4 -o addr show dev "$interface" | grep -Fq "inet $ADDRESS "; then
  exit 0
fi

if ! nmcli -g connection.id connection show "$PROFILE" >/dev/null 2>&1; then
  logger -t "$TAG" "missing NM profile: $PROFILE"
  exit 1
fi

nmcli connection modify "$PROFILE" \
  connection.interface-name "$interface" \
  ipv4.method manual \
  ipv4.addresses "$ADDRESS" \
  ipv4.never-default yes \
  ipv6.method disabled \
  connection.autoconnect yes
nmcli connection up "$PROFILE" ifname "$interface" >/dev/null
logger -t "$TAG" "activated $PROFILE on $interface with $ADDRESS"
