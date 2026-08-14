#!/usr/bin/env bash
set -euo pipefail

SOURCE_URL="https://github.com/fallen8angel/openpilot_Carrot.git"
SOURCE_REF="carrot-wip"
CARROT_BASE="88ede0618225dd58e2d67f5189ba58e5e665bdaf"
EXPECTED_SOURCE="2158fda8055c42f9c673580fd3f5a7a125ea5139"
TARGET_BRANCH="NEXO"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git fetch origin "$TARGET_BRANCH"
git checkout -B "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
BASE="$(git rev-parse HEAD)"
BASE_TREE="$(git rev-parse HEAD^{tree})"

git remote remove carrot >/dev/null 2>&1 || true
git remote add carrot "$SOURCE_URL"
git fetch carrot "$SOURCE_REF"
SOURCE="$(git rev-parse "carrot/$SOURCE_REF")"

printf 'NEXO_BASE=%s\nCARROT_BASE=%s\nCARROT_SOURCE=%s\n' "$BASE" "$CARROT_BASE" "$SOURCE"

if [ "$SOURCE" != "$EXPECTED_SOURCE" ]; then
  echo "SOURCE_MOVED_REVIEW_REQUIRED expected=$EXPECTED_SOURCE actual=$SOURCE"
  exit 41
fi

# This update must be history-only. Any source content change means re-review is required.
if ! git diff --quiet "$CARROT_BASE..$SOURCE" -- .; then
  echo "BLOCKED_SOURCE_HAS_CONTENT_CHANGES"
  git diff --name-status "$CARROT_BASE..$SOURCE"
  exit 42
fi

if git merge-base --is-ancestor "$SOURCE" HEAD; then
  echo "NO_NEW_CARROT_HISTORY=1"
  exit 0
fi

if ! git merge --no-ff --no-commit "$SOURCE"; then
  git merge --abort || true
  echo "MERGE_CONFLICT=1"
  exit 43
fi

# NEXO content must be byte-for-byte identical after this history-only merge.
AFTER_TREE="$(git write-tree)"
if [ "$AFTER_TREE" != "$BASE_TREE" ]; then
  echo "BLOCKED_NEXO_TREE_CHANGED base=$BASE_TREE after=$AFTER_TREE"
  git diff --cached --name-status "$BASE"
  git merge --abort || true
  exit 44
fi

# Re-check critical NEXO policies and syntax before recording the merge history.
grep -Fq 'params.get_int("ClusterHud") in (1, 2)' openpilot/system/manager/process_config.py
grep -Fq '# PythonProcess("cweb_push"' openpilot/system/manager/process_config.py
grep -Fq 'NEXO_DIAG_COMPLETE' openpilot/selfdrive/carrot/web/js/pages/nexo_diag.js
python3 -m py_compile \
  nexo_ai_cruise.py \
  openpilot/selfdrive/controls/lib/longcontrol.py \
  tools/nexo_long_diag.py \
  opendbc_repo/opendbc/car/hyundai/hyundaican.py \
  opendbc_repo/opendbc/car/hyundai/carcontroller.py \
  openpilot/system/manager/process_config.py

git diff --check --cached

git commit -m "Record latest openpilot_Carrot history in NEXO"
MERGE_SHA="$(git rev-parse HEAD)"
git push origin "$TARGET_BRANCH"
printf 'SYNC_MERGE_SHA=%s\nSYNC_SOURCE_SHA=%s\nSYNC_TREE=%s\n' "$MERGE_SHA" "$SOURCE" "$BASE_TREE"
