"use strict";

const NEXO_8SEC_DIAG_CMD = "python3 /data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py";
const NEXO_8SEC_REPORT_URL = "/download/nexo-8sec-diagnostic.txt";
const NEXO_8SEC_COMPLETE_MARKER = "NEXO_DIAG_COMPLETE";
const NEXO_8SEC_FAILED_MARKER = "NEXO_DIAG_FAILED";

function nexoDiagToolsRoot() {
  return document.querySelector("#pageTools .tools-scroll-stack") || document.getElementById("pageTools");
}

function nexoDiagFilename() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `nexo-8sec-diagnostic-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.txt`;
}

function nexoDownloadText(text, filename) {
  const blob = new Blob([String(text || "")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || nexoDiagFilename();
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function nexoStart8SecDiagnostic() {
  const response = await fetch("/api/tools", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify({ action: "shell_cmd", cmd: NEXO_8SEC_DIAG_CMD }),
  });
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    throw new Error(`진단 시작 응답 파싱 실패 (HTTP ${response.status})`);
  }
  const text = String(data?.out || data?.error || "").trim();
  if (!response.ok || data?.ok === false || !text.includes("NEXO_DIAG_STARTED")) {
    throw new Error(text || `진단 시작 실패 (HTTP ${response.status})`);
  }
}

async function nexoWaitForReport(onProgress) {
  // comma2 can need a few extra seconds after the 8 second capture to collect
  // tmux/process diagnostics, so allow enough headroom without accepting a
  // partial file.
  const deadline = Date.now() + 55000;
  let attempt = 0;
  while (Date.now() < deadline) {
    attempt += 1;
    if (typeof onProgress === "function") onProgress(attempt);
    try {
      const response = await fetch(`${NEXO_8SEC_REPORT_URL}?t=${Date.now()}`, { cache: "no-store" });
      if (response.ok) {
        const text = (await response.text()).trim();
        if (text.includes(NEXO_8SEC_FAILED_MARKER)) {
          throw new Error(text);
        }
        // The final URL is published atomically by the backend. Still require
        // an explicit completion marker so STARTED/partial output can never be
        // mistaken for a finished diagnostic and downloaded to the phone.
        if (text.includes(NEXO_8SEC_COMPLETE_MARKER)) {
          return text;
        }
      }
    } catch (e) {
      if (String(e?.message || e).includes(NEXO_8SEC_FAILED_MARKER)) throw e;
    }
    await sleep(1000);
  }
  throw new Error("55초 안에 완성된 진단 파일을 받지 못했습니다. STARTED 한 줄만 있는 파일은 더 이상 다운로드하지 않습니다.");
}

function ensureNexo8SecDiagnosticCard() {
  if (document.getElementById("nexo8SecDiagCard")) return true;
  const root = nexoDiagToolsRoot();
  if (!root) return false;

  const old = document.getElementById("nexoCanDiagCard");
  if (old) old.remove();

  const card = document.createElement("section");
  card.id = "nexo8SecDiagCard";
  card.className = "row-wrap nexo-can-diag-card";
  card.style.cssText = "margin-top:12px;padding:14px;border-radius:16px;background:var(--card-bg,rgba(255,255,255,.055));border:1px solid rgba(255,255,255,.10);";

  const title = document.createElement("div");
  title.textContent = "NEXO 통합진단 · 자동주차 20초";
  title.style.cssText = "font-size:16px;font-weight:800;margin-bottom:6px;";

  const desc = document.createElement("div");
  desc.textContent = "기존 통합진단과 함께 자동주차를 20초 관측합니다. 실행 후 순정 자동주차 버튼 → 주차공간 탐색 → 가능하면 자동조향 시작까지 진행하면 SPAS11 목표 조향각과 MDPS SPAS 활성 신호를 TXT에 기록합니다.";
  desc.style.cssText = "font-size:12px;opacity:.72;line-height:1.45;margin-bottom:10px;";

  const button = document.createElement("button");
  button.type = "button";
  button.id = "btnNexo8SecDiag";
  button.textContent = "통합진단 실행 · 자동주차 20초";
  button.style.cssText = "width:100%;min-height:48px;border:0;border-radius:12px;font-size:15px;font-weight:800;cursor:pointer;background:#1f8cff;color:#fff;";

  const status = document.createElement("div");
  status.hidden = true;
  status.style.cssText = "margin-top:10px;padding:10px 12px;border-radius:12px;font-size:13px;font-weight:700;background:rgba(255,255,255,.06);";

  const retryDownload = document.createElement("button");
  retryDownload.type = "button";
  retryDownload.textContent = "TXT 다시 다운로드";
  retryDownload.hidden = true;
  retryDownload.style.cssText = "width:100%;min-height:44px;margin-top:8px;border:1px solid rgba(255,255,255,.18);border-radius:12px;font-size:14px;font-weight:800;cursor:pointer;background:rgba(255,255,255,.08);color:inherit;";

  let lastText = "";
  let lastName = "";

  retryDownload.onclick = () => {
    if (lastText) nexoDownloadText(lastText, lastName);
  };

  button.onclick = async () => {
    if (button.disabled) return;
    button.disabled = true;
    retryDownload.hidden = true;
    status.hidden = false;
    status.textContent = "진단을 시작합니다…";
    button.textContent = "자동주차 신호 수집 중…";
    try {
      await nexoStart8SecDiagnostic();
      const text = await nexoWaitForReport((attempt) => {
        const sec = Math.min(35, attempt);
        status.textContent = sec <= 5 ? "진단 시작 · 순정 자동주차 버튼을 눌러주세요" : `자동주차 신호 수집 중… ${sec}초 · 주차공간 탐색/자동조향을 진행하세요`;
      });
      lastText = text;
      lastName = nexoDiagFilename();
      nexoDownloadText(lastText, lastName);
      status.textContent = `진단 완료 · ${lastName}`;
      retryDownload.hidden = false;
    } catch (e) {
      status.textContent = `진단 실패: ${e?.message || e}`;
    } finally {
      button.disabled = false;
      button.textContent = "통합진단 다시 실행 · 자동주차 20초";
    }
  };

  card.append(title, desc, button, status, retryDownload);
  root.appendChild(card);
  return true;
}

function initNexo8SecDiagnostic() {
  if (ensureNexo8SecDiagnosticCard()) return;
  const observer = new MutationObserver(() => {
    if (ensureNexo8SecDiagnosticCard()) observer.disconnect();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(() => observer.disconnect(), 20000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initNexo8SecDiagnostic, { once: true });
} else {
  initNexo8SecDiagnostic();
}

// Separate long-running logger. Existing 8-second diagnostic behavior above
// intentionally remains unchanged.
(() => {
  if (document.querySelector('script[data-nexo-long-log="1"]')) return;
  const script = document.createElement("script");
  script.src = "/js/pages/nexo_long_log.js?v=20260814-1404";
  script.dataset.nexoLongLog = "1";
  script.async = false;
  document.head.appendChild(script);
})();

// One-shot XPlus golden-reference backup. It is loaded from this standalone
// diagnostic file too, so an ordinary git pull can expose the button without
// depending on a rebuilt web bundle.
(() => {
  if (document.querySelector('script[data-nexo-golden-backup="1"]')) return;
  const script = document.createElement("script");
  script.src = "/js/pages/nexo_golden_backup.js?v=20260816-2312";
  script.dataset.nexoGoldenBackup = "1";
  script.async = false;
  document.head.appendChild(script);
})();
