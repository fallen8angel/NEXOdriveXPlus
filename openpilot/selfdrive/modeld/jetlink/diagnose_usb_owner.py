#!/usr/bin/env python3
"""Read-only comma USB/Jetlink triage. Never changes UDC, networking or vehicle state."""
import json
import os
from pathlib import Path
import subprocess

GADGET_ROOT = Path("/sys/kernel/config/usb_gadget")
STATUS_FILES = [
    "/dev/shm/nexo-jetlink.json",
    "/dev/shm/nexo-jetlink-model.json",
    "/dev/shm/nexo-jetlink-gadget",
]

def read(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return None

def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=4, check=False)
        return {"exit": result.returncode, "output": result.stdout.strip(), "error": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}

def inspect():
    gadgets = []
    for path in sorted(GADGET_ROOT.glob("*/UDC")):
        root = path.parent
        gadgets.append({
            "name": root.name,
            "udc": read(path),
            "functions": sorted(p.name for p in (root / "functions").glob("*")) if (root / "functions").exists() else [],
        })
    bound = [g for g in gadgets if g["udc"]]
    status = {p: read(p) for p in STATUS_FILES}
    result = {
        "gadget_owners": gadgets,
        "udcs": sorted(p.name for p in Path("/sys/class/udc").glob("*")),
        "usb_state": {p.name: read(p / "state") for p in Path("/sys/class/udc").glob("*")},
        "interfaces": command("ip", "-br", "addr"),
        "usb_neighbors_v6": command("ip", "-6", "neigh", "show", "dev", "usb0"),
        "usb_neighbors_v4": command("ip", "-4", "neigh", "show", "dev", "usb0"),
        "jetlink_enabled": Path("/data/nexo_jetlink_enabled").exists(),
        "selected_transport": read("/data/nexo_jetlink_transport") or "usb",
        "ncm_route": command("ip", "-4", "route", "get", "192.168.100.250", "from", "192.168.100.127"),
        "display_mode": read("/data/params/d/NexoJetsonUsb"),
        "status": status,
        "diagnosis": [],
    }
    if len(result["udcs"]) == 1 and any(g["name"] == "g1" and g["udc"] for g in bound):
        result["diagnosis"].append(
            "AGNOS g1 owns the only USB device controller. Separate jetlink FunctionFS gadget cannot bind concurrently; never unbind g1 remotely."
        )
    if any("ncm." in f for g in gadgets for f in g["functions"]):
        result["diagnosis"].append(
            "USB NCM is configured. Its IP/SSH connectivity is separate from Jetlink inference readiness."
        )
    if result["selected_transport"] == "ncm-tcp":
        route = result["ncm_route"]
        if route.get("exit") != 0 or "dev usb0" not in route.get("output", ""):
            result["diagnosis"].append("NCM-TCP selected but peer route is NOT usb0; no inference connection should be attempted.")
        else:
            result["diagnosis"].append("NCM-TCP route uses usb0; this alone does not prove the Jetson server or HUD is ready.")
    if result["jetlink_enabled"] and status.get("/dev/shm/nexo-jetlink.json"):
        try:
            s = json.loads(status["/dev/shm/nexo-jetlink.json"])
            if s.get("state") == "retrying":
                result["diagnosis"].append("Jetlink is retrying: check the reported error; this is not proof of Jetson boot failure.")
        except (ValueError, TypeError):
            pass
    result["diagnosis"].append(
        "Carrot baseline also uses a separate jetlink gadget and refuses an occupied UDC; switching to it alone does not fix g1. "
        "Evaluate a mutually exclusive parked/offroad handoff only with independently verified recovery access."
    )
    return result

if __name__ == "__main__":
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))
