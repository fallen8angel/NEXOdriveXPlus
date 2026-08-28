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
if is_nexo and params.get_int("EnableRadarTracks") == 0:
  params.put_int("EnableRadarTracks", 1)
  print("NEXO: restored EnableRadarTracks=1 for the validated radar longitudinal path.")
PY
  then
    echo "NEXO runtime defaults deferred until Params is ready."
  fi
}

# The first-generation NEXO fork is intended to use the validated radar-track
# longitudinal path. A missing/reset parameter previously made CarParams fall
# back to openpilotLong=False even though the NEXO code was present. Restore
# only the unset value (0); explicit nonzero modes such as -2 remain untouched.
apply_nexo_runtime_defaults

STAMP="$CACHE_DIR/carrot_params_keys.sha256"
HEADER_HASH="$(sha256sum "$HEADER" | awk '{print $1}')"
BUILT_HASH="$(cat "$STAMP" 2>/dev/null || true)"

if [ "$HEADER_HASH" = "$BUILT_HASH" ] && [ -f "$MODULE" ]; then
  exit 0
fi

echo "Params registry changed; rebuilding params_pyx.so."
rm -f \
  "$ROOT/openpilot/common/params.o" \
  "$ROOT/openpilot/common/params.os" \
  "$ROOT/openpilot/common/libcommon.a" \
  "$ROOT/openpilot/common/common.a" \
  "$MODULE"

cd "$ROOT"
scons -u -j4 openpilot/common/params_pyx.so
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
  'from openpilot.common.params import Params; keys = Params().all_keys(); assert b"EnableRadarTracks" in keys and b"CarrotRadarMode" in keys and b"RadarMotionMode" in keys and b"RadarDPathMode" not in keys and b"RadarLeadModelMode" not in keys'

# A rebuild may have been required before Params could be imported, so apply
# the same NEXO-only default once more with the freshly built module.
apply_nexo_runtime_defaults

printf '%s\n' "$HEADER_HASH" > "$STAMP.tmp"
mv -f "$STAMP.tmp" "$STAMP"
