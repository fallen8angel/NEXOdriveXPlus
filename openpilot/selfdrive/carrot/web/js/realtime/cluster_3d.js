"use strict";

/*
 * Cluster HUD companion 3D view for Carrot Web.
 *
 * This renderer deliberately does not open the road-camera WebRTC stream.
 * It consumes the same compact openpilot state that feeds the external HUD:
 * carState/modelV2/radarState/liveTracks/lateralPlan. This lets the 7000
 * display run beside ClusterHud without taking a second camera stream.
 */
window.CarrotCluster3D = (() => {
  const BLINK_PERIOD_MS = 1000;
  const BLINK_ON_MS = 500;
  const MAX_DISTANCE_M = 110;
  const DEFAULT_LANE_WIDTH_M = 3.6;
  const MAX_TRACKS = 28;

  const runtime = {
    active: false,
    lease: null,
    leftStartMs: null,
    rightStartMs: null,
    hazardStartMs: null,
    leftPrev: false,
    rightPrev: false,
  };

  function finite(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function isActive() {
    return runtime.active;
  }

  function clusterHudEnabled() {
    return Number(window.CarrotDeviceRuntimeState?.clusterHud || 0) > 0;
  }

  function acquireDataLease() {
    if (runtime.lease?.active) return true;
    const activity = window.CarrotDriveDataActivity;
    if (!activity || typeof activity.acquire !== "function") {
      console.error("[cluster3d] drive-data activity facade unavailable");
      return false;
    }
    runtime.lease = activity.acquire({
      owner: "cluster-hud-3d",
      hud: true,
      overlay: true,
      tracks: true,
    });
    return true;
  }

  function releaseDataLease() {
    try {
      runtime.lease?.release?.();
    } catch (_) {}
    runtime.lease = null;
  }

  function resetBlinkState() {
    runtime.leftStartMs = null;
    runtime.rightStartMs = null;
    runtime.hazardStartMs = null;
    runtime.leftPrev = false;
    runtime.rightPrev = false;
  }

  function emitChange(reason) {
    window.dispatchEvent(new CustomEvent("carrot:cluster3dchange", {
      detail: { active: runtime.active, reason: String(reason || "") },
    }));
  }

  function requestRender(reason) {
    if (typeof window.requestCarrotVisionRender === "function") {
      window.requestCarrotVisionRender({
        force: true,
        overlayDirty: true,
        hudDirty: true,
        reason: reason || "cluster 3d",
      });
    } else {
      window.dispatchEvent(new CustomEvent("carrot:render-request", {
        detail: { force: true, overlayDirty: true, hudDirty: true },
      }));
    }
  }

  function start(reason = "user start") {
    if (runtime.active) return true;
    if (!clusterHudEnabled()) return false;
    if (!acquireDataLease()) return false;
    runtime.active = true;
    resetBlinkState();
    document.documentElement.dataset.carrotCluster3d = "1";
    emitChange(reason);
    requestRender(reason);
    return true;
  }

  function restoreStageMedia() {
    const ids = ["carrotRoadVideo", "carrotLastFrameCanvas", "carrotPerformanceCanvas"];
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.style.removeProperty("visibility");
      el.style.removeProperty("opacity");
    });
    ["carrotOverlayCanvas", "carrotHudCanvas"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.style.removeProperty("transform");
      el.style.removeProperty("transform-origin");
    });
  }

  function stop(reason = "user stop") {
    if (!runtime.active) return false;
    runtime.active = false;
    releaseDataLease();
    resetBlinkState();
    delete document.documentElement.dataset.carrotCluster3d;
    restoreStageMedia();
    emitChange(reason);
    requestRender(reason);
    return true;
  }

  function syncAvailability() {
    if (runtime.active && !clusterHudEnabled()) stop("cluster hud disabled");
    return clusterHudEnabled();
  }

  function blinkState(carState, nowMs) {
    const left = Boolean(carState?.leftBlinker);
    const right = Boolean(carState?.rightBlinker);

    if (left && right) {
      if (runtime.hazardStartMs == null || !(runtime.leftPrev && runtime.rightPrev)) {
        runtime.hazardStartMs = nowMs;
        runtime.leftStartMs = null;
        runtime.rightStartMs = null;
      }
      const lit = ((nowMs - runtime.hazardStartMs) % BLINK_PERIOD_MS) < BLINK_ON_MS;
      runtime.leftPrev = left;
      runtime.rightPrev = right;
      return { left, right, leftLit: lit, rightLit: lit };
    }

    runtime.hazardStartMs = null;
    if (left) {
      if (!runtime.leftPrev || runtime.leftStartMs == null) runtime.leftStartMs = nowMs;
    } else {
      runtime.leftStartMs = null;
    }
    if (right) {
      if (!runtime.rightPrev || runtime.rightStartMs == null) runtime.rightStartMs = nowMs;
    } else {
      runtime.rightStartMs = null;
    }

    const leftLit = left && ((nowMs - runtime.leftStartMs) % BLINK_PERIOD_MS) < BLINK_ON_MS;
    const rightLit = right && ((nowMs - runtime.rightStartMs) % BLINK_PERIOD_MS) < BLINK_ON_MS;
    runtime.leftPrev = left;
    runtime.rightPrev = right;
    return { left, right, leftLit, rightLit };
  }

  function setupCanvas(canvas, width, height) {
    if (!canvas) return null;
    const dpr = Math.min(Math.max(1, Number(window.devicePixelRatio) || 1), 1.5);
    const pixelWidth = Math.max(1, Math.round(width * dpr));
    const pixelHeight = Math.max(1, Math.round(height * dpr));
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    canvas.style.transform = "none";
    canvas.style.transformOrigin = "0 0";
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return ctx;
  }

  function prepareStageMedia() {
    ["carrotRoadVideo", "carrotLastFrameCanvas", "carrotPerformanceCanvas"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.style.visibility = "hidden";
      el.style.opacity = "0";
    });
  }

  function projection(width, height) {
    const cx = width * 0.5;
    const horizonY = height * 0.22;
    const egoY = height * 0.89;
    return {
      cx,
      horizonY,
      egoY,
      point(forwardM, lateralM, zM = 0) {
        const forward = clamp(finite(forwardM), -4, MAX_DISTANCE_M);
        const effective = Math.max(0, forward);
        const depthT = effective / (effective + 14);
        const y = egoY - (egoY - horizonY) * depthT - finite(zM) * (height * 0.025) / (1 + effective / 12);
        const lateralScale = width * 0.105 / (1 + effective / 12);
        return {
          x: cx - finite(lateralM) * lateralScale,
          y,
          scale: lateralScale,
          depth: effective,
        };
      },
    };
  }

  function pathPointSeries(path) {
    const xs = Array.isArray(path?.x) ? path.x : [];
    const ys = Array.isArray(path?.y) ? path.y : [];
    const zs = Array.isArray(path?.z) ? path.z : [];
    const count = Math.min(xs.length, ys.length || xs.length, 80);
    const out = [];
    for (let index = 0; index < count; index += 1) {
      const x = finite(xs[index], NaN);
      if (!Number.isFinite(x) || x < 0 || x > MAX_DISTANCE_M) continue;
      out.push({
        forward: x,
        lateral: ys.length ? finite(ys[index]) : 0,
        z: zs.length ? finite(zs[index]) : 0,
      });
    }
    return out;
  }

  function fallbackLine(lateral, maxDistance = 90) {
    const points = [];
    for (let distance = 1; distance <= maxDistance; distance += 4) {
      points.push({ forward: distance, lateral, z: 0 });
    }
    return points;
  }

  function drawPolyline(ctx, points, proj, color, widthPx = 2, dash = []) {
    if (!Array.isArray(points) || points.length < 2) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = widthPx;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.setLineDash(dash);
    ctx.beginPath();
    let moved = false;
    points.forEach((point) => {
      const p = proj.point(point.forward, point.lateral, point.z);
      if (!moved) {
        ctx.moveTo(p.x, p.y);
        moved = true;
      } else {
        ctx.lineTo(p.x, p.y);
      }
    });
    ctx.stroke();
    ctx.restore();
  }

  function drawRibbon(ctx, centerPoints, halfWidthM, proj, fillStyle) {
    if (!Array.isArray(centerPoints) || centerPoints.length < 2) return;
    const left = [];
    const right = [];
    centerPoints.forEach((point) => {
      left.push(proj.point(point.forward, point.lateral + halfWidthM, point.z));
      right.push(proj.point(point.forward, point.lateral - halfWidthM, point.z));
    });
    ctx.save();
    ctx.fillStyle = fillStyle;
    ctx.beginPath();
    left.forEach((point, index) => {
      if (index === 0) ctx.moveTo(point.x, point.y);
      else ctx.lineTo(point.x, point.y);
    });
    for (let index = right.length - 1; index >= 0; index -= 1) {
      ctx.lineTo(right[index].x, right[index].y);
    }
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function drawRoad(ctx, model, proj, width, height) {
    const gradient = ctx.createLinearGradient(0, proj.horizonY, 0, proj.egoY);
    gradient.addColorStop(0, "rgba(27,31,35,0.94)");
    gradient.addColorStop(1, "rgba(12,15,18,0.99)");

    const left = [];
    const right = [];
    for (let d = 0; d <= MAX_DISTANCE_M; d += 5) {
      left.push(proj.point(d, 7.2));
      right.push(proj.point(d, -7.2));
    }
    ctx.fillStyle = gradient;
    ctx.beginPath();
    left.forEach((p, index) => index ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
    for (let index = right.length - 1; index >= 0; index -= 1) ctx.lineTo(right[index].x, right[index].y);
    ctx.closePath();
    ctx.fill();

    const edges = Array.isArray(model?.roadEdges) ? model.roadEdges : [];
    if (edges.length >= 2) {
      edges.forEach((edge) => drawPolyline(ctx, pathPointSeries(edge), proj, "rgba(176,188,198,0.34)", 2));
    } else {
      drawPolyline(ctx, fallbackLine(7.2), proj, "rgba(176,188,198,0.30)", 2);
      drawPolyline(ctx, fallbackLine(-7.2), proj, "rgba(176,188,198,0.30)", 2);
    }

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.035)";
    ctx.lineWidth = 1;
    for (let d = 8; d <= 80; d += 8) {
      const a = proj.point(d, 7.0);
      const b = proj.point(d, -7.0);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawLaneHighlight(ctx, signals, proj) {
    function laneBand(side) {
      const inner = side === "left" ? DEFAULT_LANE_WIDTH_M * 0.5 : -DEFAULT_LANE_WIDTH_M * 0.5;
      const outer = side === "left" ? DEFAULT_LANE_WIDTH_M * 1.5 : -DEFAULT_LANE_WIDTH_M * 1.5;
      const innerPoints = fallbackLine(inner, 72);
      const outerPoints = fallbackLine(outer, 72);
      ctx.save();
      ctx.fillStyle = "rgba(44,205,117,0.20)";
      ctx.beginPath();
      innerPoints.forEach((point, index) => {
        const p = proj.point(point.forward, point.lateral);
        if (index === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      });
      for (let index = outerPoints.length - 1; index >= 0; index -= 1) {
        const p = proj.point(outerPoints[index].forward, outerPoints[index].lateral);
        ctx.lineTo(p.x, p.y);
      }
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
    if (signals.leftLit) laneBand("left");
    if (signals.rightLit) laneBand("right");
  }

  function drawLaneLines(ctx, model, proj) {
    const laneLines = Array.isArray(model?.laneLines) ? model.laneLines : [];
    const probs = Array.isArray(model?.laneLineProbs) ? model.laneLineProbs : [];
    if (laneLines.length) {
      laneLines.forEach((line, index) => {
        const probability = clamp(finite(probs[index], 0.65), 0, 1);
        const alpha = 0.18 + probability * 0.66;
        drawPolyline(ctx, pathPointSeries(line), proj, "rgba(231,238,244," + alpha.toFixed(3) + ")", index === 1 || index === 2 ? 3 : 2);
      });
      return;
    }
    [-5.4, -1.8, 1.8, 5.4].forEach((offset, index) => {
      drawPolyline(
        ctx,
        fallbackLine(offset),
        proj,
        index === 1 || index === 2 ? "rgba(235,241,246,0.72)" : "rgba(235,241,246,0.30)",
        index === 1 || index === 2 ? 3 : 2,
      );
    });
  }

  function drawPlannedPath(ctx, overlayState, proj) {
    const lateral = pathPointSeries(overlayState?.lateralPlan?.position);
    const model = pathPointSeries(overlayState?.modelV2?.position);
    const path = lateral.length >= 2 ? lateral : model;
    if (path.length < 2) return;
    drawRibbon(ctx, path, 0.52, proj, "rgba(34,190,229,0.24)");
    drawPolyline(ctx, path, proj, "rgba(90,223,250,0.92)", 3.5);
  }

  function leadArray(radarState) {
    const entries = [];
    function add(value, source, primary = false, cutIn = false) {
      if (!value || value.status === false) return;
      const dRel = finite(value.dRel, NaN);
      const yRel = finite(value.yRel, NaN);
      if (!Number.isFinite(dRel) || !Number.isFinite(yRel) || dRel <= 0.2 || dRel > MAX_DISTANCE_M) return;
      entries.push({
        dRel,
        yRel,
        vRel: finite(value.vRel),
        source,
        primary,
        cutIn,
        trackId: finite(value.radarTrackId, -1),
        radarSource: value.radarSource || "",
        fcw: Boolean(value.fcw),
      });
    }

    add(radarState?.leadOne, "leadOne", true, false);
    add(radarState?.leadTwo, "leadTwo", false, false);
    add(radarState?.leadLeft, "leadLeft", false, false);
    add(radarState?.leadRight, "leadRight", false, false);
    [
      ["leadsLeft", false],
      ["leadsCenter", false],
      ["leadsRight", false],
      ["leadsLeft2", false],
      ["leadsRight2", false],
      ["leadsCutIn", true],
    ].forEach(([key, cutIn]) => {
      const list = Array.isArray(radarState?.[key]) ? radarState[key] : [];
      list.forEach((lead) => add(lead, key, false, cutIn));
    });
    return entries;
  }

  function collectVehicles(overlayState) {
    const vehicles = leadArray(overlayState?.radarState);
    const tracks = Array.isArray(overlayState?.liveTracks?.points) ? overlayState.liveTracks.points : [];
    tracks.slice(0, MAX_TRACKS).forEach((track) => {
      const dRel = finite(track?.dRel, NaN);
      const yRel = finite(track?.yRel, NaN);
      if (!Number.isFinite(dRel) || !Number.isFinite(yRel) || dRel <= 0.2 || dRel > MAX_DISTANCE_M) return;
      const duplicate = vehicles.some((vehicle) => (
        Math.abs(vehicle.dRel - dRel) < 1.2 && Math.abs(vehicle.yRel - yRel) < 0.75
      ));
      if (duplicate) return;
      vehicles.push({
        dRel,
        yRel,
        vRel: finite(track?.vRel),
        source: "liveTrack",
        primary: false,
        cutIn: false,
        trackId: finite(track?.trackId, -1),
        radarSource: track?.radarSource || "",
        fcw: false,
      });
    });
    vehicles.sort((a, b) => b.dRel - a.dRel);
    return vehicles.slice(0, MAX_TRACKS);
  }

  function drawVehicle(ctx, vehicle, proj, carState) {
    const p = proj.point(vehicle.dRel, vehicle.yRel);
    const width = clamp(p.scale * 1.9, 11, 112);
    const height = width * 0.58;
    const x = p.x - width * 0.5;
    const y = p.y - height;

    ctx.save();
    ctx.globalAlpha = clamp(1.18 - vehicle.dRel / 135, 0.42, 1);
    ctx.shadowColor = "rgba(0,0,0,0.5)";
    ctx.shadowBlur = Math.max(2, width * 0.10);
    ctx.shadowOffsetY = Math.max(1, width * 0.05);

    const body = vehicle.fcw ? "#e3484e" : vehicle.cutIn ? "#d9a640" : vehicle.primary ? "#e7edf1" : "#9aa5ad";
    const side = vehicle.fcw ? "#a82e35" : vehicle.cutIn ? "#9e7324" : "#68737b";
    ctx.fillStyle = side;
    ctx.beginPath();
    ctx.moveTo(x + width * 0.08, y + height * 0.38);
    ctx.lineTo(x + width * 0.22, y + height * 0.08);
    ctx.lineTo(x + width * 0.78, y + height * 0.08);
    ctx.lineTo(x + width * 0.92, y + height * 0.38);
    ctx.lineTo(x + width, y + height);
    ctx.lineTo(x, y + height);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = body;
    ctx.beginPath();
    ctx.moveTo(x + width * 0.18, y + height * 0.38);
    ctx.lineTo(x + width * 0.30, y + height * 0.12);
    ctx.lineTo(x + width * 0.70, y + height * 0.12);
    ctx.lineTo(x + width * 0.82, y + height * 0.38);
    ctx.lineTo(x + width * 0.88, y + height * 0.83);
    ctx.lineTo(x + width * 0.12, y + height * 0.83);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = "rgba(31,45,55,0.92)";
    ctx.fillRect(x + width * 0.31, y + height * 0.20, width * 0.38, height * 0.21);

    const braking = vehicle.primary && finite(vehicle.vRel) < -1.6 && finite(carState?.vEgo) > 1.2;
    ctx.fillStyle = braking ? "#ff3131" : "#7f292d";
    const lampW = width * 0.18;
    const lampH = Math.max(2, height * 0.12);
    ctx.fillRect(x + width * 0.13, y + height * 0.60, lampW, lampH);
    ctx.fillRect(x + width * 0.69, y + height * 0.60, lampW, lampH);

    if (vehicle.primary || vehicle.cutIn || vehicle.fcw) {
      ctx.lineWidth = Math.max(1.5, width * 0.025);
      ctx.strokeStyle = vehicle.fcw ? "#ff4a50" : vehicle.cutIn ? "#ffc14b" : "rgba(255,255,255,0.92)";
      ctx.strokeRect(x - 3, y - 3, width + 6, height + 6);
    }
    ctx.restore();

    if (vehicle.primary && vehicle.dRel < 80) {
      ctx.save();
      ctx.font = "700 " + clamp(width * 0.23, 12, 22) + "px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.lineWidth = 4;
      ctx.strokeStyle = "rgba(0,0,0,0.86)";
      ctx.fillStyle = "#ffffff";
      const label = Math.round(vehicle.dRel) + "m";
      ctx.strokeText(label, p.x, y - 7);
      ctx.fillText(label, p.x, y - 7);
      ctx.restore();
    }
  }

  function drawEgo(ctx, proj, width, height, carState, signals) {
    const bodyW = clamp(width * 0.115, 92, 186);
    const bodyH = bodyW * 0.62;
    const cx = proj.cx;
    const baseY = height * 0.925;
    const x = cx - bodyW * 0.5;
    const y = baseY - bodyH;

    ctx.save();
    ctx.shadowColor = "rgba(0,0,0,0.55)";
    ctx.shadowBlur = 18;
    ctx.shadowOffsetY = 8;

    ctx.fillStyle = "#bac3ca";
    ctx.beginPath();
    ctx.moveTo(x + bodyW * 0.08, y + bodyH * 0.48);
    ctx.lineTo(x + bodyW * 0.22, y + bodyH * 0.08);
    ctx.lineTo(x + bodyW * 0.78, y + bodyH * 0.08);
    ctx.lineTo(x + bodyW * 0.92, y + bodyH * 0.48);
    ctx.lineTo(x + bodyW, y + bodyH);
    ctx.lineTo(x, y + bodyH);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = "#edf1f3";
    ctx.beginPath();
    ctx.moveTo(x + bodyW * 0.16, y + bodyH * 0.48);
    ctx.lineTo(x + bodyW * 0.29, y + bodyH * 0.14);
    ctx.lineTo(x + bodyW * 0.71, y + bodyH * 0.14);
    ctx.lineTo(x + bodyW * 0.84, y + bodyH * 0.48);
    ctx.lineTo(x + bodyW * 0.89, y + bodyH * 0.84);
    ctx.lineTo(x + bodyW * 0.11, y + bodyH * 0.84);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = "#26343e";
    ctx.beginPath();
    ctx.moveTo(x + bodyW * 0.30, y + bodyH * 0.24);
    ctx.lineTo(x + bodyW * 0.70, y + bodyH * 0.24);
    ctx.lineTo(x + bodyW * 0.76, y + bodyH * 0.48);
    ctx.lineTo(x + bodyW * 0.24, y + bodyH * 0.48);
    ctx.closePath();
    ctx.fill();

    const brake = Boolean(carState?.brakeLights);
    ctx.fillStyle = brake ? "#ff2828" : "#8a282e";
    ctx.beginPath();
    ctx.moveTo(x + bodyW * 0.10, y + bodyH * 0.58);
    ctx.lineTo(x + bodyW * 0.37, y + bodyH * 0.68);
    ctx.lineTo(x + bodyW * 0.11, y + bodyH * 0.76);
    ctx.closePath();
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(x + bodyW * 0.90, y + bodyH * 0.58);
    ctx.lineTo(x + bodyW * 0.63, y + bodyH * 0.68);
    ctx.lineTo(x + bodyW * 0.89, y + bodyH * 0.76);
    ctx.closePath();
    ctx.fill();

    if (signals.leftLit) {
      ctx.fillStyle = "#ffb000";
      ctx.fillRect(x + bodyW * 0.08, y + bodyH * 0.66, bodyW * 0.16, Math.max(4, bodyH * 0.07));
    }
    if (signals.rightLit) {
      ctx.fillStyle = "#ffb000";
      ctx.fillRect(x + bodyW * 0.76, y + bodyH * 0.66, bodyW * 0.16, Math.max(4, bodyH * 0.07));
    }

    ctx.fillStyle = "#1b2024";
    ctx.fillRect(x + bodyW * 0.18, y + bodyH * 0.82, bodyW * 0.64, bodyH * 0.08);
    ctx.restore();
  }

  function drawArrow(ctx, side, lit, width, height) {
    if (!lit) return;
    const direction = side === "left" ? -1 : 1;
    const cx = width * (side === "left" ? 0.30 : 0.70);
    const cy = height * 0.72;
    const size = clamp(Math.min(width, height) * 0.065, 30, 66);
    ctx.save();
    ctx.fillStyle = "#2ccd75";
    ctx.strokeStyle = "#087641";
    ctx.lineWidth = Math.max(2, size * 0.06);
    ctx.beginPath();
    ctx.moveTo(cx + direction * size, cy);
    ctx.lineTo(cx - direction * size * 0.12, cy - size * 0.62);
    ctx.lineTo(cx - direction * size * 0.12, cy - size * 0.25);
    ctx.lineTo(cx - direction * size * 0.76, cy - size * 0.25);
    ctx.lineTo(cx - direction * size * 0.76, cy + size * 0.25);
    ctx.lineTo(cx - direction * size * 0.12, cy + size * 0.25);
    ctx.lineTo(cx - direction * size * 0.12, cy + size * 0.62);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  function drawBlindspot(ctx, carState, width, height) {
    const left = Boolean(carState?.leftBlindspot);
    const right = Boolean(carState?.rightBlindspot);
    if (!left && !right) return;
    const y = height * 0.79;
    const radius = clamp(Math.min(width, height) * 0.018, 10, 22);
    ctx.save();
    ctx.fillStyle = "#ff3b3b";
    ctx.strokeStyle = "rgba(255,255,255,0.88)";
    ctx.lineWidth = 2;
    if (left) {
      ctx.beginPath();
      ctx.arc(width * 0.39, y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    if (right) {
      ctx.beginPath();
      ctx.arc(width * 0.61, y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawWorld(ctx, width, height, hudState, overlayState, signals) {
    const proj = projection(width, height);
    const bg = ctx.createLinearGradient(0, 0, 0, height);
    bg.addColorStop(0, "#151b20");
    bg.addColorStop(0.36, "#20282e");
    bg.addColorStop(1, "#080b0e");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);

    const model = overlayState?.modelV2 || null;
    drawRoad(ctx, model, proj, width, height);
    drawLaneHighlight(ctx, signals, proj);
    drawLaneLines(ctx, model, proj);
    drawPlannedPath(ctx, overlayState, proj);

    const carState = hudState?.carState || {};
    collectVehicles(overlayState).forEach((vehicle) => drawVehicle(ctx, vehicle, proj, carState));
    drawEgo(ctx, proj, width, height, carState, signals);
    drawBlindspot(ctx, carState, width, height);

    return proj;
  }

  function drawHud(ctx, width, height, hudState, signals) {
    const carState = hudState?.carState || {};
    drawArrow(ctx, "left", signals.leftLit, width, height);
    drawArrow(ctx, "right", signals.rightLit, width, height);

    const leftBlind = Boolean(carState.leftBlindspot);
    const rightBlind = Boolean(carState.rightBlindspot);
    if (leftBlind || rightBlind) {
      ctx.save();
      ctx.font = "800 " + clamp(height * 0.027, 13, 25) + "px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#ff5b5b";
      ctx.strokeStyle = "rgba(0,0,0,0.9)";
      ctx.lineWidth = 4;
      if (leftBlind) {
        ctx.strokeText("BSM", width * 0.385, height * 0.84);
        ctx.fillText("BSM", width * 0.385, height * 0.84);
      }
      if (rightBlind) {
        ctx.strokeText("BSM", width * 0.615, height * 0.84);
        ctx.fillText("BSM", width * 0.615, height * 0.84);
      }
      ctx.restore();
    }
  }

  function render(options = {}) {
    if (!runtime.active) return false;
    if (!clusterHudEnabled()) {
      stop("cluster hud unavailable");
      return false;
    }
    if (!runtime.lease?.active && !acquireDataLease()) return false;

    const width = Math.max(1, Math.round(finite(options.width, options.stage?.clientWidth || 1)));
    const height = Math.max(1, Math.round(finite(options.height, options.stage?.clientHeight || 1)));
    const worldCtx = setupCanvas(options.overlayCanvas, width, height);
    const hudCtx = setupCanvas(options.hudCanvas, width, height);
    if (!worldCtx || !hudCtx) return false;

    prepareStageMedia();
    const hudState = options.hudState || {};
    const overlayState = options.overlayState || {};
    const signals = blinkState(hudState?.carState || {}, performance.now());

    drawWorld(worldCtx, width, height, hudState, overlayState, signals);
    drawHud(hudCtx, width, height, hudState, signals);
    return true;
  }

  window.addEventListener("pagehide", () => {
    if (runtime.active) releaseDataLease();
  });

  return Object.freeze({
    start,
    stop,
    render,
    isActive,
    isAvailable: clusterHudEnabled,
    syncAvailability,
  });
})();
