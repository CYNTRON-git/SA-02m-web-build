/* SA-02m Web Interface -- INIT (extracted from app.js, F10 decomposition).
   Plain classic script sharing the global scope; original load order preserved.
   See index.html for the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   INIT
   ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  const verEl = document.getElementById('app-version');
  if (verEl) {
    verEl.textContent = 'v' + APP_VERSION;  // immediate baked fallback
    // Auto version: overwrite with the ACTUALLY deployed version read from the
    // server at runtime (single source of truth = the VERSION file), so the
    // header self-corrects even against a stale cached APP_VERSION constant.
    // no-store keeps it live; any failure silently keeps the baked fallback.
    fetch('VERSION', { cache: 'no-store' })
      .then(r => (r.ok ? r.text() : Promise.reject()))
      .then(t => {
        const line = t.split('\n')
          .map(s => s.trim())
          .filter(s => s && s.charAt(0) !== '#')[0];
        if (line && /^\d+\.\d+/.test(line)) verEl.textContent = 'v' + line;
      })
      .catch(() => {});
  }

  initNav();
  if (typeof initNavDrawer === 'function') initNavDrawer();  // mobile drawer (app/nav.js)
  if (typeof initTopbarAutoHide === 'function') initTopbarAutoHide();  // hide topbar on scroll (short landscape)
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
  window.refreshKernelBoot = refreshKernelBoot;
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
