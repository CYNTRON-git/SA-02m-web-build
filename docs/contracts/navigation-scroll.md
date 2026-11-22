# Navigation scroll and topbar

In landscape viewports up to 720px high, a downward scroll beyond 48px hides the
topbar; an upward scroll of more than 4px reveals it. Small direction jitter does
not toggle it. Reaching the first 8px or leaving that media query reveals it.

The shipped CSS transition changes scrollport geometry and may clamp scrollTop
near the content bottom. That layout event is not a new user gesture: nav.js
rebases the position when the viewport height or content extent changes. The
next gesture with stable geometry is handled normally. Main and window scrolling
keep independent position/geometry baselines. No polling, additional timer,
observer or animation override is used.

`test-nav-scroll.mjs` executes the shipped callbacks for clamping, repeated
transition samples, actual direction, jitter, top boundary and separate
scrollports. `test-nav-scroll-browser.mjs` loads the actual CSS/nav in Chromium,
uses wheel input near the bottom and checks 45 stable frames after the normal
transition, then a genuine upward gesture and ordinary button/text input. It
covers 1280×720, 1024×600, 812×375 and tall 1280×900. The existing MQTT template
browser test remains unchanged and checks the complete application integration.
