"use strict";

const NEXO_CAN_DIAG_CMD = "python3 /data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py";

function findToolsBottom() {
  return document.querySelector("#pageTools .tools-scroll-stack") || document.getElementById("pageTools");
}

function makeDiagFilename() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `nexo-8sec-diagnostic-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.txt`;
}

function downloadDiagnosticText(text) {
  const blob = new Blob([String(text || "")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = makeDiagFilename();
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function ensureNexoCanDiagCard() {
  if (document.getElementById("nexoCanDiagCard")) return true;
  const root = findToolsBottom();
  if (!root) return false;

  const card = document.createElement("section");
  card.id = "nexoCanDiagCard";
  card.className = "row-wrap nexo-can-diag-card";
  card.style.cssText = "margin-top:12px;padding:14px;border-radius:16px;background:var(--card-bg,rgba(255,255,255,.055));border:1px solid rgba(255,255,255,.10);";

  const title = document.createElement("div");
  title.textContent = "NEXO 8초 통합진단";
  title.style.cssText = "font-size:16px;font-weight:800;margin-bottom:6px;";

  const desc = document.createElement("div");
  desc.textContent = "Panda fault · CAN IRQ · raw CAN · SCC/FCA · carState · controls · radar를 8초간 통합 확인합니다.";
  desc.style.cssText = "font-size:12px;opacity:.72;line-height:1.45;margin-bottom:10px;";

  const button = document.createElement("button");
  button.id = "btnNexoCanDiag";
  button.type = "button";
  button.textContent = "8초 통합진단 실행";
  button.style.cssText = "width:100%;min-height:48px;border:0;border-radius:12px;font-size:15px;font-weight:800;cursor:pointer;background:#1f8cff;color:#fff;";

  const download = document.createElement("button");
  download.id = "btnNexoCanDiagDownload";
  download.type = "button";
  download.textContent = "진단 TXT 다운로드";
  download.hidden = true;
  download.style.cssText = "width:100%;min-height:44px;margin-top:8px;border:1px solid rgba(255,255,255,.18);border-radius:12px;font-size:14px;font-weight:800;cursor:pointer;background:rgba(255,255,255,.08);color:inherit;";

  const result = document.createElement("pre");
  result.id = "nexoCanDiagResult";
  result.hidden = true;
  result.style.cssText = "margin:12px 0 0;padding:12px;border-radius:12px;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(0,0,0,.32);max-height:520px;overflow:auto;";

  let latestReport = "";
  download.addEventListener("click", () => {
    if (latestReport) downloadDiagnosticText(latestReport);
  });

  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    download.hidden = true;
    latestReport = "";
    button.textContent = "8초간 통합진단 중…";
    result.hidden = false;
    result.textContent = "Panda/CAN/SCC/radar 상태를 8초 동안 수집하고 있습니다…\n차량은 P단 정차 상태를 유지하세요.";
    try {
      const response = await fetch("/api/tools", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "shell_cmd", cmd: NEXO_CAN_DIAG_CMD }),
      });
      const data = await response.json();
      latestReport = String(data.out || data.error || "진단 결과가 없습니다.").trim();
      result.textContent = latestReport;
      result.scrollTop = 0;
      if (latestReport) download.hidden = false;
    } catch (error) {
      latestReport = `NEXO 8초 통합진단 실행 실패\n${error?.message || error}`;
      result.textContent = latestReport;
      download.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = "8초 통합진단 다시 실행";
    }
  });

  card.append(title, desc, button, download, result);
  root.appendChild(card);
  return true;
}

function initNexoCanDiag() {
  if (ensureNexoCanDiagCard()) return;
  const observer = new MutationObserver(() => {
    if (ensureNexoCanDiagCard()) observer.disconnect();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.setTimeout(() => observer.disconnect(), 15000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initNexoCanDiag, { once: true });
} else {
  initNexoCanDiag();
}
