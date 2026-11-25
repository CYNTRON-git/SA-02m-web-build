#!/usr/bin/env node
// comment-mutation-proof-exempt: executes shipped navigation against scroll geometry and gestures; asserts class state after real callbacks, not source text.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../www/network_config/static/js/app/nav.js', import.meta.url), 'utf8');
function fixture(windowY = 0) {
  const classes = new Set(), handlers = {}, windowHandlers = {};
  let landscape = true, now = 0;
  const app = { classList: { contains: k => classes.has(k), add: k => classes.add(k), remove: k => classes.delete(k) } };
  const main = { scrollTop: 0, clientHeight: 660, scrollHeight: 1100, addEventListener: (k, fn) => { handlers[k] = fn; } };
  const root = { scrollTop: windowY, clientHeight: 720, scrollHeight: 1600 };
  const window = { scrollY: windowY, innerHeight: 720, matchMedia: () => ({ matches: landscape }),
    performance: { now: () => now }, getComputedStyle: node => ({ overflowY: node.overflowY || 'visible' }),
    addEventListener: (k, fn) => { windowHandlers[k] = fn; } };
  const context = vm.createContext({ window, document: {
    querySelector: k => k === '.app' ? app : k === '.main' ? main : null, documentElement: root,
  } });
  vm.runInContext(source, context, { filename: 'shipped-nav.js' });
  window.initTopbarAutoHide();
  return { hidden: () => classes.has('topbar-hidden'),
    scroll(y, height = main.clientHeight, extent = main.scrollHeight) {
      Object.assign(main, { scrollTop: y, clientHeight: height, scrollHeight: extent }); handlers.scroll();
    },
    windowScroll(y, height = root.clientHeight, extent = root.scrollHeight) {
      window.scrollY = y; Object.assign(root, { scrollTop: y, clientHeight: height, scrollHeight: extent }); windowHandlers.scroll();
    }, landscape: value => { landscape = value; },
    resize() { if (windowHandlers.resize) windowHandlers.resize(); },
    input(name, event = {}) { windowHandlers[name]({ target: main, defaultPrevented: false, ...event }); },
    main, root, advance(ms) { now += ms; },
  };
}

