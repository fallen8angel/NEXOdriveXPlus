"use strict";

// Keep the bundled Tools feature as a loader only. The actual NEXO diagnostic
// UI lives in standalone static files so a git pull can update them without a
// web bundle rebuild. The 8-second UI waits for NEXO_DIAG_COMPLETE before
// downloading the TXT report.
(() => {
  if (!document.querySelector('script[data-nexo-8sec-diag="1"]')) {
    const script = document.createElement("script");
    script.src = "/js/pages/nexo_diag.js?v=20260816-2312";
    script.dataset.nexo8secDiag = "1";
    script.async = false;
    document.head.appendChild(script);
  }

  if (!document.querySelector('script[data-nexo-golden-backup="1"]')) {
    const script = document.createElement("script");
    script.src = "/js/pages/nexo_golden_backup.js?v=20260816-2312";
    script.dataset.nexoGoldenBackup = "1";
    script.async = false;
    document.head.appendChild(script);
  }
})();
