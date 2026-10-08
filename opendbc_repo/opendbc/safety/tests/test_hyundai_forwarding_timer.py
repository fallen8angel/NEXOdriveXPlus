"""Native production-hook tests: CC='zig cc' or a GCC/Clang compiler is required."""
import ctypes
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[4]
HYUNDAI = 8
HYUNDAI_LEGACY = 23
HYUNDAI_CANFD = 28
FCEV_LONG = 256 | 4
CAMERA_SCC = 8
UINT32_MASK = 0xFFFFFFFF
CLASSIC_CASES = [
  (0x340, 0, 8, 2, (0x340,), 200_000),
  (0x485, 0, 8, 2, (0x485,), 200_000),
  (0x421, 0, 8, 2, (0x420, 0x421, 0x389), 400_000),
  (0x50A, 0, 8, 2, (0x50A,), 800_000),
  (0x38D, 0, 8, 2, (0x38D,), 400_000),
  (0x483, 0, 8, 2, (0x483,), 400_000),
  (0x251, 2, 8, 0, (0x251,), 200_000),
]
CANFD_CASES = [
  (0x12A, 0, 16, 2, (0x12A,), 30_000),
  (0x160, 0, 16, 2, (0x160,), 40_000),
  (0x161, 0, 32, 2, (0x161,), 70_000),
  (0x4A3, 2, 8, 0, (0x4A3,), 220_000),
  (0xEA, 2, 24, 0, (0xEA,), 30_000),
  (0x1DA, 0, 32, 2, (0x1DA,), 1_020_000),
]
# HDA2 longitudinal enables the 1 Hz message; camera-SCC classic enables MDPS TX.
CONFIGS = [(HYUNDAI, FCEV_LONG | CAMERA_SCC, CLASSIC_CASES),
           (HYUNDAI_LEGACY, FCEV_LONG | CAMERA_SCC, CLASSIC_CASES),
           (HYUNDAI_CANFD, 16 | 4, CANFD_CASES)]
CASES = [(mode, param, case) for mode, param, cases in CONFIGS for case in cases]


@pytest.fixture(scope="module")
def safety(tmp_path_factory):
  build_dir = tmp_path_factory.mktemp("hyundai_forwarding")
  library = build_dir / ("safety.dll" if sys.platform == "win32" else "safety.so")
  # An optional include overlay allows the exact same tests to reproduce baseline failures.
  source_root = Path(os.environ.get("SAFETY_SOURCE_ROOT", ROOT))
  compiler = shlex.split(os.environ.get("CC", "cc"), posix=sys.platform != "win32")
  compiler = [part.strip('"') for part in compiler]
  build = subprocess.run(compiler + [
    "-shared", "-fPIC", "-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror",
    "-Wno-unused-variable", "-Wno-sign-compare",
    "-I", str(ROOT / "panda/board"),
    "-I", str(source_root / "opendbc_repo/opendbc/safety"),
    str(Path(__file__).with_suffix(".c").with_name("hyundai_forwarding_timer.c")),
    "-o", str(library),
  ], check=False, capture_output=True, text=True)
  assert build.returncode == 0, build.stdout + build.stderr
  lib = ctypes.CDLL(str(library))
  lib.timer_test_init.argtypes = [ctypes.c_uint16, ctypes.c_uint16]
  lib.timer_test_clock.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
  for method in (lib.timer_test_tx, lib.timer_test_fwd):
    method.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint8)]
    method.restype = ctypes.c_int
  lib.timer_test_relay.argtypes = [ctypes.c_bool]
  lib.timer_test_controls.argtypes = [ctypes.c_bool, ctypes.c_bool]
  lib.timer_test_button.argtypes = [ctypes.c_int, ctypes.c_bool]
  lib.timer_test_controls_allowed.restype = ctypes.c_bool
  return lib


