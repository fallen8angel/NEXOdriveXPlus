#!/usr/bin/env python3
"""Read-only comma USB/Jetlink triage. Never changes UDC, networking or vehicle state."""
import argparse
import hashlib
import json
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
        "Carrot plain USB and newer composite FFS+NCM setup both refuse an occupied UDC. " +
        "Neither proves ADB/g1 coexistence; NEXO NCM-TCP instead reuses the existing g1 network."
    )
    return result


def inspect_host():
    """Jetson facts only; no address/route/service changes or peer identity guesses."""
    interfaces = []
    for net in sorted(Path('/sys/class/net').glob('*')):
        interfaces.append({'name': net.name, 'device': str((net / 'device').resolve()),
                           'driver': (net / 'device/driver').resolve().name,
                           'carrier': read(net / 'carrier'), 'operstate': read(net / 'operstate')})
    model = Path(__file__).resolve().parent.parent / 'models/driving_supercombo.onnx'
    digest = None
    try:
        sha = hashlib.sha256()
        with model.open('rb') as source:
            while chunk := source.read(1 << 20):
                sha.update(chunk)
        digest = sha.hexdigest()
    except OSError:
        pass
    return {'role': 'jetson', 'interfaces': interfaces, 'addresses': command('ip', '-j', '-4', 'addr', 'show'),
            'return_route': command('ip', '-4', 'route', 'get', '192.168.100.127', 'from', '192.168.100.250'),
            'usb_tree': command('lsusb', '-t'), 'listeners': command('ss', '-ltn'), 'model_sha256': digest,
            'services': {unit: command('systemctl', 'show', unit, '--no-pager',
                                      '-p', 'ActiveState,SubState,Result,NRestarts,ExecMainStatus') for unit in
                         ('nexo-jetlink-server.service', 'nexo-jetson-hud.service', 'nexo-yolo.service')},
            'diagnosis': ['Fan, carrier, ping and SSH do not prove TensorRT or HUD operation.',
                          'Configured addresses must match on both ends; this report does not provision persistent networking.']}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('comma', 'jetson'), default='comma')
    args = parser.parse_args()
    print(json.dumps(inspect_host() if args.role == 'jetson' else inspect(), ensure_ascii=False, indent=2))
