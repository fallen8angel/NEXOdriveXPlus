#!/usr/bin/env bash
set -euo pipefail

SOURCE_URL="https://github.com/fallen8angel/openpilot_Carrot.git"
SOURCE_REF="carrot-wip"
EXPECTED_SOURCE="88ede0618225dd58e2d67f5189ba58e5e665bdaf"
CARROT_BASE="a765bed202bba1a844117fc7980c2937e743b796"
LAST_SYNC="dfa24e73fa5e3368560bc1c15d0d0921684c4ed2"
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

printf 'NEXO_BASE=%s\nCARROT_BASE=%s\nCARROT_SOURCE=%s\n' "$BASE" "$CARROT_BASE" "$SOURCE"
if [ "$SOURCE" != "$EXPECTED_SOURCE" ]; then
  echo "SOURCE_MOVED_REVIEW_REQUIRED expected=$EXPECTED_SOURCE actual=$SOURCE"
  exit 41
fi
if git merge-base --is-ancestor "$SOURCE" HEAD; then
  echo "NO_NEW_CARROT_COMMITS=1"
  exit 0
fi

# Compare actual Carrot-to-Carrot changes only. NEXO and Carrot histories diverge,
# so using the NEXO merge commit as the Carrot diff base gives false overlaps.
git diff --name-only "$CARROT_BASE..$SOURCE" | sort -u > /tmp/carrot_changed.txt
git diff --name-only "$LAST_SYNC..$BASE" | sort -u > /tmp/nexo_changed.txt

echo '--- Actual Carrot new paths ---'
cat /tmp/carrot_changed.txt
echo '--- NEXO custom paths since previous sync ---'
cat /tmp/nexo_changed.txt

comm -12 /tmp/carrot_changed.txt /tmp/nexo_changed.txt > /tmp/overlap.txt
if [ -s /tmp/overlap.txt ]; then
  echo 'BLOCKED_OVERLAPPING_NEXO_PATHS:'
  cat /tmp/overlap.txt
  exit 42
fi

# Never auto-resolve a vehicle-control repository conflict.
if ! git merge --no-ff --no-commit "$SOURCE"; then
  git merge --abort || true
  echo "MERGE_CONFLICT=1"
  exit 43
fi

# Every NEXO file changed after the last Carrot sync must stay byte-identical.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if ! git diff --quiet "$BASE" -- "$f"; then
    echo "BLOCKED_NEXO_CUSTOM_FILE_CHANGED=$f"
    git merge --abort || true
    exit 44
  fi
done < /tmp/nexo_changed.txt

# NEXO-specific policies and current custom work must remain present.
grep -Fq 'params.get_int("ClusterHud") in (1, 2)' openpilot/system/manager/process_config.py
grep -Fq '# PythonProcess("cweb_push"' openpilot/system/manager/process_config.py
grep -Fq 'NEXO_DIAG_COMPLETE' openpilot/selfdrive/carrot/web/js/pages/nexo_diag.js
grep -Fq 'NEXO_DIAG_STARTED' openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py

# Current NEXO branch intentionally contains custom cruise/longitudinal/Panda work.
test -f nexo_ai_cruise.py
test -f openpilot/selfdrive/controls/lib/longcontrol.py
test -f opendbc_repo/opendbc/safety/safety/safety_hyundai_common.h
test -f openpilot/selfdrive/pandad/spi_protocol_v3.cc
test -f tools/nexo_long_diag.py

# Python syntax checks for NEXO custom code and every new Carrot Python file.
python3 -m py_compile \
  nexo_ai_cruise.py \
  openpilot/selfdrive/controls/lib/longcontrol.py \
  tools/nexo_long_diag.py \
  openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py \
  openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py
while IFS= read -r f; do
  case "$f" in
    *.py) [ -f "$f" ] && python3 -m py_compile "$f" ;;
  esac
done < /tmp/carrot_changed.txt

# Confirm the new weak-vision radar acquisition logic and regression data landed together.
grep -Fq 'STATIONARY_WEAK_VISION_MIN_PROB' openpilot/selfdrive/carrot/radar_motion/primary.py
grep -Fq 'STATIONARY_WEAK_VISION_PAIR_CONFIRMATION_S' openpilot/selfdrive/carrot/radar_motion/primary.py
grep -Fq 'sorento-1-4-weak-vision-paired-early-lead' openpilot/selfdrive/carrot/cluster/cutin_validation_cases.json

# Offroad warning policy change must match Carrot source intent.
grep -Fq '{"Offroad_ExcessiveActuation", {CLEAR_ON_MANAGER_START, JSON}}' openpilot/common/params_keys.h

git diff --check --cached

git commit -m "Merge latest openpilot_Carrot updates into NEXO"
MERGE_SHA="$(git rev-parse HEAD)"
git push origin "$TARGET_BRANCH"
printf 'SYNC_MERGE_SHA=%s\nSYNC_SOURCE_SHA=%s\n' "$MERGE_SHA" "$SOURCE"
