#!/usr/bin/env bash
#if [[ "$(cat /data/params/d/EnableConnect)" == "2" ]]; then
#  export API_HOST="https://api.carrotpilot.app"
#  export ATHENA_HOST="wss://athena.carrotpilot.app"
#fi

# NEXO preflight: vehicle-specific Params must be fixed before card creates CarParams.
# The first-gen NEXO uses the Mando front radar on bus 1 and its SCC ECU on
# legacy bus 0. A stale HyundaiCameraSCC setting routes radar UDS to bus 2 and
# can leave openpilotLongitudinalControl disabled.
#
# EnableRadarTracks is a persistent, user-selectable setting (-2..3). Do not
# overwrite it here; otherwise every reboot forces the user's selection back
# to mode 1 before CarParams is created.
_nexo_selected="$(cat /data/params/d/CarSelected3 2>/dev/null || true) $(cat /data/params/d/CarName 2>/dev/null || true)"
if [[ "${_nexo_selected,,}" == *"nexo"* ]]; then
  mkdir -p /data/params/d
  printf '0' > /data/params/d/HyundaiCameraSCC
  printf '1' > /data/params/d/NexoLongPreflightApplied
  echo "[NEXO preflight] HyundaiCameraSCC cleared; preserving user EnableRadarTracks setting"
fi
unset _nexo_selected

exec ./launch_chffrplus.sh
