#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEADER="$ROOT/openpilot/common/params_keys.h"
MODULE="$ROOT/openpilot/common/params_pyx.so"
CACHE_DIR="${SCONS_CACHE_DIR:-/data/scons_cache}"

if ! mkdir -p "$CACHE_DIR" 2>/dev/null; then
  CACHE_DIR="/tmp/scons_cache"
  mkdir -p "$CACHE_DIR"
fi

# Preserve the exact multi-value radar mode across a native Params rebuild.
# Older params_pyx builds could treat EnableRadarTracks as BOOL and collapse
# 2/3 to 1 in long-running processes. Keep a validated raw value and restore
# it with the freshly rebuilt INT schema before manager starts.
#
# IMPORTANT: never initialize or normalize this key to 1 during startup.
# EnableRadarTracks is PERSISTENT INT (-2..3). Its registry/UI default is 0,
# and any existing user-selected value must survive reboot verbatim.
RADAR_TRACKS_PATH="/data/params/d/EnableRadarTracks"
SAVED_RADAR_TRACKS=""
if [ -f "$RADAR_TRACKS_PATH" ]; then
  SAVED_RADAR_TRACKS="$(tr -d '\000\r\n ' < "$RADAR_TRACKS_PATH" 2>/dev/null || true)"
  case "$SAVED_RADAR_TRACKS" in
    -2|-1|0|1|2|3) ;;
    *) SAVED_RADAR_TRACKS="" ;;
  esac
fi

runtime_params_schema_ok() {
  [ -f "$MODULE" ] || return 1
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from openpilot.common.params import ParamKeyType, Params

params = Params()
assert params.get_type("EnableRadarTracks") == ParamKeyType.INT
PY
}

# The first-generation NEXO fork initializes the validated radar-track path only
# when this parameter has never been written. An explicit user choice of 0 is a
# real SCC-radar mode and must survive restart just like -2/-1/1/2/3.
STAMP="$CACHE_DIR/carrot_params_keys.sha256"
HEADER_HASH="$(sha256sum "$HEADER" | awk '{print $1}')"
BUILT_HASH="$(cat "$STAMP" 2>/dev/null || true)"

SCHEMA_OK=0
if runtime_params_schema_ok; then
  SCHEMA_OK=1
fi

if [ "$HEADER_HASH" = "$BUILT_HASH" ] && [ -f "$MODULE" ] && [ "$SCHEMA_OK" = "1" ]; then
  # EnableRadarTracks is PERSISTENT. A normal boot must never rewrite a
  # user-selected -2..3 value.
  exit 0
fi

if [ -f "$MODULE" ] && [ "$SCHEMA_OK" != "1" ]; then
  echo "Params runtime schema mismatch; rebuilding params_pyx.so."
else
  echo "Params registry changed; rebuilding params_pyx.so."
fi
rm -f \
  "$ROOT/openpilot/common/params.o" \
  "$ROOT/openpilot/common/params.os" \
  "$ROOT/openpilot/common/libcommon.a" \
  "$ROOT/openpilot/common/common.a" \
  "$MODULE"

cd "$ROOT"
scons -u -j4 openpilot/common/params_pyx.so
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
  'from openpilot.common.params import ParamKeyType, Params; p = Params(); keys = p.all_keys(); assert b"EnableRadarTracks" in keys and b"CarrotRadarMode" in keys and b"RadarMotionMode" in keys and b"RadarDPathMode" not in keys and b"RadarLeadModelMode" not in keys; assert p.get_type("EnableRadarTracks") == ParamKeyType.INT'

# Do not apply a NEXO-specific startup default here. The registry/UI default
# is 0 and an existing user choice must survive reboot unchanged.

if [ -n "$SAVED_RADAR_TRACKS" ]; then
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$SAVED_RADAR_TRACKS" <<'PY'
import sys
from openpilot.common.params import ParamKeyType, Params

value = int(sys.argv[1])
params = Params()
assert params.get_type("EnableRadarTracks") == ParamKeyType.INT
params.put_int("EnableRadarTracks", value)
assert params.get_int("EnableRadarTracks") == value
print(f"NEXO: preserved EnableRadarTracks={value} across Params rebuild.")
PY
fi

printf '%s\n' "$HEADER_HASH" > "$STAMP.tmp"
mv -f "$STAMP.tmp" "$STAMP"
