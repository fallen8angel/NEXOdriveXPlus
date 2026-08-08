#!/usr/bin/env bash
set -Eeuo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

BASE_SHA="$(git rev-parse HEAD)"
CARROT_SHA="fc42411dda7e1bf3a1b76c6828bd624a371291cb"

echo "NEXO base: $BASE_SHA"
echo "Carrot target: $CARROT_SHA"

git remote remove upstream 2>/dev/null || true
git remote add upstream https://github.com/fallen8angel/openpilot_Carrot.git
git fetch --no-tags upstream carrot-wip
git cat-file -e "${CARROT_SHA}^{commit}"

if git merge-base --is-ancestor "$CARROT_SHA" HEAD; then
  echo "Carrot target is already contained in NEXO."
  exit 0
fi

set +e
git merge --no-ff --no-commit "$CARROT_SHA"
MERGE_RC=$?
set -e

if [[ "$MERGE_RC" -ne 0 ]]; then
  echo "Merge conflicts detected. Taking Carrot versions first, then restoring NEXO-specific policies."
  while IFS= read -r -d '' path; do
    echo "Resolving conflict from Carrot: $path"
    if git checkout --theirs -- "$path"; then
      git add -- "$path"
    else
      git rm -f -- "$path"
    fi
  done < <(git diff --name-only --diff-filter=U -z)
fi

# Preserve NEXO-only files exactly from the target branch before the merge.
git checkout "$BASE_SHA" -- nexo_tuning_test.patch
git checkout "$BASE_SHA" -- opendbc_repo/opendbc/car/hyundai/hyundaican.py

# Do not allow the upstream repository to replace NEXO's workflow files.
git rm -rf --ignore-unmatch .github/workflows
git checkout "$BASE_SHA" -- .github/workflows

python - <<'PY'
from pathlib import Path

# Start from the newest Carrot process layout, but preserve NEXO policies.
p = Path("openpilot/system/manager/process_config.py")
t = p.read_text(encoding="utf-8")
old_hud = '    return params.get_int("ClusterHud") == 1'
new_hud = '    return params.get_int("ClusterHud") in (1, 2)'
if old_hud in t:
  t = t.replace(old_hud, new_hud, 1)
elif new_hud not in t:
  raise SystemExit("ClusterHud NEXO policy location was not found")

active_push = '  PythonProcess("cweb_push", "openpilot.selfdrive.carrot.cweb_push", always_run, enabled=not PC),'
disabled_push = '  # PythonProcess("cweb_push", "openpilot.selfdrive.carrot.cweb_push", always_run, enabled=not PC),'
if active_push in t:
  t = t.replace(active_push,
    '  # Disabled in NEXO branches: do not report device ID/IP/port to the Carrot developer server.\n'
    + disabled_push, 1)
elif disabled_push not in t:
  raise SystemExit("cweb_push NEXO policy location was not found")
p.write_text(t, encoding="utf-8")

# Preserve the NEXO stock-long cruise-button lockout prevention on top of
# Carrot's newest Hyundai controller changes.
p = Path("opendbc_repo/opendbc/car/hyundai/carcontroller.py")
t = p.read_text(encoding="utf-8")
marker = "    # The NEXO cluster can temporarily lock cruise input after rapid repeated"
if marker not in t:
  anchor = "    send_button = 0\n    activate_cruise = False\n"
  snippet = """

    # The NEXO cluster can temporarily lock cruise input after rapid repeated
    # CLU11 RES/SET messages. In stock-long fallback, never chase the set speed
    # with synthetic buttons. Only permit conservative standstill resume bursts.
    if self.CP.carFingerprint == CAR.HYUNDAI_NEXO_1ST_GEN:
      resume_interval = int(0.15 / DT_CTRL)
      resume_cooldown = int(1.0 / DT_CTRL)

      if CS.out.brakePressed or CS.out.gasPressed:
        self.activateCruise = 0

      if not CC.cruiseControl.resume:
        self.button_spamming_count = 0
        self.button_wait = resume_interval
        self.prev_clu_speed = current
        return 0

      if (self.frame - self.last_button_frame) < self.button_wait:
        return 0

      self.last_button_frame = self.frame
      self.button_spamming_count += 1
      self.prev_clu_speed = current
      if self.button_spamming_count >= 3:
        self.button_spamming_count = 0
        self.button_wait = resume_cooldown
      else:
        self.button_wait = resume_interval
      return Buttons.RES_ACCEL
"""
  if anchor not in t:
    raise SystemExit("NEXO cruise-button insertion point was not found")
  t = t.replace(anchor, anchor + snippet, 1)
  p.write_text(t, encoding="utf-8")
PY

git add -A

if git diff --name-only --diff-filter=U | grep -q .; then
  echo "Unresolved merge conflicts remain:"
  git diff --name-only --diff-filter=U
  exit 1
fi

grep -q 'return params.get_int("ClusterHud") in (1, 2)' openpilot/system/manager/process_config.py
grep -q '# PythonProcess("cweb_push"' openpilot/system/manager/process_config.py
grep -q 'The NEXO cluster can temporarily lock cruise input' opendbc_repo/opendbc/car/hyundai/carcontroller.py
grep -q 'CANFD_JERK_LOWER_ACCEL_BP' opendbc_repo/opendbc/car/hyundai/carcontroller.py
python -m py_compile openpilot/system/manager/process_config.py opendbc_repo/opendbc/car/hyundai/carcontroller.py
python -m compileall -q openpilot/selfdrive/carrot/radar openpilot/selfdrive/carrot/cluster
git diff --check --cached

git commit -m "Merge latest openpilot_Carrot updates into NEXO"
git push origin HEAD:NEXO
