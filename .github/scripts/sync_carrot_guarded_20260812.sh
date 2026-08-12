#!/usr/bin/env bash
set -euo pipefail

SOURCE_URL="https://github.com/fallen8angel/openpilot_Carrot.git"
SOURCE_REF="carrot-wip"
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

if git merge-base --is-ancestor "$SOURCE" HEAD; then
  echo "NO_NEW_CARROT_COMMITS=1"
  exit 0
fi

MERGE_BASE="$(git merge-base HEAD "$SOURCE")"
echo "MERGE_BASE=$MERGE_BASE"

git diff --name-only "$MERGE_BASE..$SOURCE" > /tmp/carrot_changed.txt
printf '%s\n' '--- Carrot changed paths ---'
cat /tmp/carrot_changed.txt

# NEXO-specific files whose behavior must never be silently replaced.
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
    echo "Carrot changed a NEXO-protected path. Manual review required; nothing pushed."
    exit 42
  fi
done

# Merge only when Git can do so without any content conflict. No automatic
# 'take theirs' resolution is allowed for a vehicle-control repository.
if ! git merge --no-ff --no-commit "$SOURCE"; then
  git merge --abort || true
  echo "MERGE_CONFLICT=1"
  exit 43
fi

# Confirm all NEXO-protected files stayed byte-for-byte identical to pre-merge.
for f in "${protected[@]}"; do
  if git cat-file -e "$BASE:$f" 2>/dev/null; then
    if ! git diff --quiet "$BASE" -- "$f"; then
      echo "BLOCKED_NEXO_FILE_CHANGED=$f"
      git merge --abort || true
      exit 44
    fi
  fi
done

# Preserve established NEXO policies explicitly.
grep -Fq 'params.get_int("ClusterHud") in (1, 2)' openpilot/system/manager/process_config.py
grep -Fq '# PythonProcess("cweb_push"' openpilot/system/manager/process_config.py
grep -Fq 'NEXO_DIAG_COMPLETE' openpilot/selfdrive/carrot/web/js/pages/nexo_diag.js
grep -Fq 'NEXO_DIAG_STARTED' openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py

# Python syntax checks for NEXO custom paths plus newly changed Carrot Python.
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
    *.py)
      [ -f "$f" ] && python3 -m py_compile "$f"
      ;;
  esac
done < /tmp/carrot_changed.txt

# New SPI v3 implementation is drive-critical. At minimum require the new
# standalone C++ units to pass compiler syntax checking on the CI runner.
if [ -f openpilot/selfdrive/pandad/spi_protocol_v3.cc ]; then
  g++ -std=c++17 -Iopenpilot -I. -fsyntax-only \
    openpilot/selfdrive/pandad/spi_protocol_v3.cc \
    openpilot/selfdrive/pandad/spi_v3_transport.cc \
    openpilot/selfdrive/pandad/spi_version.cc
fi

git diff --check --cached

git commit -m "Merge latest openpilot_Carrot updates into NEXO"
MERGE_SHA="$(git rev-parse HEAD)"
git push origin "$TARGET_BRANCH"
printf 'SYNC_MERGE_SHA=%s\nSYNC_SOURCE_SHA=%s\n' "$MERGE_SHA" "$SOURCE"
