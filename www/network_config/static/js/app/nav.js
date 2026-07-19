/* SA-02m Web Interface -- MOBILE NAV DRAWER (Phase 3 / A1).
   Plain classic script sharing the global scope; loaded after app/init.js.
   See index.html for the ordered <script> tags.

   At phone widths the CSS (RESPONSIVE section, S <=700) turns the static
   .sidebar into a left drawer (translateX offscreen) opened by the topbar
   hamburger (#nav-burger). Desktop is untouched: above 700px the burger and
   backdrop are display:none and the sidebar is a normal column, so this JS is
   inert there (toggling .nav-open has no visual effect above the breakpoint).

   This module ONLY toggles a wrapper class (.app.nav-open) and closes the
   drawer on backdrop tap / Esc / nav selection. The existing .nav-item
   data-tab click handlers (app.js initNav) are NOT touched -- the close is a
   separate delegated listener on the sidebar. */
'use strict';

function initNavDrawer() {
  var app = document.querySelector('.app');
  var burger = document.getElementById('nav-burger');
  var backdrop = document.getElementById('nav-backdrop');
  var sidebar = document.querySelector('.sidebar');
  if (!app || !burger) return;

  function isOpen() { return app.classList.contains('nav-open'); }
  function openDrawer() { app.classList.add('nav-open'); }
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
}

window.initNavDrawer = initNavDrawer;
