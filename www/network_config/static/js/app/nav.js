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
  var root = document.documentElement;
  var mainState = { y: main.scrollTop, height: main.clientHeight, extent: main.scrollHeight };
  var windowState = { y: window.scrollY || root.scrollTop || 0,
    height: root.clientHeight, extent: root.scrollHeight };
  var shortLandscape = function () {
    return window.matchMedia
      && window.matchMedia('(orientation: landscape) and (max-height: 720px)').matches;
  };
  var clock = function () { return window.performance.now(); };
  var inputState = function (event) {
    if (event.defaultPrevented) return null;
    var target = event.target;
    while (target && target !== main && target !== root && target !== document.body) {
      if (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(target.tagName || '')) return null;
      if (target.scrollHeight > target.clientHeight && window.getComputedStyle
          && /^(auto|scroll)$/.test(window.getComputedStyle(target).overflowY)) return null;
      target = target.parentElement;
    }
    return target === main ? mainState : target === root || target === document.body ? windowState : null;
  };
  var noteInput = function (state, direction) {
    if (state) state.input = { direction: direction, at: clock() };
  };
  var clearInput = function () { mainState.input = null; windowState.input = null; };
  var onScroll = function (y, height, extent, state) {
    var lastY = state.y;
    var geometryChanged = height !== state.height || extent !== state.extent;
    var input = state.input;
    state.input = null;
    state.y = y;
    state.height = height;
    state.extent = extent;
    if (!shortLandscape() || y <= 8) {
      if (app.classList.contains('topbar-hidden')) app.classList.remove('topbar-hidden');
      return;
    }
    // Hiding/showing the header resizes the scrollport and can clamp scrollTop
    // near the bottom. Rebase that layout sample, not a new user direction.
    // Each scrollport keeps its own baseline; later real gestures still apply.
    // A fresh, matching gesture can reverse direction during the transition.
    // Consume its witness on this sample, including zero/clamped movement;
    // input without a scroll must not authorize a later layout event.
    var directedInput = input && clock() - input.at <= 150
      && (y - lastY) * input.direction > 4;
    if (geometryChanged && !directedInput) return;
    if (y > lastY + 4 && y > 48) {
      app.classList.add('topbar-hidden');
    } else if (y < lastY - 4) {
      app.classList.remove('topbar-hidden');
    }
  };
  main.addEventListener('scroll', function () {
    onScroll(main.scrollTop, main.clientHeight, main.scrollHeight, mainState);
  }, { passive: true });
  window.addEventListener('scroll', function () {
    onScroll(window.scrollY || root.scrollTop || 0, root.clientHeight, root.scrollHeight, windowState);
  }, { passive: true });
  window.addEventListener('wheel', function (event) {
    clearInput();
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    var state = inputState(event);
    var scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 && state ? state.height : 1;
    if (Math.abs(event.deltaY * scale) > 4) noteInput(state, event.deltaY > 0 ? 1 : -1);
  }, { passive: true });
  window.addEventListener('keydown', function (event) {
    clearInput();
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    var direction = /^(ArrowUp|PageUp|Home)$/.test(event.key) ? -1
      : /^(ArrowDown|PageDown|End)$/.test(event.key) ? 1
      : event.key === ' ' ? (event.shiftKey ? -1 : 1) : 0;
    if (direction) noteInput(inputState(event), direction);
  });
  var touch = null;
  window.addEventListener('touchstart', function (event) {
    clearInput();
    touch = event.touches.length === 1
      ? { state: inputState(event), y: event.touches[0].clientY } : null;
  }, { passive: true });
  window.addEventListener('touchmove', function (event) {
    clearInput();
    if (!touch || event.touches.length !== 1 || event.defaultPrevented) { touch = null; return; }
    var y = event.touches[0].clientY;
    var delta = touch.y - y;
    if (Math.abs(delta) > 4) {
      noteInput(touch.state, delta > 0 ? 1 : -1);
      touch.y = y;
    }
  }, { passive: true });
  var endTouch = function () { if (touch && touch.state) touch.state.input = null; touch = null; };
  window.addEventListener('touchend', endTouch, { passive: true });
  window.addEventListener('touchcancel', endTouch, { passive: true });
  window.addEventListener('resize', function () {
    mainState.y = main.scrollTop; mainState.height = main.clientHeight; mainState.extent = main.scrollHeight;
    windowState.y = window.scrollY || root.scrollTop || 0;
    windowState.height = root.clientHeight; windowState.extent = root.scrollHeight;
    clearInput(); endTouch();
    if (!shortLandscape()) app.classList.remove('topbar-hidden');
  }, { passive: true });
}

window.initTopbarAutoHide = initTopbarAutoHide;