def packet(addr, length, accel=0):
  data = bytearray(length)
  if addr == 0x340:
    data[2] = 0
    data[3] = 4  # neutral steering torque = 1024
  elif addr == 0x421:
    value = accel + 1023
    data[3] = value & 0xFF
    data[4] = ((value >> 8) & 7) | ((value & 7) << 5)
    data[5] = value >> 3
  return (ctypes.c_uint8 * length).from_buffer_copy(data)


def initialize(safety, mode, param, micros=1_000_000, seconds=10):
  assert safety.timer_test_init(mode, param) == 0
  safety.timer_test_clock(micros, seconds)
  safety.timer_test_controls(True, False)


def forward(safety, addr, bus, length=8):
  data = (ctypes.c_uint8 * length)(*range(length))
  before = bytes(data)
  result = safety.timer_test_fwd(addr, bus, length, data)
  assert bytes(data) == before  # timer fix must not alter CAN content
  return result


@pytest.mark.parametrize("mode,param,case", CASES)
@pytest.mark.parametrize("micros,seconds", [(0, 0), (50_000, 4295), (1_000_000, 10)])
def test_never_transmitted_stock_is_forwarded(safety, mode, param, case, micros, seconds):
  addr, _, length, rx_bus, stock_addrs, _ = case
  initialize(safety, mode, param, micros, seconds)
  for stock_addr in stock_addrs:
    assert forward(safety, stock_addr, rx_bus, length) == 2 - rx_bus


