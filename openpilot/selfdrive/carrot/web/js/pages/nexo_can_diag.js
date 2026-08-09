"use strict";

const NEXO_CAN_DIAG_CMD = "python3 /data/openpilot/openpilot/selfdrive/carrot/server/features/tools/nexo_can_diag.py";

function findToolsBottom() {
  return document.querySelector("#pageTools .tools-scroll-stack") || document.getElementById("pageTools");
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
  title.textContent = "NEXO CAN 진단";
  title.style.cssText = "font-size:16px;font-weight:800;margin-bottom:6px;";

  const desc = document.createElement("div");
  desc.textContent = "Panda fault · CAN IRQ · bus-off · RX 안전검사 · carState를 3초간 확인합니다.";
  desc.style.cssText = "font-size:12px;opacity:.72;line-height:1.45;margin-bottom:10px;";

  const button = document.createElement("button");
  button.id = "btnNexoCanDiag";
  button.type = "button";
  button.textContent = "CAN 진단 실행";
  button.style.cssText = "width:100%;min-height:48px;border:0;border-radius:12px;font-size:15px;font-weight:800;cursor:pointer;background:#1f8cff;color:#fff;";

  const result = document.createElement("pre");
  result.id = "nexoCanDiagResult";
  result.hidden = true;
  result.style.cssText = "margin:12px 0 0;padding:12px;border-radius:12px;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(0,0,0,.32);max-height:440px;overflow:auto;";

  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "3초간 진단 중…";
    result.hidden = false;
    result.textContent = "Panda/CAN 상태를 확인하고 있습니다…";
    try {
      const response = await fetch("/api/tools", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "shell_cmd", cmd: NEXO_CAN_DIAG_CMD }),
      });
      const data = await response.json();
      const text = String(data.out || data.error || "진단 결과가 없습니다.").trim();
      result.textContent = text;
      result.scrollTop = 0;
    } catch (error) {
      result.textContent = `CAN 진단 실행 실패\n${error?.message || error}`;
    } finally {
      button.disabled = false;
      button.textContent = "CAN 진단 다시 실행";
    }
  });

  card.append(title, desc, button, result);
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
