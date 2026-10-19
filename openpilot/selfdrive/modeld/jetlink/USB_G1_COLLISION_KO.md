# Jetlink g1 USB collision — Carrot comparison and safe field procedure

Observed on comma4 / AGNOS: `/sys/class/udc` contains only `a600000.dwc3`, occupied by `g1` with `ffs.adb` and `ncm.0`. Jetlink returns `USB gadget 'g1' already owns a600000.dwc3`. `NexoJetsonUsb=0` does **not** disable AGNOS `g1`.

## Carrot upstream benchmark

- `ajouatom/openpilot` `carrot-wip` `tools/jetlink/setup_gadget.sh` creates a separate `jetlink` FunctionFS gadget and **rejects any other bound owner**. It does not multiplex into AGNOS `g1`. Therefore copying Carrot setup alone **cannot** solve the collision.
- `tools/jetlink/USB-C-HOTFIX.md` describes an Orin Nano P3768/JetPack 36.4.7 **USB-C role** issue (Jetson host versus device). Do not apply it to an unspecified Jetson board or a USB-A host connection.
- Jetson host must run the protocol-compatible server and exact NEXO model contract. The USB NCM network and the Jetlink USB bulk transport are not interchangeable.

## Safe read-only probe

While parked, on comma:

```sh
cd /data/openpilot
python3 openpilot/selfdrive/modeld/jetlink/diagnose_usb_owner.py
```

An older AGNOS composite `g1` owning the only UDC is a **design-level conflict** with a distinct `jetlink` gadget; a USB cable, DHCP fix, or SSH key cannot resolve that conflict alone. `configured` shows USB enumeration, not that the Jetson model is online. A peer responding on NCM may still stop responding due to host/network issues.

## Recovery / implementation decisions (not automatically applied)

1. Retain AGNOS ADB+NCM while diagnosing; do not `echo '' > g1/UDC`, disable ADB, or delete gadget entries remotely. That can sever the only service path.
2. Confirm Jetson host identity, model, port role and stable independent Wi-Fi/LAN SSH **before** any USB mode handoff. The Carrot USB-C hotfix is conditional on the exact supported Jetson carrier/port.
3. A future implementation must pick **one** of two designs: (a) a parked/offroad, reversible, mutually exclusive gadget handoff with a known independent recovery path; or (b) a tested AGNOS-compatible composite `g1` with proper FunctionFS lifetime/host enumeration support. Do not mix both approaches, or claim composite support exists today.
4. Validate host server startup, NEXO SHA match, USB protocol handshake, latency, panel output and disconnect recovery on real hardware before enabling inference. Leave existing local model fallback and vehicle CAN/MED/SCC/radar logic untouched.

This change adds diagnostics only; it does not enable Jetlink, modify `g1`, override AGNOS, or establish successful Jetson inference.
