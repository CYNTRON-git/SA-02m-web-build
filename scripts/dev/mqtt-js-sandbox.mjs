// Test helper, not a registry row: asserts nothing itself.
/* ═══════════════════════════════════════════════════════════════════════════
   Shared loader for the mqtt.js unit tests (test-mqtt-scan-type.mjs): the
   SHIPPED static/js/mqtt.js evaluated whole in a vm context over a minimal
   fake DOM, so a test drives the real functions (beginScanResults,
   appendScanDevice, addDeviceFromScan, …) — never a re-typed copy. mqtt.js is
   an ES module only for its `import` statements; each is replaced by stub
   bindings, the rest runs as a classic script (its top level is declarations
   plus `window.*` shims — no DOM access at load).
   Stdlib-only (node:vm), same posture as the other js-unit rows.
   ═══════════════════════════════════════════════════════════════════════════ */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
export const ROOT = join(HERE, '..', '..');
export const MQTT_JS = join(ROOT, 'www', 'network_config', 'static', 'js', 'mqtt.js');
export const I18N_JS = join(ROOT, 'www', 'network_config', 'static', 'js', 'i18n.js');

class Text {
  constructor(t) { this.nodeType = 3; this._t = String(t); this.parent = null; }
  get textContent() { return this._t; }
}

export class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.className = '';
    this.disabled = false;
    this.hidden = false;
    this.parent = null;
    this.style = {};
    this.dataset = {};
    this._text = null;
    this._value = undefined;
    this.selected = false;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === 'disabled') this.disabled = true;
    if (k === 'hidden') this.hidden = true;
  }
  removeAttribute(k) {
    delete this.attrs[k];
    if (k === 'disabled') this.disabled = false;
    if (k === 'hidden') this.hidden = false;
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(typeof c === 'string' ? new Text(c) : c)); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatch(t) {
    (this.listeners[t] || []).forEach((f) => f({ target: this, preventDefault() {}, stopPropagation() {} }));
    if (t === 'click' && typeof this.onclick === 'function') this.onclick({ target: this });
  }
  set innerHTML(v) { this.children = []; this._text = v ? String(v) : null; }
  get innerHTML() { return this._text || ''; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text || '';
  }
  replaceWith(n) {
    const p = this.parent;
    const i = p.children.indexOf(this);
    p.children[i] = n;
    n.parent = p;
  }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((c) => c !== this);
  }
  get classList() {
    const self = this;
    return {
      add(c) { if (!self.className.split(' ').includes(c)) self.className = (self.className + ' ' + c).trim(); },
      remove(c) { self.className = self.className.split(' ').filter((x) => x !== c).join(' '); },
      toggle(c, on) { (on === undefined ? !this.contains(c) : on) ? this.add(c) : this.remove(c); },
      contains(c) { return self.className.split(' ').includes(c); },
    };
  }
  /** Descendant elements, depth-first. */
  all() {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (c instanceof El) { out.push(c); walk(c); } });
    walk(this);
    return out;
  }
  querySelectorAll(sel) {
    const tag = String(sel).trim().toUpperCase();
    return this.all().filter((e) => e.tagName === tag);
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  get options() { return this.children.filter((c) => c instanceof El && c.tagName === 'OPTION'); }
  get value() {
    if (this.tagName === 'SELECT') {
      const opts = this.options;
      const s = opts.find((o) => o.selected) || opts[0];
      return s ? s.value : '';
    }
    if (this.tagName === 'OPTION') return 'value' in this.attrs ? this.attrs.value : this.textContent;
    if (this._value !== undefined) return this._value;
    return 'value' in this.attrs ? this.attrs.value : '';
  }
  set value(v) {
    if (this.tagName === 'SELECT') {
      this.options.forEach((o) => { o.selected = o.value === String(v); });
      return;
    }
    this._value = String(v);
  }
  get selectedIndex() { return this.options.findIndex((o) => o.selected); }
}

/** A fresh sandbox with mqtt.js loaded. `ids` = {id: El} for getElementById.
 *  `lang: 'en'` first loads the SHIPPED i18n.js and switches it to English, so
 *  uiT() goes through the real DICT; the default leaves window.sa02mI18n null
 *  (uiT returns the Russian source). */
export function loadMqttJs({ ids = {}, lang = 'ru' } = {}) {
  let src = readFileSync(MQTT_JS, 'utf8');
  const importRe = /^import\s*\{([^}]*)\}\s*from\s*['"][^'"]+['"];?\s*$/gm;
  if (!importRe.test(src)) throw new Error('mqtt.js: expected an import line to stub');
  // `a as b` binds b. A multi-line import keeps its line count, so a vm stack
  // trace still names the shipped file's line.
  src = src.replace(importRe, (m, names) =>
    names.split(',').map((n) => n.trim()).filter(Boolean)
      .map((n) => `const ${n.split(/\s+as\s+/).pop()} = {};`).join(' ')
      + '\n'.repeat((m.match(/\n/g) || []).length));
  const registry = Object.assign({}, ids);
  const document = {
    createElement: (t) => new El(t),
    createTextNode: (t) => new Text(t),
    getElementById: (id) => registry[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    // 'loading' keeps i18n.js from binding its DOM walker at load.
    readyState: 'loading',
    body: new El('body'),
  };
  const window = { sa02mI18n: null, addEventListener() {}, location: { href: '' } };
  const ctx = {
    window, document, console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0,
    fetch: () => Promise.reject(new Error('no network in tests')),
    confirm: () => true, alert() {}, prompt: () => null,
    URLSearchParams, Promise, Math, JSON, Number, String, Object, Array, Set, Map,
  };
  vm.createContext(ctx);
  if (lang === 'en') {
    vm.runInContext(readFileSync(I18N_JS, 'utf8'), ctx, { filename: 'i18n.js' });
    if (!window.sa02mI18n || typeof window.sa02mI18n.setLang !== 'function') {
      throw new Error('i18n.js: window.sa02mI18n.setLang not defined');
    }
    window.sa02mI18n.setLang('en');
  }
  vm.runInContext(src, ctx, { filename: 'mqtt.js' });
  return {
    ctx,
    ids: registry,
    run: (code) => vm.runInContext(code, ctx),
    el: (tag) => new El(tag),
  };
}
