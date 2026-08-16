"use strict";

const NEXO_GOLDEN_START_URL = "/api/nexo-golden-backup/start";
const NEXO_GOLDEN_STATUS_URL = "/api/nexo-golden-backup/status";
const NEXO_GOLDEN_DOWNLOAD_URL = "/download/nexo-xplus-golden-backup.tar.gz";
const NEXO_GOLDEN_MANIFEST_URL = "/view/nexo-xplus-golden-manifest.txt";

function nexoGoldenToolsRoot() {
  return document.querySelector("#pageTools .tools-scroll-stack") || document.getElementById("pageTools");
}

function nexoGoldenFilename(session) {
  const suffix = String(session || "").trim();
  return suffix ? `XPlus-NEXO-GOLDEN-${suffix}.tar.gz` : "XPlus-NEXO-GOLDEN.tar.gz";
}

function nexoGoldenSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function nexoGoldenJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    throw new Error(`응답 파싱 실패 (HTTP ${response.status})`);
  }
  if (!response.ok && response.status !== 409) {
    throw new Error(data?.error || `HTTP ${response.status}`);
  }
  return data || {};
}

async function nexoGoldenStart() {
  const data = await nexoGoldenJson(NEXO_GOLDEN_START_URL, { method: "POST" });
  if (data.ok === false && !data.active) {
    throw new Error(data.error || "골든 백업을 시작하지 못했습니다.");
  }
  return data;
}

async function nexoGoldenWait(onUpdate) {
  // This archive may include recent rlog/qlog files, so do not tie it to the
  // 8-second diagnostic deadline. Poll while the server reports active.
  while (true) {
    const data = await nexoGoldenJson(`${NEXO_GOLDEN_STATUS_URL}?t=${Date.now()}`);
    if (typeof onUpdate === "function") onUpdate(data);
    if (data.finished && !data.active) return data;
    if (data.error && !data.active) throw new Error(data.error);
    await nexoGoldenSleep(1000);
  }
}

