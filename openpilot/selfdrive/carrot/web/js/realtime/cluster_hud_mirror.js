"use strict";

window.CarrotClusterHudMirror = (() => {
  const runtime = {
    active: false,
    image: null,
    previousStyles: new Map(),
  };

  const hiddenIds = [
    "carrotRoadVideo",
    "carrotLastFrameCanvas",
    "carrotPerformanceCanvas",
    "carrotOverlayCanvas",
    "carrotHudCanvas",
    "carrotOnroadAlert",
  ];

  function clusterHudEnabled() {
    return Number(window.CarrotDeviceRuntimeState?.clusterHud || 0) > 0;
  }

  function imageElement() {
    if (!runtime.image) runtime.image = document.getElementById("carrotClusterHudMirror");
    return runtime.image;
  }

  function saveAndHide(el) {
    if (!el) return;
    if (!runtime.previousStyles.has(el)) {
      runtime.previousStyles.set(el, {
        display: el.style.display,
        visibility: el.style.visibility,
        opacity: el.style.opacity,
      });
    }
    el.style.display = "none";
  }

  function restoreElements() {
    for (const [el, styles] of runtime.previousStyles.entries()) {
      el.style.display = styles.display;
      el.style.visibility = styles.visibility;
      el.style.opacity = styles.opacity;
    }
    runtime.previousStyles.clear();
  }

  function syncStopControl() {
    const button = document.getElementById("btnClusterHudMirrorStop");
    if (!button) return;
    button.hidden = !runtime.active;
    button.setAttribute("aria-hidden", runtime.active ? "false" : "true");
  }

  function emitChange(reason) {
    window.dispatchEvent(new CustomEvent("carrot:clusterhudmirrorchange", {
      detail: { active: runtime.active, reason: String(reason || "") },
    }));
  }

  function start(reason = "user start") {
    if (runtime.active) return true;
    if (!clusterHudEnabled()) return false;

    const image = imageElement();
    if (!image) return false;

    hiddenIds.forEach((id) => saveAndHide(document.getElementById(id)));
    image.hidden = false;
    image.style.display = "block";
    image.src = "/api/cluster_hud/mjpeg?t=" + Date.now();
    runtime.active = true;
    document.documentElement.dataset.carrotClusterHudMirror = "1";
    syncStopControl();
    emitChange(reason);
    return true;
  }

  function stop(reason = "user stop") {
    if (!runtime.active) return false;
    runtime.active = false;
    const image = imageElement();
    if (image) {
      image.removeAttribute("src");
      image.hidden = true;
      image.style.display = "none";
    }
    restoreElements();
    delete document.documentElement.dataset.carrotClusterHudMirror;
    syncStopControl();
    emitChange(reason);
    return true;
  }

  function syncAvailability() {
    if (runtime.active && !clusterHudEnabled()) stop("cluster hud disabled");
    return clusterHudEnabled();
  }

  function isActive() {
    return runtime.active;
  }

  const stopButton = document.getElementById("btnClusterHudMirrorStop");
  if (stopButton && stopButton.dataset.clusterHudMirrorBound !== "1") {
    stopButton.dataset.clusterHudMirrorBound = "1";
    stopButton.addEventListener("click", () => {
      if (typeof window.CarrotVisionStop === "function") {
        window.CarrotVisionStop("HUD mirror stop control");
      } else {
        stop("HUD mirror stop control");
      }
    });
  }

  const image = imageElement();
  if (image) {
    image.addEventListener("error", () => {
      if (!runtime.active) return;
      window.setTimeout(() => {
        if (!runtime.active) return;
        image.src = "/api/cluster_hud/mjpeg?t=" + Date.now();
      }, 600);
    });
  }
  syncStopControl();

  return Object.freeze({
    start,
    stop,
    isActive,
    isAvailable: clusterHudEnabled,
    syncAvailability,
  });
})();
