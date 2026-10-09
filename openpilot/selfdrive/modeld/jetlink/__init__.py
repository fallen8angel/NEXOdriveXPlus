"""NEXO experimental Jetlink model-offload adapter.

This package is derived from the Carrot Jetlink integration pinned at
ajouatom/openpilot@eb5bbf2720286f17fc55d6eab8a2f7aacecf1f3d.

It is intentionally opt-in. The normal NEXO model remains the default and the
existing NEXO display/YOLO USB integration is kept separate.
"""

from pathlib import Path

ENABLE_FILE = Path('/data/nexo_jetlink_enabled')
STATUS = Path('/dev/shm/nexo-jetlink.json')
MODEL_STATUS = Path('/dev/shm/nexo-jetlink-model.json')
SPEC_FILE = Path('/dev/shm/nexo-jetlink-spec.json')
FAULT = Path('/dev/shm/nexo-jetlink-fault')
SOCKET = '/dev/shm/nexo-jetlink.sock'
GADGET = '/sys/kernel/config/usb_gadget/jetlink'
FFS_MOUNT = '/dev/ffs-jetlink'


def enabled() -> bool:
  return ENABLE_FILE.exists()
