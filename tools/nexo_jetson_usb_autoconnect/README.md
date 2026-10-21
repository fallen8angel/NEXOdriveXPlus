# NEXO Jetson USB-NCM automatic reconnection

Jetson-side utility only. Does not modify comma4 vehicle controls, Jetlink inference, or HUD.

When comma4 reboots, its USB-NCM interface name on Jetson may change.
This script detects the **04d8:1234** USB gadget by sysfs vendor/product IDs
and rebinds the **existing** NetworkManager profile `Wired connection 2` to
the new interface, assigning `192.168.100.250/24`.

The timer checks every ~20 seconds. If the interface already has the correct
IP address, it makes no changes. If the expected USB gadget or profile is
missing, it does not change unrelated interfaces.

## Installation (on Jetson; requires sudo)

```bash
sudo install -m 0755 nexo-usb-autoconnect.sh /usr/local/sbin/
sudo install -m 0644 nexo-usb-autoconnect.service /etc/systemd/system/
sudo install -m 0644 nexo-usb-autoconnect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexo-usb-autoconnect.timer
sudo systemctl start nexo-usb-autoconnect.service
```

## Verification

```bash
systemctl list-timers nexo-usb-autoconnect.timer
journalctl -t nexo-usb-autoconnect -n 20 --no-pager
ip -4 addr
ping -c 3 -W 2 192.168.100.127
```

## Rollback

```bash
sudo systemctl disable --now nexo-usb-autoconnect.timer
sudo rm /usr/local/sbin/nexo-usb-autoconnect.sh
sudo rm /etc/systemd/system/nexo-usb-autoconnect.service
sudo rm /etc/systemd/system/nexo-usb-autoconnect.timer
sudo systemctl daemon-reload
```

Note: this is not yet deployed or reboot-tested on the physical Jetson.
The USB device ID must remain 04d8:1234. NetworkManager profile
`Wired connection 2` must be the dedicated comma4 USB profile.
