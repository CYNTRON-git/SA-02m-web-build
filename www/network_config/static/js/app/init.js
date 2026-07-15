/* SA-02m Web Interface -- INIT (extracted from app.js, F10 decomposition).
   Plain classic script sharing the global scope; original load order preserved.
   See index.html for the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   INIT
   ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  const verEl = document.getElementById('app-version');
  if (verEl) verEl.textContent = 'v' + APP_VERSION;

  initNav();
  applyDeepLinkTab();  // open a tab from #hash/?tab= (cloud settings deep-link)
  applyVariantVisibility('sa02m-1eth');
  initDashboardPlaceholders();
  initForms();
  initValidation();
  initWebCredsForm();
  initThemeToggle();
  handleUrlStatus();
  hydratePriorityWarmup();
  bindUsbPowerResetButton();
  loadVariant();
  bindStatusPollingLifecycle();
  initStatusPolling();

  /* Expose globals for inline onclick */
  window.setHw    = setHw;
  window.toggleHw = toggleHw;
  window.doRestart = doRestart;
  window.doReboot  = doReboot;
  window.loadServicesControl = loadServicesControl;
  window.loadKernelControl = loadKernelControl;
  window.applyKernelProfile = applyKernelProfile;
  window.applyCpuProfile = applyCpuProfile;
  window.serviceCtlAction = serviceCtlAction;
  window.doLogout  = doLogout;
  window.loadLog   = loadLog;
  window.loadSshDebug = loadSshDebug;
  window.syncTimeFromPC = syncTimeFromPC;
  window.exportInstallLog = exportInstallLog;
  window.toggleStorageAutoFormat = toggleStorageAutoFormat;

  document.getElementById('apply-variant-btn')?.addEventListener('click', applyVariant);
});