function nexoGoldenDownload(session) {
  const a = document.createElement("a");
  a.href = `${NEXO_GOLDEN_DOWNLOAD_URL}?t=${Date.now()}`;
  a.download = nexoGoldenFilename(session);
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function ensureNexoGoldenBackupCard() {
  if (document.getElementById("nexoGoldenBackupCard")) return true;
  const root = nexoGoldenToolsRoot();
  if (!root) return false;

  const card = document.createElement("section");
  card.id = "nexoGoldenBackupCard";
  card.className = "row-wrap nexo-golden-backup-card";
  card.style.cssText = "margin-top:12px;padding:14px;border-radius:16px;background:var(--card-bg,rgba(255,255,255,.055));border:1px solid rgba(255,255,255,.10);";

  const title = document.createElement("div");
  title.textContent = "XPlus → NexoPilot 골든 백업";
  title.style.cssText = "font-size:16px;font-weight:800;margin-bottom:6px;";

  const desc = document.createElement("div");
  desc.textContent = "NexoPilot 설치 전에 현재 XPlus의 실제 dirty 소스 · 서브모듈 diff · Hyundai/Panda/DBC · CarParams/차량설정 · 런타임 상태 · 최근 rlog/qlog 시작/최신 구간을 한 번에 보존합니다. 차량 제어값은 변경하지 않습니다.";
  desc.style.cssText = "font-size:12px;opacity:.76;line-height:1.5;margin-bottom:10px;";

  const startButton = document.createElement("button");
  startButton.type = "button";
  startButton.id = "btnNexoGoldenBackup";
  startButton.textContent = "XPlus 골든 백업 만들기";
  startButton.style.cssText = "width:100%;min-height:48px;border:0;border-radius:12px;font-size:15px;font-weight:800;cursor:pointer;background:#16a56a;color:#fff;";

  const status = document.createElement("div");
  status.hidden = true;
  status.style.cssText = "margin-top:10px;padding:10px 12px;border-radius:12px;font-size:13px;font-weight:700;background:rgba(255,255,255,.06);line-height:1.45;";

  const row = document.createElement("div");
  row.style.cssText = "display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;";

  const downloadButton = document.createElement("button");
  downloadButton.type = "button";
  downloadButton.textContent = "골든백업 다운로드";
  downloadButton.hidden = true;
  downloadButton.style.cssText = "min-height:44px;border:1px solid rgba(255,255,255,.18);border-radius:12px;font-size:13px;font-weight:800;cursor:pointer;background:rgba(255,255,255,.08);color:inherit;";

  const manifestButton = document.createElement("button");
  manifestButton.type = "button";
  manifestButton.textContent = "목록 보기";
  manifestButton.hidden = true;
  manifestButton.style.cssText = "min-height:44px;border:1px solid rgba(255,255,255,.18);border-radius:12px;font-size:13px;font-weight:800;cursor:pointer;background:rgba(255,255,255,.08);color:inherit;";

  let lastSession = "";

  downloadButton.onclick = () => nexoGoldenDownload(lastSession);
  manifestButton.onclick = () => window.open(`${NEXO_GOLDEN_MANIFEST_URL}?t=${Date.now()}`, "_blank");

  function render(data) {
    const progress = Number(data?.progress || 0);
    const message = String(data?.message || "").trim();
    const size = Number(data?.archive_size || 0);
    const sizeText = size > 0 ? ` · ${(size / 1024 / 1024).toFixed(1)} MB` : "";
    lastSession = String(data?.session || lastSession || "");

    if (data?.active) {
      status.hidden = false;
      status.textContent = `${message || "골든 백업 진행 중"} · ${Math.max(0, Math.min(100, progress))}%`;
      startButton.disabled = true;
      startButton.textContent = "골든 백업 수집 중…";
      return;
    }

    if (data?.finished) {
      status.hidden = false;
      status.textContent = `골든 백업 완료${sizeText} · ${lastSession || "session"}`;
      startButton.disabled = false;
      startButton.textContent = "골든 백업 다시 만들기";
      downloadButton.hidden = false;
      manifestButton.hidden = false;
      return;
    }

    if (data?.error) {
      status.hidden = false;
      status.textContent = `골든 백업 실패: ${data.error}`;
      startButton.disabled = false;
      startButton.textContent = "골든 백업 다시 시도";
    }
  }

  startButton.onclick = async () => {
    if (startButton.disabled) return;
    startButton.disabled = true;
    downloadButton.hidden = true;
    manifestButton.hidden = true;
    status.hidden = false;
    status.textContent = "현재 XPlus 골든 자료 수집을 시작합니다…";
    try {
      const started = await nexoGoldenStart();
      render(started);
      const done = await nexoGoldenWait(render);
      render(done);
      nexoGoldenDownload(done.session);
    } catch (e) {
      render({ error: e?.message || String(e), active: false, finished: false });
    } finally {
      if (!startButton.textContent.includes("수집 중")) startButton.disabled = false;
    }
  };

  card.append(title, desc, startButton, status);
  row.append(downloadButton, manifestButton);
  card.appendChild(row);
  root.appendChild(card);

  // Restore server-side progress after a browser refresh.
  nexoGoldenJson(`${NEXO_GOLDEN_STATUS_URL}?t=${Date.now()}`)
    .then((data) => {
      render(data);
      if (data?.active) nexoGoldenWait(render).then(render).catch((e) => render({ error: e?.message || e }));
    })
    .catch(() => {});

  return true;
}

function initNexoGoldenBackup() {
  if (ensureNexoGoldenBackupCard()) return;
  const observer = new MutationObserver(() => {
    if (ensureNexoGoldenBackupCard()) observer.disconnect();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(() => observer.disconnect(), 20000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initNexoGoldenBackup, { once: true });
} else {
  initNexoGoldenBackup();
}
