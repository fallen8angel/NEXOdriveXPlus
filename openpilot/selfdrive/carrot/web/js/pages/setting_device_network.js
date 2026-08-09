"use strict";

let deviceNetworkRefreshTimer = null;
let deviceNetworkRefreshInFlight = false;

function shouldRefreshDeviceNetwork() {
  const deviceItems = document.getElementById("deviceItems");
  return (
    CURRENT_PAGE === "setting" &&
    CURRENT_SETTING_TAB === "device" &&
    CURRENT_DEVICE_GROUP === "Network" &&
    !document.hidden &&
    deviceItems &&
    !deviceItems.hidden &&
    deviceItems.style.display !== "none"
  );
}

function stopDeviceNetworkRefresh() {
  if (!deviceNetworkRefreshTimer) return;
  window.clearTimeout(deviceNetworkRefreshTimer);
  deviceNetworkRefreshTimer = null;
}

function scheduleDeviceNetworkRefresh(delay = DEVICE_NETWORK_REFRESH_MS) {
  stopDeviceNetworkRefresh();
  if (!shouldRefreshDeviceNetwork()) return;
  deviceNetworkRefreshTimer = window.setTimeout(() => {
    deviceNetworkRefreshTimer = null;
    refreshDeviceNetworkPanel().catch((err) => console.error("[DeviceTab]", err));
  }, delay);
}

function syncDeviceNetworkRefresh() {
  if (shouldRefreshDeviceNetwork()) scheduleDeviceNetworkRefresh();
  else stopDeviceNetworkRefresh();
}

async function refreshDeviceNetworkPanel() {
  if (!shouldRefreshDeviceNetwork() || deviceNetworkRefreshInFlight) {
    syncDeviceNetworkRefresh();
    return;
  }

  deviceNetworkRefreshInFlight = true;
  try {
    await renderDeviceItems("Network", false, { silentRefresh: true });
  } finally {
    deviceNetworkRefreshInFlight = false;
    syncDeviceNetworkRefresh();
  }
}

// NEXO-specific Tools extension. This file is already loaded directly by
// index.html, so loading the diagnostic card here does not require rebuilding
// js/generated/tools.js after a git pull.
(function loadNexoCanDiagTool() {
  if (document.querySelector('script[data-nexo-can-diag-loader="1"]')) return;
  const script = document.createElement("script");
  script.src = "/js/pages/nexo_can_diag.js";
  script.dataset.nexoCanDiagLoader = "1";
  script.async = false;
  document.head.appendChild(script);
})();
