#!/usr/bin/env node
// Execute the shipped action region and its exact RU/EN dictionary values.
// comment-mutation-proof-exempt: behavioural harness; invokes shipped action functions and asserts POSTs, toasts, timers and RU/EN text against response scenarios.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = process.env.SYSTEM_ACTIONS_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const source = fs.readFileSync(path.join(root, 'www/network_config/static/js/app/services.js'), 'utf8');
const region = source.slice(source.indexOf('function doRestart()'), source.indexOf('function doLogout()'));
assert(region.includes('function doReboot()'), 'system action extraction failed');
const i18n = fs.readFileSync(path.join(root, 'www/network_config/static/js/i18n.js'), 'utf8');
const dictionary = i18n.slice(i18n.indexOf('const DICT = ') + 13, i18n.indexOf('\n  };', i18n.indexOf('const DICT = ')) + 4);
const dict = vm.runInNewContext('(' + dictionary + ')');
for (const key of region.matchAll(/uiT\('([^']+)'\)/g)) assert.equal(typeof dict[key[1]], 'string', 'missing EN: ' + key[1]);
let cases = 0;
for (const lang of ['ru', 'en']) {
  for (const action of ['doRestart', 'doReboot']) {
    for (const scenario of ['accepted', 'rejected', 'malformed', 'old-ok', 'busy', 'network']) {
      const toasts = [], calls = [], timers = [], confirms = [];
      const world = {
        uiT: s => lang === 'en' ? dict[s] : s,
        confirm: s => { confirms.push(s); return true; },
        toast: (...args) => toasts.push(args),
        withCsrfHeaders: h => ({ ...h, 'X-SA02M-CSRF': 'token' }),
        fetchStatus() {}, location: { reload() {} },
        setTimeout: (fn, delay) => timers.push(delay),
        fetch: async (url, opts) => {
          calls.push([url, opts]);
          if (scenario === 'network') throw new Error('network');
          return { ok: true, json: async () => {
            if (scenario === 'malformed') throw new Error('invalid JSON');
            if (scenario === 'accepted') return { ok: true, accepted: true };
            if (scenario === 'old-ok') return { ok: true };
            return { ok: false, error_code: scenario === 'busy' ? 'E_UPDATE_RUNNING' : 'admission_failed' };
          } };
        },
      };
      vm.createContext(world); vm.runInContext(region, world); world[action]();
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(calls.length, 1, 'no duplicate POST');
      assert.equal(calls[0][1].method, 'POST');
      assert.equal(calls[0][1].headers['X-SA02M-CSRF'], 'token');
      assert.equal(toasts.length, 1);
      if (scenario === 'accepted') {
        assert.equal(toasts[0][1], 'info');
        assert.match(toasts[0][0], lang === 'en' ? /accepted/ : /принят/);
        assert.deepEqual(timers, [action === 'doReboot' ? 180000 : 2000]);
      } else {
        assert.equal(timers.length, 0, 'unconfirmed action must not schedule a success reload');
        assert.equal(toasts[0][1], scenario === 'busy' && action === 'doReboot' ? 'info' : 'error');
      }
      if (action === 'doRestart') assert.match(confirms[0], /nginx.*fcgiwrap/);
      if (lang === 'en') assert.doesNotMatch(confirms[0], /[А-Яа-я]/);
      cases++;
    }
  }
}
console.log(`PASS ${cases} system-action UI cases, RU/EN, truthful admission and single POST`);
