#!/usr/bin/env bash
#if [[ "$(cat /data/params/d/EnableConnect)" == "2" ]]; then
#  export API_HOST="https://api.carrotpilot.app"
#  export ATHENA_HOST="wss://athena.carrotpilot.app"
#fi

# NEXO preflight: these Params must be fixed before card creates CarParams.
# The first-gen NEXO uses the Mando front radar on bus 1 and its SCC ECU on
# legacy bus 0. A stale HyundaiCameraSCC setting routes radar UDS to bus 2 and
# can leave openpilotLongitudinalControl disabled. The 8-second diagnostic used
# to set EnableRadarTracks only after CarParams had already been created, which
# was too late for the running drive session.
_nexo_selected="$(cat /data/params/d/CarSelected3 2>/dev/null || true) $(cat /data/params/d/CarName 2>/dev/null || true)"
if [[ "${_nexo_selected,,}" == *"nexo"* ]]; then
  mkdir -p /data/params/d
  printf '1' > /data/params/d/EnableRadarTracks
  printf '0' > /data/params/d/HyundaiCameraSCC
  printf '1' > /data/params/d/NexoLongPreflightApplied
  echo "[NEXO preflight] Mando radar tracks enabled; HyundaiCameraSCC cleared before CarParams"
fi
unset _nexo_selected

exec ./launch_chffrplus.sh
