#!/usr/bin/env node
// comment-mutation-proof-exempt: executes shipped navigation against scroll geometry and gestures; asserts class state after real callbacks, not source text.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../www/network_config/static/js/app/nav.js', import.meta.url), 'utf8');
function fixture(windowY = 0) {
  const classes = new Set(), handlers = {}, windowHandlers = {};
  let landscape = true;
  const app = { classList: { contains: k => classes.has(k), add: k => classes.add(k), remove: k => classes.delete(k) } };
  const main = { scrollTop: 0, clientHeight: 660, scrollHeight: 1100, addEventListener: (k, fn) => { handlers[k] = fn; } };
  const root = { scrollTop: windowY, clientHeight: 720, scrollHeight: 1600 };
  const window = { scrollY: windowY, innerHeight: 720, matchMedia: () => ({ matches: landscape }),
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
