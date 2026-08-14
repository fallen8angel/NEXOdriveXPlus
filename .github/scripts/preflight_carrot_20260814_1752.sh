#!/usr/bin/env bash
set -euo pipefail
SOURCE_URL="https://github.com/fallen8angel/openpilot_Carrot.git"
SOURCE_REF="carrot-wip"
CARROT_BASE="2158fda8055c42f9c673580fd3f5a7a125ea5139"
EXPECTED_SOURCE="aaa6fe77e21ee2ff4e9202f4d459f45326f4ea1c"
EXPECTED_NEXO_BASE="9745e9f48baddfb0d2741606cebe67131cec12b8"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git remote remove carrot >/dev/null 2>&1 || true
git remote add carrot "$SOURCE_URL"
git fetch carrot "$SOURCE_REF"
SOURCE="$(git rev-parse carrot/$SOURCE_REF)"
BASE="$(git rev-parse HEAD)"
printf 'NEXO_BASE=%s\nCARROT_BASE=%s\nCARROT_SOURCE=%s\n' "$BASE" "$CARROT_BASE" "$SOURCE"
[ "$SOURCE" = "$EXPECTED_SOURCE" ] || { echo SOURCE_MOVED_REVIEW_REQUIRED; exit 41; }
# Trigger/workflow commits are expected on top of the reviewed NEXO base.
git merge-base --is-ancestor "$EXPECTED_NEXO_BASE" "$BASE" || { echo NEXO_BASE_MOVED_REVIEW_REQUIRED; exit 42; }

git diff --name-only "$CARROT_BASE..$SOURCE" | sort -u > /tmp/carrot.txt
git diff --name-only "$CARROT_BASE..$BASE" | sort -u > /tmp/nexo.txt
comm -12 /tmp/carrot.txt /tmp/nexo.txt > /tmp/overlap.txt

echo '--- CARROT_CHANGED ---'; cat /tmp/carrot.txt
echo '--- NEXO_CUSTOM_OVERLAP ---'; cat /tmp/overlap.txt || true

echo '--- HIGH_RISK_CARROT_PATHS ---'
grep -E '(^|/)(pandad|panda_comms|spi\.cc|safety_hyundai|carstate\.py|car/card\.py)$|openpilot/selfdrive/pandad|opendbc_repo/opendbc/safety' /tmp/carrot.txt || true

set +e
git merge --no-ff --no-commit "$SOURCE"
MERGE_RC=$?
set -e
if [ "$MERGE_RC" -ne 0 ]; then
  echo 'MERGE_CONFLICT=1'
  git diff --name-only --diff-filter=U || true
  git merge --abort || true
  exit 0
fi

echo 'MERGE_CONFLICT=0'
echo '--- MERGED_CHANGED_PATHS ---'
git diff --cached --name-status "$BASE" || true

echo '--- VERIFY_NEXO_CANONICAL_NAME ---'
grep -n 'CAR.HYUNDAI_NEXO_1ST_GEN' opendbc_repo/opendbc/car/hyundai/carstate.py || true

echo '--- VERIFY_NEW_SPEED_CAMERA_LOGIC ---'
grep -n 'VehicleSpeedCameraDistanceTime' opendbc_repo/opendbc/car/hyundai/carstate.py openpilot/common/params_keys.h || true

echo '--- CURRENT_NEXO_PANDAD_DIFF_FROM_CARROT_BASE ---'
git diff --stat "$CARROT_BASE..$BASE" -- openpilot/selfdrive/pandad/pandad.cc openpilot/selfdrive/pandad/spi.cc openpilot/selfdrive/pandad/panda_comms.h || true

echo '--- MERGED_PANDAD_DIFF_FROM_NEXO_BASE ---'
git diff --stat "$BASE" -- openpilot/selfdrive/pandad/pandad.cc openpilot/selfdrive/pandad/spi.cc openpilot/selfdrive/pandad/panda_comms.h || true

git merge --abort || true