@pytest.mark.parametrize("mode,param,case", CASES)
@pytest.mark.parametrize("start", [0, 1_000_000, UINT32_MASK - 10_000])
def test_existing_deadlines_and_short_rollover(safety, mode, param, case, start):
  addr, tx_bus, length, rx_bus, stock_addrs, timeout = case
  initialize(safety, mode, param, start, 10)
  assert safety.timer_test_tx(addr, tx_bus, length, packet(addr, length)) == 1
  for elapsed in (0, timeout - 1, timeout):
    safety.timer_test_clock((start + elapsed) & UINT32_MASK, 10 + elapsed // 1_000_000)
    for stock_addr in stock_addrs:
      assert forward(safety, stock_addr, rx_bus, length) == (-1 if elapsed < timeout else 2 - rx_bus)


@pytest.mark.parametrize("mode,param,case", CASES)
@pytest.mark.parametrize("observe_expiry", [False, True])
def test_full_microsecond_lap_does_not_resurrect_old_tx(safety, mode, param, case, observe_expiry):
  addr, tx_bus, length, rx_bus, stock_addrs, timeout = case
  initialize(safety, mode, param)
  assert safety.timer_test_tx(addr, tx_bus, length, packet(addr, length)) == 1
  if observe_expiry:
    safety.timer_test_clock(1_000_000 + timeout, 12)
    assert forward(safety, stock_addrs[0], rx_bus, length) == 2 - rx_bus
  safety.timer_test_clock(1_000_001, 4305)
  for stock_addr in stock_addrs:
    assert forward(safety, stock_addr, rx_bus, length) == 2 - rx_bus
  # A fresh, accepted TX with the same address must start blocking again.
  assert safety.timer_test_tx(addr, tx_bus, length, packet(addr, length)) == 1
  assert forward(safety, stock_addrs[0], rx_bus, length) == -1


@pytest.mark.parametrize("mode,param,case", CASES)
@pytest.mark.parametrize("rejection", ["relay", "bus", "length"])
def test_rejected_tx_cannot_suppress_stock(safety, mode, param, case, rejection):
  addr, tx_bus, length, rx_bus, stock_addrs, _ = case
  initialize(safety, mode, param)
  bus = 3 if rejection == "bus" else tx_bus
  size = 12 if length == 8 else 8
  size = size if rejection == "length" else length
  safety.timer_test_relay(rejection == "relay")
  assert safety.timer_test_tx(addr, bus, size, packet(addr, size)) == 0
  safety.timer_test_relay(False)
  for stock_addr in stock_addrs:
    assert forward(safety, stock_addr, rx_bus, length) == 2 - rx_bus


@pytest.mark.parametrize("mode,param,case", CASES)
def test_safety_mode_reinit_discards_previous_tx(safety, mode, param, case):
  addr, tx_bus, length, rx_bus, stock_addrs, _ = case
  initialize(safety, mode, param)
  assert safety.timer_test_tx(addr, tx_bus, length, packet(addr, length)) == 1
  assert safety.timer_test_init(19, 0) == 0
  initialize(safety, mode, param)
  for stock_addr in stock_addrs:
    assert forward(safety, stock_addr, rx_bus, length) == 2 - rx_bus


@pytest.mark.parametrize("mode,param,cases", CONFIGS)
def test_unrelated_messages_and_bus_one_remain_unchanged(safety, mode, param, cases):
  initialize(safety, mode, param)
  for addr, tx_bus, length, _, _, _ in cases:
    assert safety.timer_test_tx(addr, tx_bus, length, packet(addr, length)) == 1
  assert forward(safety, 0x123, 0) == 2
  assert forward(safety, 0x123, 2) == 0
  assert forward(safety, 0x123, 1) == -1


def test_one_hz_deadline_survives_two_tick_boundaries_and_coarse_wrap(safety):
  addr = 0x1DA
  for seconds in (50, UINT32_MASK):
    initialize(safety, HYUNDAI_CANFD, 16 | 4, 999_000, seconds)
    assert safety.timer_test_tx(addr, 0, 32, packet(addr, 32)) == 1
    safety.timer_test_clock(2_018_999, (seconds + 2) & UINT32_MASK)
    assert forward(safety, addr, 2, 32) == -1
    safety.timer_test_clock(2_019_000, (seconds + 2) & UINT32_MASK)
    assert forward(safety, addr, 2, 32) == 0


@pytest.mark.parametrize("mode", [HYUNDAI, HYUNDAI_LEGACY])
def test_nexo_med_scc_authorization_unchanged(safety, mode):
  initialize(safety, mode, FCEV_LONG)
  safety.timer_test_controls(False, False)
  safety.timer_test_button(0, True)  # MODE -> MED_WAIT
  safety.timer_test_button(0, False)
  assert safety.timer_test_controls_allowed()
  assert safety.timer_test_tx(0x340, 0, 8, packet(0x340, 8)) == 1
  for accel in (-100, 100):
    assert safety.timer_test_tx(0x421, 0, 8, packet(0x421, 8, accel)) == 0
    assert forward(safety, 0x421, 2) == 0
  assert safety.timer_test_tx(0x421, 0, 8, packet(0x421, 8)) == 1
  safety.timer_test_button(2, False)
  safety.timer_test_button(0, False)  # SET release -> SPEED_CONTROL
  assert safety.timer_test_tx(0x421, 0, 8, packet(0x421, 8, 100)) == 1
  safety.timer_test_controls(True, True)  # brake keeps lateral only
  assert safety.timer_test_tx(0x421, 0, 8, packet(0x421, 8, 100)) == 0
  assert safety.timer_test_tx(0x340, 0, 8, packet(0x340, 8)) == 1
  safety.timer_test_button(0, False)  # return to MED_WAIT
  safety.timer_test_button(4, False)  # second-stage CANCEL -> OFF
  assert not safety.timer_test_controls_allowed()


def test_canfd_per_bus_tracking_and_permanent_block_unchanged(safety):
  initialize(safety, HYUNDAI_CANFD, 16 | 4)
  assert safety.timer_test_tx(0x12A, 1, 16, packet(0x12A, 16)) == 1
  assert forward(safety, 0x12A, 2, 16) == 0  # bus 1 TX never owned bus 0 forwarding
  assert forward(safety, 0x4B9, 0) == -1  # existing unconditional rule
