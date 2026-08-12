#!/usr/bin/env bash
set -euo pipefail

SOURCE_URL="https://github.com/fallen8angel/openpilot_Carrot.git"
SOURCE_REF="carrot-wip"
EXPECTED_SOURCE="a765bed202bba1a844117fc7980c2937e743b796"
LAST_SYNC="8c4a994317a271546ffa83e1ec2af60786d92fd9"
TARGET_BRANCH="NEXO"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git fetch origin "$TARGET_BRANCH"
git checkout -B "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
BASE="$(git rev-parse HEAD)"

git remote remove carrot >/dev/null 2>&1 || true
git remote add carrot "$SOURCE_URL"
git fetch carrot "$SOURCE_REF"
SOURCE="$(git rev-parse "carrot/$SOURCE_REF")"

printf 'NEXO_BASE=%s\nCARROT_SOURCE=%s\n' "$BASE" "$SOURCE"
if [ "$SOURCE" != "$EXPECTED_SOURCE" ]; then
  echo "SOURCE_MOVED_REVIEW_REQUIRED expected=$EXPECTED_SOURCE actual=$SOURCE"
  exit 41
fi
if git merge-base --is-ancestor "$SOURCE" HEAD; then
  echo "NO_NEW_CARROT_COMMITS=1"
  exit 0
fi

MERGE_BASE="$(git merge-base HEAD "$SOURCE")"
echo "MERGE_BASE=$MERGE_BASE"
git diff --name-only "$MERGE_BASE..$SOURCE" | sort -u > /tmp/carrot_changed.txt
git diff --name-only "$LAST_SYNC..$BASE" | sort -u > /tmp/nexo_changed.txt

echo '--- Carrot new paths ---'
cat /tmp/carrot_changed.txt
echo '--- NEXO custom paths since previous Carrot sync ---'
cat /tmp/nexo_changed.txt

# Any overlap with work done on NEXO after the previous sync requires manual review.
comm -12 /tmp/carrot_changed.txt /tmp/nexo_changed.txt > /tmp/overlap.txt
if [ -s /tmp/overlap.txt ]; then
  echo 'BLOCKED_OVERLAPPING_NEXO_PATHS:'
  cat /tmp/overlap.txt
  exit 42
fi

# Explicit NEXO behavior protection, including MED, button/LIMIT, diagnostics and UI custom work.
protected=(
  "nexo_ai_cruise.py"
  "opendbc_repo/opendbc/car/hyundai/hyundaican.py"
  "opendbc_repo/opendbc/car/hyundai/interface.py"
  "opendbc_repo/opendbc/car/hyundai/carcontroller.py"
  "opendbc_repo/opendbc/car/hyundai/carstate.py"
  "opendbc_repo/opendbc/car/hyundai/values.py"
  "opendbc_repo/opendbc/car/hyundai/radar_interface.py"
  "opendbc_repo/opendbc/car/hyundai/tests/test_nexo_ai_cruise.py"
  "openpilot/system/manager/process_config.py"
  "openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py"
  "openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py"
  "openpilot/selfdrive/carrot/web/js/pages/nexo_diag.js"
  "openpilot/selfdrive/carrot/web/js/pages/nexo_can_diag.js"
  "openpilot/selfdrive/carrot/web/src/features/tools/can_diag.js"
  "openpilot/selfdrive/carrot/web/js/pages/branch.js"
  "openpilot/selfdrive/carrot/web/js/pages/setting_device_network.js"
  "sitecustomize.py"
  "tools/nexo_long_diag.py"
)
for f in "${protected[@]}"; do
  if grep -Fxq "$f" /tmp/carrot_changed.txt; then
    echo "BLOCKED_PROTECTED_PATH=$f"
    exit 43
  fi
done

# Never auto-resolve a control-repository merge conflict.
if ! git merge --no-ff --no-commit "$SOURCE"; then
  git merge --abort || true
  echo "MERGE_CONFLICT=1"
  exit 44
fi

# Every file modified in NEXO after the previous sync must remain byte-identical.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if ! git diff --quiet "$BASE" -- "$f"; then
    echo "BLOCKED_NEXO_CUSTOM_FILE_CHANGED=$f"
    git merge --abort || true
    exit 45
  fi
done < /tmp/nexo_changed.txt

# Established NEXO policies must still exist.
grep -Fq 'params.get_int("ClusterHud") in (1, 2)' openpilot/system/manager/process_config.py
grep -Fq '# PythonProcess("cweb_push"' openpilot/system/manager/process_config.py
grep -Fq 'NEXO_DIAG_COMPLETE' openpilot/selfdrive/carrot/web/js/pages/nexo_diag.js
grep -Fq 'NEXO_DIAG_STARTED' openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py

# The new source intentionally reverts unfinished SPI v3 and returns pandad to the established path.
grep -Fq "panda_env.Library('panda', ['panda.cc', 'panda_comms.cc', 'spi.cc'])" openpilot/selfdrive/pandad/SConscript
for f in \
  openpilot/selfdrive/pandad/spi_protocol_v3.cc \
  openpilot/selfdrive/pandad/spi_protocol_v3.h \
  openpilot/selfdrive/pandad/spi_v3_transport.cc \
  openpilot/selfdrive/pandad/spi_v3_transport.h \
  panda/board/drivers/spi_v3.h; do
  if [ -e "$f" ]; then
    echo "SPI_V3_REVERT_INCOMPLETE=$f"
    git merge --abort || true
    exit 46
  fi
done

# Python syntax checks: NEXO custom control paths plus every changed Carrot Python file.
python3 -m py_compile \
  nexo_ai_cruise.py \
  opendbc_repo/opendbc/car/hyundai/hyundaican.py \
  opendbc_repo/opendbc/car/hyundai/interface.py \
  opendbc_repo/opendbc/car/hyundai/carcontroller.py \
  openpilot/system/manager/process_config.py \
  openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py \
  openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py \
  sitecustomize.py \
  tools/nexo_long_diag.py
while IFS= read -r f; do
  case "$f" in
    *.py) [ -f "$f" ] && python3 -m py_compile "$f" ;;
  esac
done < /tmp/carrot_changed.txt

# Ensure the radar ghost fix and its regression test landed together.
grep -Fq 'STATIONARY_HELD_FRONT_NO_VISION_MAX_DPATH_M' openpilot/selfdrive/carrot/radar_motion/primary.py
grep -Fq 'test_weak_vision_releases_only_offset_front_stationary_hold' openpilot/selfdrive/carrot/tests/test_radar_motion_predictor.py

git diff --check --cached

git commit -m "Merge latest openpilot_Carrot updates into NEXO"
MERGE_SHA="$(git rev-parse HEAD)"
git push origin "$TARGET_BRANCH"
printf 'SYNC_MERGE_SHA=%s\nSYNC_SOURCE_SHA=%s\n' "$MERGE_SHA" "$SOURCE"