test('viewport growth and near-bottom clamping do not reverse a downward gesture', () => {
  const f = fixture(); f.scroll(388); assert(f.hidden());
  f.scroll(330, 718); assert(f.hidden());
});
test('viewport shrink does not reverse an upward gesture', () => {
  const f = fixture(); f.scroll(388); f.scroll(330); assert(!f.hidden());
  f.scroll(388, 602); assert(!f.hidden());
});
test('transition geometry rebases repeatedly and a later real upward gesture still reveals', () => {
  const f = fixture(); f.scroll(388);
  for (const [y, height] of [[378, 670], [358, 690], [330, 718], [330, 718]]) {
    f.scroll(y, height); assert(f.hidden());
  }
  f.scroll(310); assert(!f.hidden());
  for (const [y, height] of [[330, 700], [350, 680], [370, 660]]) {
    f.scroll(y, height); assert(!f.hidden());
  }
  f.scroll(390); assert(f.hidden());
});
test('actual down/up gestures and small jitter retain their behavior', () => {
  const f = fixture(); f.scroll(120); assert(f.hidden());
  f.scroll(118); assert(f.hidden());
  f.scroll(100); assert(!f.hidden());
});
test('leaving short landscape and reaching the top always reveal', () => {
  const f = fixture(); f.scroll(120); f.landscape(false); f.scroll(121, 900); assert(!f.hidden());
  f.landscape(true); f.scroll(121, 660); f.scroll(150); assert(f.hidden());
  f.scroll(0, 720); assert(!f.hidden());
});
test('window and main scroll positions have independent direction baselines', () => {
  const f = fixture(200); f.scroll(120); f.scroll(100); assert(!f.hidden());
  f.windowScroll(200); assert(!f.hidden(), 'an unchanged outer scroll position is not a downward gesture');
  f.windowScroll(220); assert(f.hidden());
  f.scroll(100); assert(f.hidden(), 'an unchanged main position is not an upward gesture');
  f.scroll(80); assert(!f.hidden());
});
test('window content clamping rebases without swallowing the next real gesture', () => {
  const f = fixture(); f.windowScroll(400); assert(f.hidden());
  f.windowScroll(340, 720, 1540); assert(f.hidden());
  f.windowScroll(320); assert(!f.hidden());
});
test('leaving landscape reveals on resize without needing a scroll event', () => {
  const f = fixture(); f.scroll(200); assert(f.hidden());
  f.landscape(false); f.resize(); assert(!f.hidden());
  f.landscape(true); f.resize(); assert(!f.hidden(), 'resize must not manufacture a downward gesture');
  f.scroll(230); assert(f.hidden());
});
test('fresh real input can reverse during geometry changes but is consumed once', () => {
  for (const [name, event] of [['wheel', { deltaY: -40 }], ['keydown', { key: 'ArrowUp' }]]) {
    const f = fixture(); f.scroll(200); f.input(name, event);
    f.scroll(160, 680); assert(!f.hidden(), name+' was swallowed during the transition');
    f.scroll(200, 660); assert(!f.hidden(), 'expansion anchoring is not a new gesture');
    f.scroll(230); assert(f.hidden(), 'ordinary next gesture is still handled');
  }
});
test('stale, zero-motion, opposite and small input cannot authorize later clamping', () => {
  const expired = fixture(); expired.scroll(200); expired.input('wheel', { deltaY: -40 });
  expired.advance(151); expired.scroll(160, 680); assert(expired.hidden());
  const zero = fixture(); zero.scroll(200); zero.input('wheel', { deltaY: -40 });
  zero.scroll(200); zero.scroll(160, 680); assert(zero.hidden());
  const opposite = fixture(); opposite.scroll(200); opposite.input('wheel', { deltaY: 40 });
  opposite.scroll(160, 680); assert(opposite.hidden());
  const small = fixture(); small.scroll(200); small.input('wheel', { deltaY: -2 });
  small.scroll(160, 680); assert(small.hidden(), 'small input is not reversal intent');
  const jitter = fixture(); jitter.scroll(200); jitter.input('wheel', { deltaY: -40 });
  jitter.scroll(198, 680); assert(jitter.hidden(), 'actual movement still must exceed four pixels');
  jitter.scroll(8, 690); assert(!jitter.hidden(), 'top boundary wins during moving geometry');
});
test('prevented and control-owned inputs do not drive the outer navigation', () => {
  for (const event of [{ defaultPrevented: true }, { ctrlKey: true }, { metaKey: true }, { altKey: true },
    ...['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].map(tagName => ({ target: { tagName } })),
    { target: { isContentEditable: true } },
    { target: { scrollHeight: 400, clientHeight: 100, overflowY: 'auto' } }]) {
    const f = fixture();
    if (event.target) event.target.parentElement = f.main;
    for (const kind of ['keydown', 'wheel']) {
      f.scroll(200, 660); f.scroll(240);
      f.input(kind, { key: 'ArrowUp', deltaY: -40, ...event });
      f.scroll(200, 680); assert(f.hidden(), kind+' '+JSON.stringify(event));
    }
  }
});
test('touch reversal uses the owning scrollport and cancellation clears intent', () => {
  const f = fixture(); f.scroll(200);
  f.input('touchstart', { touches: [{ clientY: 100 }] });
  f.input('touchmove', { touches: [{ clientY: 130 }] });
  f.scroll(160, 680); assert(!f.hidden());
  for (const finish of ['touchend', 'touchcancel', 'multitouch']) {
    const g = fixture(); g.scroll(200);
    g.input('touchstart', { touches: [{ clientY: 100 }] });
    g.input('touchmove', { touches: [{ clientY: 130 }] });
    if (finish === 'multitouch') g.input('touchmove', { touches: [{ clientY: 140 }, { clientY: 150 }] });
    else g.input(finish, { touches: [] });
    g.scroll(160, 680); assert(g.hidden(), finish+' left stale intent');
  }
});
test('keyboard and wheel units preserve the movement threshold and source isolation', () => {
  for (const key of ['ArrowUp', 'PageUp', 'Home', ' ']) {
    const f = fixture(); f.scroll(200);
    f.input('keydown', { key, shiftKey: key === ' ' });
    f.scroll(160, 680); assert(!f.hidden(), key+' up');
  }
  for (const key of ['ArrowDown', 'PageDown', 'End', ' ']) {
    const f = fixture(); f.scroll(200); f.scroll(160);
    f.input('keydown', { key }); f.scroll(200, 680); assert(f.hidden(), key+' down');
  }
  for (const deltaMode of [1, 2]) {
    const f = fixture(); f.scroll(200);
    f.input('wheel', { deltaY: -1, deltaMode }); f.scroll(160, 680); assert(!f.hidden());
  }
  const f = fixture(200); f.scroll(200);
  f.input('wheel', { target: f.root, deltaY: -40 });
  f.scroll(160, 680); assert(f.hidden(), 'outer input did not belong to main');
  f.windowScroll(160, 700); assert(!f.hidden(), 'outer input must use its own state');
});
