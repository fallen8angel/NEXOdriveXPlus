"use strict";

const NEXO_8SEC_DIAG_CMD = "python3 /data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag_download.py";

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

async function nexoRun8SecDiagnostic() {
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
    throw new Error(`서버 응답 파싱 실패 (HTTP ${response.status})`);
  }

  const text = String(data?.out || data?.error || "").trim();
  if (!response.ok || data?.ok === false) {
    throw new Error(text || `HTTP ${response.status}`);
  }
  if (!text) throw new Error("진단 결과가 비어 있습니다.");
  return text;
}

function ensureNexo8SecDiagnosticCard() {
  if (document.getElementById("nexo8SecDiagCard")) return true;
  const root = nexoDiagToolsRoot();
  if (!root) return false;

  // Remove older experimental card so only one diagnostic UI remains.
  const old = document.getElementById("nexoCanDiagCard");
  if (old) old.remove();

  const card = document.createElement("section");
  card.id = "nexo8SecDiagCard";
  card.className = "row-wrap nexo-can-diag-card";
  card.style.cssText = "margin-top:12px;padding:14px;border-radius:16px;background:var(--card-bg,rgba(255,255,255,.055));border:1px solid rgba(255,255,255,.10);";

  const title = document.createElement("div");
  title.textContent = "NEXO 8초 통합진단";
  title.style.cssText = "font-size:16px;font-weight:800;margin-bottom:6px;";

  const desc = document.createElement("div");
  desc.textContent = "8초간 Panda · CAN · CarParams · carState · SCC/FCA · 프로세스 · 크루즈/LIMIT 버튼을 수집한 뒤 TXT만 다운로드합니다.";
  desc.style.cssText = "font-size:12px;opacity:.72;line-height:1.45;margin-bottom:10px;";

  const button = document.createElement("button");
  button.type = "button";
  button.id = "btnNexo8SecDiag";
  button.textContent = "8초 통합진단 실행";
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
    status.textContent = "8초 진단 중입니다. 잠시 기다려 주세요.";
    button.textContent = "8초간 진단 중…";
    try {
      const text = await nexoRun8SecDiagnostic();
      lastText = text;
      lastName = nexoDiagFilename();
      nexoDownloadText(lastText, lastName);
      status.textContent = `진단 완료 · ${lastName}`;
      retryDownload.hidden = false;
    } catch (e) {
      status.textContent = `진단 실패: ${e?.message || e}`;
    } finally {
      button.disabled = false;
      button.textContent = "8초 통합진단 다시 실행";
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
