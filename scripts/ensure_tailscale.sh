#!/usr/bin/env bash
set -u

log() {
  local msg="NEXOdriveXPlus Tailscale: $*"
  echo "$msg"
  command -v logger >/dev/null 2>&1 && logger -t xplus-tailscale "$msg" 2>/dev/null || true
}

find_ip_bin() {
  local candidate
  for candidate in "$(command -v ip 2>/dev/null || true)" /usr/sbin/ip /usr/bin/ip /sbin/ip /bin/ip; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

IP_BIN="$(find_ip_bin || true)"

tailscale_ready() {
  [ -n "$IP_BIN" ] || return 1
  "$IP_BIN" -4 -o addr show dev tailscale0 2>/dev/null | grep -Eq '\binet [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/'
}

wait_ready() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if tailscale_ready; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# A running daemon is only healthy when tailscale0 actually has an IPv4 address.
if tailscale_ready; then
  exit 0
fi

if pgrep -x tailscaled >/dev/null 2>&1; then
  log "daemon is running but tailscale0 has no IPv4; recovering"

  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files tailscaled.service >/dev/null 2>&1; then
    systemctl reset-failed tailscaled.service >/dev/null 2>&1 || true
    systemctl restart tailscaled.service >/dev/null 2>&1 || true
    if wait_ready; then
      log "recovered with systemd restart"
      exit 0
    fi
  fi

  pkill -TERM -x tailscaled >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    pgrep -x tailscaled >/dev/null 2>&1 || break
    sleep 1
  done
  if pgrep -x tailscaled >/dev/null 2>&1; then
    log "stale tailscaled did not stop"
    exit 13
  fi
fi

# Prefer an existing OS service. This preserves the installation's state/socket
# choices and does not install or replace any Tailscale identity.
if command -v systemctl >/dev/null 2>&1; then
  systemctl reset-failed tailscaled.service >/dev/null 2>&1 || true
  systemctl start tailscaled.service >/dev/null 2>&1 || true
  if wait_ready; then
    log "started with systemd"
    exit 0
  fi

  if pgrep -x tailscaled >/dev/null 2>&1; then
    systemctl stop tailscaled.service >/dev/null 2>&1 || pkill -TERM -x tailscaled >/dev/null 2>&1 || true
    sleep 1
  fi
fi

TAILSCALED=""
for candidate in \
  "$(command -v tailscaled 2>/dev/null || true)" \
  /usr/sbin/tailscaled \
  /usr/bin/tailscaled \
  /usr/local/sbin/tailscaled \
  /usr/local/bin/tailscaled \
  /data/tailscale/tailscaled \
  /data/tailscaled \
  /opt/tailscale/tailscaled; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    TAILSCALED="$candidate"
    break
  fi
done

if [ -z "$TAILSCALED" ]; then
  TAILSCALED="$(find /data /usr/local /opt -maxdepth 5 -type f -name tailscaled -perm /111 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$TAILSCALED" ]; then
  log "tailscaled binary not found"
  exit 10
fi

STATE=""
for candidate in \
  /var/lib/tailscale/tailscaled.state \
  /data/tailscale/tailscaled.state \
  /persist/tailscale/tailscaled.state \
  /data/tailscaled.state; do
  if [ -s "$candidate" ]; then
    STATE="$candidate"
    break
  fi
done

if [ -z "$STATE" ]; then
  STATE="$(find /data /var/lib /persist -maxdepth 5 -type f -name tailscaled.state -size +0c 2>/dev/null | head -n 1 || true)"
fi

# Never replace an existing paired identity with a blank daemon. A first-time
# Tailscale login must be done locally if no authenticated state exists yet.
if [ -z "$STATE" ]; then
  log "existing Tailscale state not found; refusing unauthenticated restart"
  exit 11
fi

SOCKET_DIR=/run/tailscale
SOCKET="$SOCKET_DIR/tailscaled.sock"
mkdir -p "$SOCKET_DIR"
rm -f "$SOCKET"

LOG_DIR=/data/xplus_remote
mkdir -p "$LOG_DIR"

nohup "$TAILSCALED" --state="$STATE" --socket="$SOCKET" \
  >>"$LOG_DIR/tailscaled.log" 2>&1 &

if wait_ready; then
  log "restarted directly using persisted state"
  exit 0
fi

log "restart attempt did not create tailscale0 IPv4; see $LOG_DIR/tailscaled.log"
exit 12
