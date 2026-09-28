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

apply_nexo_runtime_defaults() {
  [ -f "$MODULE" ] || return 0

  if ! PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from openpilot.common.params import Params

params = Params()
car_name = params.get("CarName")
car_selected = params.get("CarSelected3")

if isinstance(car_name, bytes):
  car_name = car_name.decode("utf-8", errors="ignore")
if isinstance(car_selected, bytes):
  car_selected = car_selected.decode("utf-8", errors="ignore")

is_nexo = car_name == "HYUNDAI_NEXO_1ST_GEN" or car_selected == "Hyundai Nexo 2021"
radar_tracks = params.get("EnableRadarTracks", return_default=False)
if is_nexo and radar_tracks is None:
  params.put_int("EnableRadarTracks", 1)
  print("NEXO: initialized missing EnableRadarTracks=1 for the validated radar longitudinal path.")
PY
  then
    echo "NEXO runtime defaults deferred until Params is ready."
  fi
}

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
  apply_nexo_runtime_defaults
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

# A rebuild may have been required before Params could be imported, so apply
# the same NEXO-only default once more with the freshly built module.
apply_nexo_runtime_defaults

printf '%s\n' "$HEADER_HASH" > "$STAMP.tmp"
mv -f "$STAMP.tmp" "$STAMP"
