"use strict";

// Keep the bundled Tools feature as a loader only. The actual NEXO diagnostic
// UI lives in a standalone static file so a git pull can update it without a
// web bundle rebuild. That implementation waits for NEXO_DIAG_COMPLETE before
// downloading the TXT report.
(() => {
  if (document.querySelector('script[data-nexo-8sec-diag="1"]')) return;
  const script = document.createElement("script");
  script.src = "/js/pages/nexo_diag.js?v=20260810-1848";
  script.dataset.nexo8secDiag = "1";
  script.async = false;
  document.head.appendChild(script);
})();
