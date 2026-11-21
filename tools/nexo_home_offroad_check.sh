#!/usr/bin/env bash
# NEXOdriveXPlus: prepare comma four <-> Jetson SSH while both devices are off the car.
# Usage: bash tools/nexo_home_offroad_check.sh SHA256:<expected Jetson ED25519 fingerprint>
# Does not start driving, change vehicle configuration, restart SSH, or update firmware.
set -euo pipefail
umask 077

EXPECTED="${1:-}"
JETSON_HOST="${JETSON_HOST:-jetlink@192.168.100.250}"
JETSON_PUBLIC_KEY="${JETSON_PUBLIC_KEY:-/opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519.pub}"
JETSON_PRIVATE_KEY="${JETSON_PRIVATE_KEY:-/opt/carrot-jetlink/cache/nexo-ai-ssh/id_ed25519}"
COMMA_NCM_IP="${COMMA_NCM_IP:-192.168.100.127}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '\n== %s ==\n' "$*"; }

[[ "$EXPECTED" == SHA256:* ]] || die "Provide expected key fingerprint as first argument (SHA256:...)."
[[ "$(id -un)" == "comma" ]] || die "Run as comma on the comma four, not as root or on Jetson."
[[ -d "${HOME}/" ]] || die "HOME directory is unavailable."
command -v ssh-keygen >/dev/null || die "ssh-keygen is missing."
command -v ssh >/dev/null || die "ssh is missing."

TEMP_PUB="$(mktemp)"
trap 'rm -f "$TEMP_PUB"' EXIT
note "Read Jetson public key (never copy or print its private key)"
ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$JETSON_HOST" "cat '$JETSON_PUBLIC_KEY'" > "$TEMP_PUB" ||
  die "Cannot read Jetson public key. Check existing comma -> Jetson SSH."
[[ "$(wc -l < "$TEMP_PUB")" -eq 1 ]] || die "Unexpected public-key file format."
FINGERPRINT="$(ssh-keygen -lf "$TEMP_PUB" -E sha256 | awk 'NR==1 {print $2}')" ||
  die "Cannot parse Jetson public key."
[[ "$FINGERPRINT" == "$EXPECTED" ]] ||
  die "Fingerprint mismatch. Expected $EXPECTED, got $FINGERPRINT. No files changed."
printf 'Verified Jetson public key: %s\n' "$FINGERPRINT"

note "Prepare target SSH files without deleting existing access"
[[ ! -L "$HOME/.ssh" ]] || die "Refusing to use symlinked ~/.ssh."
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
STAMP="$(date +%Y%m%d-%H%M%S)"

install_public_key() {
  local target="$1"
  local label="$2"
  local body
  [[ ! -L "$target" ]] || die "Refusing symlink at $target."
  if [[ -e "$target" ]]; then
    [[ -f "$target" && -r "$target" && -w "$target" ]] ||
      die "Existing $target is not a writable regular file."
    cp -p -- "$target" "${target}.bak.${STAMP}" ||
      die "Failed to back up $target. No write attempted."
    printf '%s backed up to %s\n' "$label" "${target}.bak.${STAMP}"
  else
    : > "$target" || die "Cannot create $target."
  fi
  body="$(awk 'NR==1 {print $2}' "$TEMP_PUB")"
  if grep -Fq -- "$body" "$target"; then
    printf '%s: key already registered\n' "$label"
  else
    # Make sure an existing file without a trailing newline remains valid.
    if [[ -s "$target" ]] && [[ "$(tail -c 1 "$target" | od -An -tu1 | tr -d '[:space:]')" != 10 ]]; then
      printf '\n' >> "$target"
    fi
    cat "$TEMP_PUB" >> "$target"
    printf '%s: added verified Jetson public key\n' "$label"
  fi
  chmod 600 "$target"
  ssh-keygen -lf "$target" -E sha256 2>/dev/null | grep -Fq -- "$EXPECTED" ||
    die "Could not confirm key in $target."
}

install_public_key "$HOME/.ssh/authorized_keys" "OpenSSH authorized_keys"
install_public_key "/data/params/d/GithubSshKeys" "openpilot GithubSshKeys"

note "Check actual SSH server authorized-key sources (read-only)"
if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
  sudo -n /usr/sbin/sshd -T -C "user=comma,host=localhost,addr=${JETSON_HOST#*@}" 2>&1 |
    grep -E '^(authorizedkeysfile|authorizedkeyscommand|pubkeyauthentication) ' || true
else
  /usr/sbin/sshd -T 2>&1 |
    grep -E '^(authorizedkeysfile|authorizedkeyscommand|pubkeyauthentication) ' || true
fi

note "Test Jetson -> comma via dedicated Jetson key (no password)"
if ssh -T -o BatchMode=yes -o ConnectTimeout=10 "$JETSON_HOST" \
  "ssh -i '$JETSON_PRIVATE_KEY' -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 comma@$COMMA_NCM_IP 'printf REVERSE_SSH_OK'"; then
  printf '\nSUCCESS: Jetson -> comma SSH key authentication works.\n'
else
  printf '\nWARNING: Public key registered but reverse SSH did not succeed.\n' >&2
  printf 'Check comma USB-NCM IP, sshd key source, Jetson private-key permissions, and sshd logs.\n' >&2
fi

note "Read-only Jetson home diagnostics"
ssh -T -o BatchMode=yes -o ConnectTimeout=8 "$JETSON_HOST" '
  printf "host: "; hostname
  printf "kernel: "; uname -r
  printf "uptime: "; uptime -p
  printf "disk usage:\n"; df -h / /opt 2>/dev/null || true
  for s in carrot-jetlink.service carrot-jetlink-hud.service nexo-jetson-ncm.service; do
    printf "%s: " "$s"; systemctl is-active "$s" 2>/dev/null || true
  done
  printf "Jetson platform release:\\n"; head -3 /etc/nv_tegra_release 2>/dev/null || true
  printf "AI cache metadata paths (read-only):\\n"
  find /opt/carrot-jetlink/cache -maxdepth 4 -type f \\( -name "last-loaded.json" -o -name "build.json" -o -name "result.json" \\) -print 2>/dev/null | head -30 || true
  printf "GPU device nodes:\\n"; ls -l /dev/nvhost-gpu /dev/nvidia0 2>/dev/null || true
  printf "External HUD USB status (disconnected at home is normal):\\n"
  lsusb 2>/dev/null | grep -Ei "1cbe:0092|1cbe:0123" || echo "no supported HUD USB attached"
  printf "USB NCM interfaces:\n"; ip -brief addr 2>/dev/null | grep -E "192\\.168\\.100\\.|usb|enx" || true
' || printf 'WARNING: Jetson read-only diagnostic failed.\n' >&2

note "Complete: no driving controls, firmware, SSH service restarts, or Git state changed"
