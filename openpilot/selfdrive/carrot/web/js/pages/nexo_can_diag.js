"use strict";

// Legacy loader retained for devices/browser caches that still request this
// path. Do not create a second diagnostic card here: always route to the
// completed-report implementation, which waits for NEXO_DIAG_COMPLETE before
// downloading anything.
(() => {
  if (document.querySelector('script[data-nexo-8sec-diag="1"]')) return;
  const script = document.createElement("script");
  script.src = "/js/pages/nexo_diag.js?v=20260810-1848";
  script.dataset.nexo8secDiag = "1";
  script.async = false;
  document.head.appendChild(script);
})();
