#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from openpilot.common.swaglog import cloudlog


# /data/openpilot/openpilot/system/xplus_remote/remoteconnectd.py -> /data/openpilot
REPO_DIR = Path(__file__).resolve().parents[3]
TAILSCALE_HELPER = REPO_DIR / "scripts" / "ensure_tailscale.sh"
STATE_DIR = Path("/data/xplus_remote")
IP_FILE = STATE_DIR / "tailscale_ip"
XPLUS_NTFY_TOPIC_FILE = STATE_DIR / "ntfy_topic"
LEGACY_NTFY_TOPIC_FILE = Path("/data/nexopilot/ntfy_topic")
POLL_INTERVAL = 2.0
RECOVERY_INTERVAL = 30.0
FAILURE_NOTIFY_DELAY = 60.0
NOTIFY_RETRY_INTERVAL = 10.0


def ntfy_topic() -> str:
  """Load the XPlus topic, falling back to the existing NexoPilot topic."""
  for topic_file in (XPLUS_NTFY_TOPIC_FILE, LEGACY_NTFY_TOPIC_FILE):
    try:
      topic = topic_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
      continue

    if not topic:
      continue

    # Preserve a legacy NexoPilot topic in the XPlus state directory so future
    # repo switches do not unnecessarily lose the notification configuration.
    if topic_file == LEGACY_NTFY_TOPIC_FILE:
      try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not XPLUS_NTFY_TOPIC_FILE.exists():
          XPLUS_NTFY_TOPIC_FILE.write_text(topic + "\n", encoding="utf-8")
      except OSError:
        pass
    return topic

  return ""


def internet_available() -> bool:
  try:
    with socket.create_connection(("1.1.1.1", 443), timeout=2.0):
      return True
  except OSError:
    return False


def local_port_ready(port: int) -> bool:
  try:
    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
      return True
  except OSError:
    return False


def tailscale_ipv4() -> str:
  ip_bin = shutil.which("ip")
  if ip_bin is None:
    for candidate in ("/usr/sbin/ip", "/usr/bin/ip", "/sbin/ip", "/bin/ip"):
      if Path(candidate).is_file():
        ip_bin = candidate
        break
  if ip_bin is None:
    return ""

  try:
    result = subprocess.run(
      [ip_bin, "-4", "-o", "addr", "show", "dev", "tailscale0"],
      text=True,
      capture_output=True,
      timeout=3,
      check=False,
    )
  except Exception:
    return ""

  if result.returncode != 0:
    return ""
  match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", result.stdout)
  return match.group(1) if match else ""


def remember_ip(ip: str) -> None:
  try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    IP_FILE.write_text(ip + "\n", encoding="utf-8")
  except OSError:
    pass


def ensure_tailscale() -> None:
  if not TAILSCALE_HELPER.is_file():
    cloudlog.warning("XPlus remote: Tailscale recovery helper is missing")
    return
  try:
    result = subprocess.run(
      ["sudo", "-n", "bash", str(TAILSCALE_HELPER)],
      text=True,
      capture_output=True,
      timeout=25,
      check=False,
    )
    if result.returncode not in (0, 10, 11):
      detail = (result.stderr or result.stdout).strip()
      cloudlog.warning(f"XPlus remote: Tailscale recovery rc={result.returncode}: {detail[-500:]}")
  except Exception as error:
    cloudlog.warning(f"XPlus remote: Tailscale recovery failed: {error}")


def send_ntfy(message: str, *, priority: str = "default") -> bool:
  topic = ntfy_topic()
  if not topic:
    return False

  url = f"https://ntfy.sh/{quote(topic, safe='')}"
  request = Request(
    url,
    data=message.encode("utf-8"),
    headers={
      "Title": "NEXOdriveXPlus",
      "Priority": priority,
      "Tags": "car",
      "User-Agent": "NEXOdriveXPlus-remoteconnectd/2",
      "Content-Type": "text/plain; charset=utf-8",
    },
    method="POST",
  )
  try:
    with urlopen(request, timeout=10) as response:
      response.read(256)
    return True
  except Exception as error:
    cloudlog.warning(f"XPlus remote: ntfy send failed: {error}")
    return False


def main() -> None:
  started_at = time.monotonic()
  ready_announced = False
  failure_announced = False
  topic_missing_logged = False
  last_recovery = 0.0
  last_internet_check = 0.0
  last_notify_attempt = 0.0
  internet = False

  cloudlog.info("XPlus remote: connectivity monitor started")

  while True:
    now = time.monotonic()

    if now - last_internet_check >= 5.0:
      internet = internet_available()
      last_internet_check = now

    ts_ip = tailscale_ipv4()
    if not ts_ip and now - last_recovery >= RECOVERY_INTERVAL:
      ensure_tailscale()
      last_recovery = now
      ts_ip = tailscale_ipv4()

    web_ready = local_port_ready(7000)

    if (internet and ts_ip and web_ready and not ready_announced and
        now - last_notify_attempt >= NOTIFY_RETRY_INTERVAL):
      remember_ip(ts_ip)
      topic = ntfy_topic()
      if not topic:
        if not topic_missing_logged:
          cloudlog.warning("XPlus remote: ntfy topic is not configured")
          topic_missing_logged = True
        last_notify_attempt = now
      else:
        topic_missing_logged = False
        message = (
          "🚗 콤마 온라인\n"
          "NEXOdriveXPlus 원격접속 준비 완료\n"
          f"Tailscale: {ts_ip}\n"
          f"7000 서버: http://{ts_ip}:7000"
        )
        last_notify_attempt = now
        # Mark success only after ntfy accepts the message. A transient network
        # failure therefore retries instead of silently suppressing the alert.
        if send_ntfy(message):
          ready_announced = True
          failure_announced = False
          cloudlog.info(f"XPlus remote: startup online notification sent ({ts_ip})")

    if (internet and not ready_announced and not failure_announced and
        now - started_at >= FAILURE_NOTIFY_DELAY and (not ts_ip or not web_ready) and
        now - last_notify_attempt >= NOTIFY_RETRY_INTERVAL):
      if not ts_ip and not web_ready:
        reason = "Tailscale과 7000 서버가 아직 준비되지 않았습니다."
      elif not ts_ip:
        reason = "Tailscale 연결에 실패했습니다. 인터넷은 연결되어 있습니다."
      else:
        reason = "Tailscale은 연결됐지만 7000 서버가 아직 준비되지 않았습니다."

      cloudlog.warning(f"XPlus remote: not ready: {reason}")
      last_notify_attempt = now
      if send_ntfy(f"⚠️ XPlus 원격접속 준비 실패\n{reason}", priority="high"):
        failure_announced = True

    # If connectivity is lost after a successful ready notification, allow a
    # new notification after recovery.
    if ready_announced and (not ts_ip or not web_ready):
      ready_announced = False
      failure_announced = False
      started_at = now

    time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
  main()
