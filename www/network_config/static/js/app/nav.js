/* SA-02m Web Interface -- MOBILE NAV DRAWER (Phase 3 / A1).
   Plain classic script sharing the global scope; loaded after app/init.js.
   See index.html for the ordered <script> tags.

   At phone widths the CSS (RESPONSIVE section, S <=700) turns the static
   .sidebar into a left drawer (translateX offscreen) opened by the topbar
   hamburger (#nav-burger). Desktop is untouched: above 700px the burger and
   backdrop are display:none and the sidebar is a normal column, so this JS is
   inert there (toggling .nav-open has no visual effect above the breakpoint).

   This module toggles a wrapper class (.app.nav-open), publishes the measured
   topbar height as --drawer-top (the phone bar is taller than --topbar-h, and
   the drawer max-height must use the same offset), and closes the drawer on
   backdrop tap / Esc / nav selection. The existing .nav-item data-tab click
   handlers (app.js initNav) are NOT touched -- the close is a separate
   delegated listener on the sidebar. */
'use strict';

function initNavDrawer() {
  var app = document.querySelector('.app');
  var burger = document.getElementById('nav-burger');
  var backdrop = document.getElementById('nav-backdrop');
  var sidebar = document.querySelector('.sidebar');
  if (!app || !burger) return;

  function isOpen() { return app.classList.contains('nav-open'); }
  // One measured offset for the drawer's top and its max-height. The mobile
  // topbar is height:auto (two rows) and taller than --topbar-h; the CSS
  // fallback alone lets the scrollport end below the viewport. The variable
  // is only consumed by the drawer media queries, so it is inert on desktop.
  function pinDrawerUnderTopbar() {
    var tb = document.querySelector('.topbar');
    if (!tb) return;
    app.style.setProperty('--drawer-top', tb.offsetHeight + 'px');
  }
  function openDrawer() {
    pinDrawerUnderTopbar();
    app.classList.add('nav-open');
  }
  function closeDrawer() { app.classList.remove('nav-open'); }
  function toggleDrawer() { if (isOpen()) { closeDrawer(); } else { openDrawer(); } }

  burger.addEventListener('click', toggleDrawer);

  if (backdrop) {
    backdrop.addEventListener('click', closeDrawer);
  }

  /* Esc closes the drawer only when it is open. */
  document.addEventListener('keydown', function (e) {
    if (isOpen() && (e.key === 'Escape' || e.keyCode === 27)) {
      closeDrawer();
    }
  });

  /* Delegated close-on-select: a leaf .nav-item or any .nav-sub-item is a final
     navigation choice -> dismiss the drawer. The .nav-parent (gateway) only
     expands its sub-list, so it must NOT close. Delegation survives the
     dynamically-updated gateway sub-items. */
  if (sidebar) {
    sidebar.addEventListener('click', function (e) {
      var node = e.target;
      while (node && node !== sidebar) {
        if (node.classList) {
          if (node.classList.contains('nav-sub-item')) { closeDrawer(); return; }
          if (node.classList.contains('nav-item')) {
            if (!node.classList.contains('nav-parent')) { closeDrawer(); }
            return;
          }
        }
        node = node.parentNode;
      }
    });
  }

  window.closeNavDrawer = closeDrawer;
  pinDrawerUnderTopbar();
  window.addEventListener('resize', pinDrawerUnderTopbar);
}

window.initNavDrawer = initNavDrawer;

/* Auto-hide the topbar on scroll-down in landscape (phone and short tablet).
   A 500px cap missed phones whose landscape viewport is taller than that, so
   the bar stayed and covered the page. Scroll up shows it again. */
function initTopbarAutoHide() {
  var app = document.querySelector('.app');
  var main = document.querySelector('.main');
  if (!app || !main) return;
  var lastY = 0;
  var shortLandscape = function () {
    return window.matchMedia
      && window.matchMedia('(orientation: landscape) and (max-height: 720px)').matches;
  };
  var onScroll = function (y) {
    if (!shortLandscape()) {
      if (app.classList.contains('topbar-hidden')) app.classList.remove('topbar-hidden');
      lastY = y;
      return;
    }
    if (y > lastY + 4 && y > 48) {
      app.classList.add('topbar-hidden');
    } else if (y < lastY - 4 || y <= 8) {
      app.classList.remove('topbar-hidden');
    }
    lastY = y;
  };
  main.addEventListener('scroll', function () { onScroll(main.scrollTop); }, { passive: true });
  window.addEventListener('scroll', function () {
    onScroll(window.scrollY || document.documentElement.scrollTop || 0);
  }, { passive: true });
}

window.initTopbarAutoHide = initTopbarAutoHide;
