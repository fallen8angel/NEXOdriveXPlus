#!/usr/bin/env bash
set -euo pipefail

TARGET_BRANCH="NEXO"
SOURCE_REPO="https://github.com/fallen8angel/openpilot_Carrot.git"
SOURCE_BRANCH="carrot-wip"

 git config user.name "github-actions[bot]"
 git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

 git fetch origin "${TARGET_BRANCH}"
 git checkout -B "${TARGET_BRANCH}" "origin/${TARGET_BRANCH}"
 BASE_SHA="$(git rev-parse HEAD)"

 if git remote get-url carrot >/dev/null 2>&1; then
   git remote set-url carrot "${SOURCE_REPO}"
 else
   git remote add carrot "${SOURCE_REPO}"
 fi
 git fetch --no-tags carrot "${SOURCE_BRANCH}"
 SOURCE_SHA="$(git rev-parse FETCH_HEAD)"
 echo "Target base: ${BASE_SHA}"
 echo "Carrot source: ${SOURCE_SHA}"

 if git merge-base --is-ancestor "${SOURCE_SHA}" HEAD; then
   echo "No new Carrot commits to merge."
   exit 0
 fi

 mkdir -p /tmp/nexo-sync
 cp opendbc_repo/opendbc/car/hyundai/hyundaican.py /tmp/nexo-sync/hyundaican.py
 cp opendbc_repo/opendbc/car/hyundai/carcontroller.py /tmp/nexo-sync/carcontroller.py
 if [ -f opendbc_repo/opendbc/car/hyundai/tests/test_jerk.py ]; then
   cp opendbc_repo/opendbc/car/hyundai/tests/test_jerk.py /tmp/nexo-sync/test_jerk.py
 fi
 cp sitecustomize.py /tmp/nexo-sync/sitecustomize.py
 cp nexo_ai_cruise.py /tmp/nexo-sync/nexo_ai_cruise.py

 set +e
 git merge --no-ff --no-commit "${SOURCE_SHA}"
 MERGE_STATUS=$?
 set -e

 if [ "${MERGE_STATUS}" -ne 0 ]; then
   echo "Merge conflicts detected; resolving upstream-first, then restoring NEXO protected files."
   mapfile -t conflicts < <(git diff --name-only --diff-filter=U)
   if [ "${#conflicts[@]}" -eq 0 ]; then
     echo "Merge failed without resolvable conflict files."
     git merge --abort || true
     exit 1
   fi
   for f in "${conflicts[@]}"; do
     echo "Resolving conflict from Carrot: ${f}"
     git checkout --theirs -- "${f}"
     git add "${f}"
   done
 fi

 # Preserve NEXO classic-CAN payload and current AI/MED cruise behavior.
 cp /tmp/nexo-sync/hyundaican.py opendbc_repo/opendbc/car/hyundai/hyundaican.py
 cp /tmp/nexo-sync/carcontroller.py opendbc_repo/opendbc/car/hyundai/carcontroller.py
 if [ -f /tmp/nexo-sync/test_jerk.py ]; then
   cp /tmp/nexo-sync/test_jerk.py opendbc_repo/opendbc/car/hyundai/tests/test_jerk.py
 fi
 cp /tmp/nexo-sync/sitecustomize.py sitecustomize.py
 cp /tmp/nexo-sync/nexo_ai_cruise.py nexo_ai_cruise.py
 git add opendbc_repo/opendbc/car/hyundai/hyundaican.py \
         opendbc_repo/opendbc/car/hyundai/carcontroller.py \
         sitecustomize.py nexo_ai_cruise.py
 if [ -f opendbc_repo/opendbc/car/hyundai/tests/test_jerk.py ]; then
   git add opendbc_repo/opendbc/car/hyundai/tests/test_jerk.py
 fi

 # Re-apply NEXO manager privacy/HUD policies if upstream touched the manager.
 python3 - <<'PY'
from pathlib import Path
p = Path('openpilot/system/manager/process_config.py')
if p.exists():
  s = p.read_text()
  s = s.replace('return params.get_int("ClusterHud") == 1', 'return params.get_int("ClusterHud") in (1, 2)')
  active = '  PythonProcess("cweb_push", "openpilot.selfdrive.carrot.cweb_push", always_run, enabled=not PC),'
  commented = '  # PythonProcess("cweb_push", "openpilot.selfdrive.carrot.cweb_push", always_run, enabled=not PC),'
  if active in s:
    s = s.replace(active, '  # Disabled in NEXO tuning branch: do not report device ID/IP/port to Carrot developer server.\n' + commented)
  p.write_text(s)
PY
 git add openpilot/system/manager/process_config.py || true

 # NEXO protections and syntax validation.
 grep -q 'NEXO cluster can temporarily lock cruise input' opendbc_repo/opendbc/car/hyundai/carcontroller.py
 grep -q 'HYUNDAI_NEXO_1ST_GEN' sitecustomize.py
 grep -q 'NexoAICruiseStateManager' nexo_ai_cruise.py
 cmp -s /tmp/nexo-sync/hyundaican.py opendbc_repo/opendbc/car/hyundai/hyundaican.py
 cmp -s /tmp/nexo-sync/carcontroller.py opendbc_repo/opendbc/car/hyundai/carcontroller.py

 python3 -m py_compile sitecustomize.py nexo_ai_cruise.py openpilot/system/manager/process_config.py opendbc_repo/opendbc/car/hyundai/carcontroller.py
 python3 -m compileall -q openpilot/selfdrive/carrot/radar openpilot/selfdrive/carrot/cluster
 git diff --check

 git add -A
 if git diff --cached --quiet; then
   echo "No tree changes after merge; aborting merge state."
   git merge --abort || true
   exit 0
 fi

 git commit -m "Merge latest openpilot_Carrot updates into NEXO"
 MERGE_SHA="$(git rev-parse HEAD)"
 echo "Created merge commit: ${MERGE_SHA}"
 git push origin "${TARGET_BRANCH}"
 echo "SYNC_MERGE_SHA=${MERGE_SHA}"
 echo "SYNC_SOURCE_SHA=${SOURCE_SHA}"
