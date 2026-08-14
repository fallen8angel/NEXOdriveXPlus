"use strict";

const NEXO_LONG_START_URL = "/api/nexo-long-log/start";
const NEXO_LONG_STOP_URL = "/api/nexo-long-log/stop";
const NEXO_LONG_STATUS_URL = "/api/nexo-long-log/status";

function nexoLongToolsRoot() {
  return document.querySelector("#pageTools .tools-scroll-stack") || document.getElementById("pageTools");
}

function nexoLongFormatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}시간 ${String(minutes).padStart(2, "0")}분 ${String(secs).padStart(2, "0")}초`;
  return `${String(minutes).padStart(2, "0")}분 ${String(secs).padStart(2, "0")}초`;
}

async function nexoLongJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    throw new Error(`서버 응답 파싱 실패 (HTTP ${response.status})`);
  }
  if (!response.ok && !data?.processing) {
    throw new Error(data?.error || `요청 실패 (HTTP ${response.status})`);
  }
  return data;
}

function nexoLongOpenDownload(url) {
  if (!url) return;
  const a = document.createElement("a");
  a.href = `${url}?t=${Date.now()}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function ensureNexoLongLogCard() {
  if (document.getElementById("nexoLongLogCard")) return true;
  const shortCard = document.getElementById("nexo8SecDiagCard");
  const root = nexoLongToolsRoot();
  if (!shortCard || !root) return false;

  const card = document.createElement("section");
  card.id = "nexoLongLogCard";
  card.className = "row-wrap nexo-long-log-card";
  card.style.cssText = "margin-top:12px;padding:14px;border-radius:16px;background:var(--card-bg,rgba(255,255,255,.055));border:1px solid rgba(255,255,255,.10);";

  const title = document.createElement("div");
  title.textContent = "NEXO 장시간 개발 로그";
  title.style.cssText = "font-size:16px;font-weight:800;margin-bottom:6px;";

  const desc = document.createElement("div");
  desc.textContent = "NexoPilot 비교·개발용으로 CAN/sendcan · SCC/FCA · 레이더 · UDS · carState/Control · longitudinalPlan · Panda/CarParams · Git/설정 정보를 기록합니다. 카메라 영상은 기록하지 않습니다.";
  desc.style.cssText = "font-size:12px;opacity:.72;line-height:1.45;margin-bottom:10px;";

  const status = document.createElement("div");
  status.id = "nexoLongLogStatus";
  status.style.cssText = "margin-bottom:10px;padding:10px 12px;border-radius:12px;font-size:13px;font-weight:800;background:rgba(255,255,255,.06);";
  status.textContent = "기록 대기 중";

  const buttons = document.createElement("div");
  buttons.style.cssText = "display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;";

  const start = document.createElement("button");
  start.type = "button";
  start.id = "btnNexoLongStart";
  start.textContent = "기록 시작";
  start.style.cssText = "min-height:46px;border:0;border-radius:12px;font-size:14px;font-weight:800;cursor:pointer;background:#1f8cff;color:#fff;";

  const stop = document.createElement("button");
  stop.type = "button";
  stop.id = "btnNexoLongStop";
  stop.textContent = "기록 종료";
  stop.style.cssText = "min-height:46px;border:0;border-radius:12px;font-size:14px;font-weight:800;cursor:pointer;background:#e34b4b;color:#fff;";

  const download = document.createElement("button");
  download.type = "button";
  download.id = "btnNexoLongDownload";
  download.textContent = "다운받기";
  download.style.cssText = "min-height:46px;border:1px solid rgba(255,255,255,.18);border-radius:12px;font-size:14px;font-weight:800;cursor:pointer;background:rgba(255,255,255,.08);color:inherit;";

  buttons.append(start, stop, download);
  card.append(title, desc, status, buttons);
  shortCard.insertAdjacentElement("afterend", card);

  let lastState = null;
  let busy = false;

  function applyState(data) {
    lastState = data || {};
    const active = !!lastState.active;
    const finalizing = !!lastState.finalizing;
    const finished = !!lastState.finished;
    start.disabled = busy || active || finalizing;
    stop.disabled = busy || !active || finalizing;
    download.disabled = busy || !lastState.download_url;

    start.style.opacity = start.disabled ? ".45" : "1";
    stop.style.opacity = stop.disabled ? ".45" : "1";
    download.style.opacity = download.disabled ? ".45" : "1";

    if (active) {
      status.textContent = `● 기록 중　${nexoLongFormatElapsed(lastState.elapsed)}`;
    } else if (finalizing) {
      status.textContent = "기록 종료 처리 중… 로그 파일과 다운로드 패키지를 정리하고 있습니다.";
    } else if (finished) {
      status.textContent = `기록 완료 · ${nexoLongFormatElapsed(lastState.elapsed)} · 종료 즉시 결과를 열 수 있습니다.`;
    } else if (lastState.error) {
      status.textContent = `기록 오류: ${lastState.error}`;
    } else {
      status.textContent = "기록 대기 중";
    }
  }

  async function refreshStatus() {
    try {
      const data = await nexoLongJson(NEXO_LONG_STATUS_URL);
      applyState(data);
      return data;
    } catch (e) {
      if (!busy) status.textContent = `상태 확인 실패: ${e?.message || e}`;
      return null;
    }
  }

  start.onclick = async () => {
    if (busy || start.disabled) return;
    busy = true;
    status.textContent = "장시간 로그 기록을 시작합니다…";
    applyState(lastState);
    try {
      const data = await nexoLongJson(NEXO_LONG_START_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      applyState(data);
    } catch (e) {
      status.textContent = `기록 시작 실패: ${e?.message || e}`;
    } finally {
      busy = false;
      await refreshStatus();
    }
  };

  stop.onclick = async () => {
    if (busy || stop.disabled) return;

    // Open the result window synchronously with the button click so mobile
    // browsers do not block it as a popup after the asynchronous finalize call.
    const resultWindow = window.open("about:blank", "_blank");
    if (resultWindow) {
      try {
        resultWindow.document.title = "NEXO 로그 종료 중";
        resultWindow.document.body.textContent = "로그 종료 처리 중입니다…";
      } catch (e) {}
    }

    busy = true;
    status.textContent = "기록을 종료하고 결과를 정리합니다…";
    applyState({ ...(lastState || {}), active: false, finalizing: true });
    try {
      let data = await nexoLongJson(NEXO_LONG_STOP_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });

      // Very long sessions can need extra time to flush/compress. If the stop
      // endpoint returns "processing", keep checking the server-side state.
      const deadline = Date.now() + 180000;
      while ((!data?.finished || !data?.result_url) && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        data = await nexoLongJson(NEXO_LONG_STATUS_URL);
        applyState(data);
        if (data?.error && !data?.active && !data?.finalizing && !data?.finished) break;
      }

      if (!data?.result_url) {
        throw new Error(data?.error || "완성된 로그 결과 파일을 찾지 못했습니다.");
      }

      applyState(data);
      const target = `${data.result_url}?t=${Date.now()}`;
      if (resultWindow && !resultWindow.closed) {
        resultWindow.location.replace(target);
      } else {
        window.location.href = target;
      }
    } catch (e) {
      if (resultWindow && !resultWindow.closed) {
        try { resultWindow.close(); } catch (closeError) {}
      }
      status.textContent = `기록 종료 실패: ${e?.message || e}`;
    } finally {
      busy = false;
      await refreshStatus();
    }
  };

  download.onclick = () => {
    if (busy || download.disabled || !lastState?.download_url) return;
    nexoLongOpenDownload(lastState.download_url);
  };

  refreshStatus();
  setInterval(refreshStatus, 1000);
  return true;
}

function initNexoLongLog() {
  if (ensureNexoLongLogCard()) return;
  const observer = new MutationObserver(() => {
    if (ensureNexoLongLogCard()) observer.disconnect();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(() => observer.disconnect(), 30000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initNexoLongLog, { once: true });
} else {
  initNexoLongLog();
}
