/* SA-02m — BSH Home Connect card (вкладка «Управление»). Talks only to
   cgi-bin/sa02m_homeconnect_api.cgi (JSON per action:
   docs/contracts/home-connect.md §9). READ-ONLY integration: the card links
   an account, shows its appliances and the API budget; it sends nothing to an
   appliance. Polls every 10 s ONLY while «Управление» is the active tab and
   the page is visible (window.homeconnectTabInit/Destroy from app.js
   switchTab), plus a short chained fast-poll after an action. The sign-in
   code, its link and QR exist in the DOM only while the status says
   `awaiting_user` with a valid, unexpired `link`; they are removed on any
   other state, on expiry and on tab leave. The QR is computed here (byte
   mode, level M, versions 1–10) — the CGI returns no matrix and the page
   loads no library. */

(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

const HC_API = 'cgi-bin/sa02m_homeconnect_api.cgi';
const HC_POLL_MS = 10000;
// GET = dispatch only; a POST may add the privileged nudge — both stay under
// nginx's 20 s read timeout (same budget as the HomeKit CGI).
const HC_GET_TIMEOUT_MS = 10000;
const HC_POST_TIMEOUT_MS = 21000;
// Contract §9 `invalid_client_id` grammar; empty = clear.
const HC_CLIENT_ID_RE = /^[A-Za-z0-9_-]{8,128}$/;
// Contract §8: user_code grammar and the only sign-in hosts accepted.
const HC_USER_CODE_RE = /^[A-Za-z0-9-]{4,32}$/;
const HC_VERIFY_DOMAINS = ['home-connect.com', 'home-connect.cn', 'singlekey-id.com'];
const HC_QR_QUIET = 4;
const SVG_NS = 'http://www.w3.org/2000/svg';

// state → [RU label, badge kind]. Unknown ⇒ «н/д». `rate_limited` gets its
// «до HH:MM» appended from rate_limited_until.
const HOMECONNECT_STATE_MAP = {
  not_installed: ['не установлен', 'unk'],
  disabled: ['выключен', 'unk'],
  missing_deps: ['нет компонентов', 'err'],
  missing_client_id: ['нет Client ID', 'warn'],
  unlinked: ['не подключён', 'unk'],
  awaiting_user: ['ждёт входа', 'warn'],
  link_expired: ['код истёк', 'warn'],
  connecting: ['подключение…', 'warn'],
  connected: ['подключён', 'ok'],
  rate_limited: ['лимит API', 'warn'],
  offline: ['нет связи с облаком', 'err'],
  token_revoked: ['доступ отозван', 'err'],
  error: ['ошибка', 'err'],
};

// `{"ok":false,"error":…}` codes (dispatch §9 + the CGI) → RU.
const HOMECONNECT_ERROR_MAP = {
  invalid_client_id: 'Недопустимый Client ID',
  not_enabled: 'Сначала включите Home Connect',
  missing_client_id: 'Сначала сохраните Client ID',
  already_linked: 'Аккаунт уже подключён',
  not_installed: 'Home Connect не установлен — нужна полная установка (install.sh)',
  conf_write_failed: 'Не удалось сохранить настройки',
  homeconnect_api_failed: 'Нет ответа от платы',
  payload_too_large: 'Ошибка запроса API Home Connect',
  invalid_json: 'Ошибка запроса API Home Connect',
  not_found: 'Ошибка запроса API Home Connect',
  method_not_allowed: 'Ошибка запроса API Home Connect',
};

// ── QR encoder (ISO/IEC 18004: byte mode, EC level M, versions 1–10) ───────
// Per version (index = version): EC codewords per block, EC block count.
const QR_ECC_PER_BLOCK_M = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
const QR_BLOCKS_M = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
const QR_MAX_VERSION = 10;

function qrRawModules(ver) {
  let n = (16 * ver + 128) * ver + 64;
  if (ver >= 2) {
    const align = Math.floor(ver / 7) + 2;
    n -= (25 * align - 10) * align - 55;
    if (ver >= 7) n -= 36;
  }
  return n;
}

function qrDataCodewords(ver) {
  return Math.floor(qrRawModules(ver) / 8) - QR_ECC_PER_BLOCK_M[ver] * QR_BLOCKS_M[ver];
}

function qrAlignPositions(ver) {
  if (ver === 1) return [];
  const size = ver * 4 + 17;
  const num = Math.floor(ver / 7) + 2;
  const step = Math.ceil((ver * 4 + 4) / (num * 2 - 2)) * 2;
  const out = [6];
  for (let pos = size - 7; out.length < num; pos -= step) out.splice(1, 0, pos);
  return out;
}

function qrGfMul(x, y) {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
}

function qrRsDivisor(degree) {
  const res = new Array(degree).fill(0);
  res[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      res[j] = qrGfMul(res[j], root);
      if (j + 1 < degree) res[j] ^= res[j + 1];
    }
    root = qrGfMul(root, 0x02);
  }
  return res;
}

function qrRsRemainder(data, divisor) {
  const res = new Array(divisor.length).fill(0);
  data.forEach(function (b) {
    const factor = b ^ res.shift();
    res.push(0);
    for (let i = 0; i < divisor.length; i++) res[i] ^= qrGfMul(divisor[i], factor);
  });
  return res;
}

function qrUtf8(text) {
  return Array.from(new TextEncoder().encode(String(text)));
}

// Data codewords (mode, count, bytes, terminator, pad) + interleaved EC.
function qrCodewords(bytes, ver) {
  const bits = [];
  const put = function (val, len) { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
  const cap = qrDataCodewords(ver) * 8;
  put(4, 4);
  put(bytes.length, ver <= 9 ? 8 : 16);
  bytes.forEach(function (b) { put(b, 8); });
  put(0, Math.min(4, cap - bits.length));
  put(0, (8 - bits.length % 8) % 8);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    let v = 0;
    for (let j = 0; j < 8; j++) v = (v << 1) | bits[i + j];
    data.push(v);
  }
  for (let pad = 0xec; data.length < cap / 8; pad ^= 0xec ^ 0x11) data.push(pad);

  const nBlocks = QR_BLOCKS_M[ver];
  const eccLen = QR_ECC_PER_BLOCK_M[ver];
  const raw = Math.floor(qrRawModules(ver) / 8);
  const nShort = nBlocks - raw % nBlocks;
  const shortLen = Math.floor(raw / nBlocks);
  const div = qrRsDivisor(eccLen);
  const blocks = [];
  for (let i = 0, k = 0; i < nBlocks; i++) {
    const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1));
    k += dat.length;
    const ecc = qrRsRemainder(dat, div);
    if (i < nShort) dat.push(0);
    blocks.push(dat.concat(ecc));
  }
  const out = [];
  for (let i = 0; i < blocks[0].length; i++) {
    for (let j = 0; j < blocks.length; j++) {
      if (i !== shortLen - eccLen || j >= nShort) out.push(blocks[j][i]);
    }
  }
  return out;
}

function qrMaskBit(mask, x, y) {
  switch (mask) {
    case 0: return (x + y) % 2 === 0;
    case 1: return y % 2 === 0;
    case 2: return x % 3 === 0;
    case 3: return (x + y) % 3 === 0;
    case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
    case 5: return (x * y) % 2 + (x * y) % 3 === 0;
    case 6: return ((x * y) % 2 + (x * y) % 3) % 2 === 0;
    default: return ((x + y) % 2 + (x * y) % 3) % 2 === 0;
  }
}

// Function patterns, then the zig-zag data placement — the mask-free base.
function qrBase(ver, codewords) {
  const size = ver * 4 + 17;
  const m = [];
  const fn = [];
  for (let y = 0; y < size; y++) { m.push(new Array(size).fill(false)); fn.push(new Array(size).fill(false)); }
  const set = function (x, y, dark) { m[y][x] = dark; fn[y][x] = true; };
  for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
  [[3, 3], [size - 4, 3], [3, size - 4]].forEach(function (c) {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const x = c[0] + dx; const y = c[1] + dy;
        if (x < 0 || y < 0 || x >= size || y >= size) continue;
        const d = Math.max(Math.abs(dx), Math.abs(dy));
        set(x, y, d !== 2 && d !== 4);
      }
    }
  });
  const al = qrAlignPositions(ver);
  const last = al.length - 1;
  for (let i = 0; i <= last; i++) {
    for (let j = 0; j <= last; j++) {
      if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) continue;
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) set(al[i] + dx, al[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    }
  }
  qrDrawFormat(m, fn, 0);   // reserve the format areas (real bits per mask later)
  if (ver >= 7) {
    let rem = ver;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const bits = (ver << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const dark = ((bits >>> i) & 1) === 1;
      const a = size - 11 + i % 3; const b = Math.floor(i / 3);
      set(a, b, dark);
      set(b, a, dark);
    }
  }
  let i = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        const up = ((right + 1) & 2) === 0;
        const y = up ? size - 1 - vert : vert;
        if (!fn[y][x] && i < codewords.length * 8) {
          m[y][x] = ((codewords[i >>> 3] >>> (7 - (i & 7))) & 1) === 1;
          i++;
        }
      }
    }
  }
  return { m: m, fn: fn, size: size };
}

// Format information: EC level M (bits 00) + mask, BCH(15,5), XOR 0x5412.
function qrDrawFormat(m, fn, mask) {
  const size = m.length;
  const data = mask;   // (M = 0b00) << 3 | mask
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
  const bits = ((data << 10) | rem) ^ 0x5412;
  const bit = function (i) { return ((bits >>> i) & 1) === 1; };
  const set = function (x, y, dark) { m[y][x] = dark; fn[y][x] = true; };
  for (let i = 0; i <= 5; i++) set(8, i, bit(i));
  set(8, 7, bit(6));
  set(8, 8, bit(7));
  set(7, 8, bit(8));
  for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
  for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
  for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
  set(8, size - 8, true);   // the dark module
}

// ISO penalty rules N1–N4 (runs, 2×2 blocks, finder look-alikes, balance).
function qrPenalty(m) {
  const size = m.length;
  let p = 0;
  let dark = 0;
  const line = function (get) {
    let run = 1;
    for (let i = 1; i <= size; i++) {
      if (i < size && get(i) === get(i - 1)) { run++; continue; }
      if (run >= 5) p += 3 + (run - 5);
      run = 1;
    }
    // 1:1:3:1:1 finder look-alike (1011101) with 4 light modules before or
    // after it; outside the symbol counts as light. Each side scores.
    const at = function (i) { return i >= 0 && i < size && get(i); };
    const light4 = function (from) { return !at(from) && !at(from + 1) && !at(from + 2) && !at(from + 3); };
    for (let j = 0; j + 7 <= size; j++) {
      if (!(at(j) && !at(j + 1) && at(j + 2) && at(j + 3) && at(j + 4) && !at(j + 5) && at(j + 6))) continue;
      if (light4(j - 4)) p += 40;
      if (light4(j + 7)) p += 40;
    }
  };
  for (let y = 0; y < size; y++) line(function (x) { return m[y][x]; });
  for (let x = 0; x < size; x++) line(function (y) { return m[y][x]; });
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if (m[y][x]) dark++;
      if (x < size - 1 && y < size - 1) {
        const c = m[y][x];
        if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) p += 3;
      }
    }
  }
  const total = size * size;
  const k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
  return p + Math.max(0, k) * 10;
}

// text → rows of '0'/'1' (dark = '1'), or null when it does not fit v10-M.
// `forceMask` (0–7) is for cross-checks only; the card lets the penalty pick.
function qrMatrix(text, forceMask) {
  const bytes = qrUtf8(text);
  let ver = 1;
  while (ver <= QR_MAX_VERSION && 4 + (ver <= 9 ? 8 : 16) + bytes.length * 8 > qrDataCodewords(ver) * 8) ver++;
  if (ver > QR_MAX_VERSION) return null;
  const base = qrBase(ver, qrCodewords(bytes, ver));
  let best = null;
  let bestScore = Infinity;
  const masks = typeof forceMask === 'number' ? [forceMask] : [0, 1, 2, 3, 4, 5, 6, 7];
  masks.forEach(function (mask) {
    const m = base.m.map(function (r) { return r.slice(); });
    const fn = base.fn.map(function (r) { return r.slice(); });
    for (let y = 0; y < base.size; y++) {
      for (let x = 0; x < base.size; x++) if (!fn[y][x] && qrMaskBit(mask, x, y)) m[y][x] = !m[y][x];
    }
    qrDrawFormat(m, fn, mask);
    const score = qrPenalty(m);
    if (score < bestScore) { bestScore = score; best = m; }
  });
  return best.map(function (r) { return r.map(function (v) { return v ? '1' : '0'; }).join(''); });
}

// Horizontal runs of dark modules become one rect each.
function hcDrawQr(svg, rows) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const n = rows.length;
  const size = n + 2 * HC_QR_QUIET;
  svg.setAttribute('viewBox', '0 0 ' + size + ' ' + size);
  const bg = document.createElementNS(SVG_NS, 'rect');
  bg.setAttribute('class', 'homekit-qr-bg');
  bg.setAttribute('x', '0');
  bg.setAttribute('y', '0');
  bg.setAttribute('width', String(size));
  bg.setAttribute('height', String(size));
  svg.appendChild(bg);
  let drawn = 0;
  for (let y = 0; y < n; y++) {
    const row = rows[y];
    let x = 0;
    while (x < n) {
      if (row.charAt(x) !== '1') { x++; continue; }
      let end = x;
      while (end < n && row.charAt(end) === '1') end++;
      const r = document.createElementNS(SVG_NS, 'rect');
      r.setAttribute('class', 'homekit-qr-m');
      r.setAttribute('x', String(x + HC_QR_QUIET));
      r.setAttribute('y', String(y + HC_QR_QUIET));
      r.setAttribute('width', String(end - x));
      r.setAttribute('height', '1');
      svg.appendChild(r);
      drawn++;
      x = end;
    }
  }
  return drawn;
}

// ── Helpers ────────────────────────────────────────────────────────────────
function hcBadgeHtml(text, kind) {
  const cls = kind === 'ok' ? 'badge-ok' : kind === 'warn' ? 'badge-warn' : kind === 'err' ? 'badge-err' : 'badge-unk';
  return '<span class="badge ' + cls + '">' + escHtml(String(text)) + '</span>';
}

function hcSetBadge(text, kind) {
  const el = $('homeconnect-state');
  if (el) el.innerHTML = hcBadgeHtml(text, kind);
}

function hcSetText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function hcSetHidden(id, hidden) {
  const el = $(id);
  if (el) el.hidden = !!hidden;
}

// The standing card line — renderer-owned, shown for as long as the state
// lasts. ok: true/false tints it, null is a neutral hint.
function hcSetCardMsg(text, ok) {
  const msg = $('homeconnect-msg');
  if (!msg) return;
  if (!text) {
    msg.hidden = true;
    msg.textContent = '';
    msg.className = 'cloud-msg';
    return;
  }
  msg.hidden = false;
  msg.textContent = text;
  msg.className = 'cloud-msg' + (ok === null ? '' : (ok ? ' is-ok' : ' is-err'));
}

function hcNotice(text, ok) {
  if (!text) return;
  if (typeof cardNotice === 'function') cardNotice(text, ok);
  else if (typeof toast === 'function') toast(text, ok === false ? 'error' : 'success', 5000);
}

function hcErrorText(d) {
  const code = d && typeof d.error === 'string' ? d.error : '';
  return uiT(HOMECONNECT_ERROR_MAP[code] || 'Ошибка запроса API Home Connect');
}

function hcPad2(n) { return (n < 10 ? '0' : '') + n; }

// Epoch seconds → local «HH:MM» ('' when not a sane timestamp).
function hcClock(ts) {
  if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '';
  const dt = new Date(ts * 1000);
  return hcPad2(dt.getHours()) + ':' + hcPad2(dt.getMinutes());
}

function hcNowS() { return Date.now() / 1000; }

// Contract §8: https only, on the BSH sign-in domains (and their subdomains).
function hcSafeVerifyUrl(s) {
  if (typeof s !== 'string' || !s || s.length > 1024) return '';
  let u;
  try { u = new URL(s); } catch (e) { return ''; }
  if (u.protocol !== 'https:' || u.username || u.password) return '';
  const host = u.hostname.toLowerCase();
  const ok = HC_VERIFY_DOMAINS.some(function (d) { return host === d || host.slice(-(d.length + 1)) === '.' + d; });
  return ok ? u.href : '';
}

// The link object, only when the status is awaiting_user and every field
// passes its grammar and the code has not expired; else null.
function hcValidLink(d) {
  if (!d || d.state !== 'awaiting_user' || !d.link || typeof d.link !== 'object') return null;
  const l = d.link;
  if (typeof l.user_code !== 'string' || !HC_USER_CODE_RE.test(l.user_code)) return null;
  if (typeof l.expires_at !== 'number' || !(l.expires_at > hcNowS())) return null;
  const page = hcSafeVerifyUrl(l.verification_uri_complete) || hcSafeVerifyUrl(l.verification_uri);
  if (!page) return null;
  return { code: l.user_code, url: page, expires: l.expires_at };
}

// ── Transport ──────────────────────────────────────────────────────────────
function hcFetch(opt, ms) {
  if (typeof fetchWithTimeout === 'function') return fetchWithTimeout(HC_API, opt, ms);
  return fetch(HC_API, opt);
}

function hcGet() {
  return hcFetch({ method: 'GET', credentials: 'same-origin', cache: 'no-store' }, HC_GET_TIMEOUT_MS)
    .then(function (r) { return r.json(); });
}

function hcPost(body) {
  return hcFetch({
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'same-origin',
    cache: 'no-store',
    body: JSON.stringify(body),
  }, HC_POST_TIMEOUT_MS).then(function (r) { return r.json(); });
}

// ── State ──────────────────────────────────────────────────────────────────
let _hcLast = null;          // last rendered status (null = none / no answer)
let _hcLastFailed = false;   // the last poll got no usable answer
let _hcBusy = false;         // an action is in flight — every control disabled
let _hcSeq = 0;              // newest request wins; an older answer never renders
let _hcInflight = null;
let _hcTabActive = false;
let _hcPoll = null;
let _hcFastPoll = null;
let _hcFastPollGen = 0;
let _hcCountdown = null;
let _hcCodeKey = '';         // code|url currently drawn ('' = none)
let _hcCidOpened = false;    // the Client ID editor was opened once for missing_client_id

// ── Sign-in code box ───────────────────────────────────────────────────────
// Removes the code, link and QR from the DOM (not merely hides them).
function hcClearCode() {
  _hcCodeKey = '';
  if (_hcCountdown) { clearInterval(_hcCountdown); _hcCountdown = null; }
  const svg = $('homeconnect-qr');
  if (svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute('hidden', '');
  }
  hcSetText('homeconnect-code', '');
  hcSetText('homeconnect-countdown', '');
  const a = $('homeconnect-verify-link');
  if (a) { a.removeAttribute('href'); a.removeAttribute('title'); }
  hcSetHidden('homeconnect-code-box', true);
}

function hcTickCountdown() {
  const d = _hcLast;
  const link = hcValidLink(d);
  if (!link) {
    // Expired in the browser before the daemon said so: take the code down
    // at once and ask the board (it answers link_expired).
    hcClearCode();
    if (d) hcRender(d);
    if (_hcTabActive) hcRefresh();
    return;
  }
  const left = Math.max(0, Math.floor(link.expires - hcNowS()));
  hcSetText('homeconnect-countdown', uiT('Код действует ещё') + ' ' + Math.floor(left / 60) + ':' + hcPad2(left % 60));
}

function hcShowCode(link) {
  const key = link.code + '|' + link.url;
  if (key !== _hcCodeKey) {
    hcSetText('homeconnect-code', link.code);
    const a = $('homeconnect-verify-link');
    if (a) { a.href = link.url; a.title = link.url; }
    const svg = $('homeconnect-qr');
    const rows = qrMatrix(link.url);
    // SVGElement has no `hidden` IDL property: toggle the attribute itself.
    if (svg && rows && hcDrawQr(svg, rows) > 0) svg.removeAttribute('hidden');
    else if (svg) { while (svg.firstChild) svg.removeChild(svg.firstChild); svg.setAttribute('hidden', ''); }
    _hcCodeKey = key;
  }
  hcSetHidden('homeconnect-code-box', false);
  if (!_hcCountdown) _hcCountdown = setInterval(hcTickCountdown, 1000);
  hcTickCountdown();
}

// ── Render ─────────────────────────────────────────────────────────────────
function hcSetButtonsDisabled() {
  const d = _hcLast;
  const blocked = _hcBusy || _hcLastFailed || !d;
  const installed = !!(d && d.state !== 'not_installed');
  const en = $('homeconnect-btn-enable');
  if (en) en.disabled = blocked || !installed;
  const link = $('homeconnect-btn-link');
  if (link) link.disabled = blocked || !(d && d.enabled && d.client_id_source);
  const unlink = $('homeconnect-btn-unlink');
  if (unlink) unlink.disabled = blocked;
  ['homeconnect-btn-client', 'homeconnect-client-in'].forEach(function (id) {
    const el = $(id);
    if (el) el.disabled = blocked || !installed;
  });
}

function hcSetBusy(on) {
  _hcBusy = !!on;
  hcSetButtonsDisabled();
}

function hcFillClientId(d) {
  const det = $('homeconnect-cid');
  const inp = $('homeconnect-client-in');
  if (!det) return;
  det.hidden = !d || d.state === 'not_installed';
  if (det.hidden) return;
  if (d.state === 'missing_client_id' && !_hcCidOpened) { det.open = true; _hcCidOpened = true; }
  if (inp && document.activeElement !== inp) {
    inp.value = typeof d.client_id === 'string' ? d.client_id : '';
    inp.placeholder = d.vendor_preset && !d.client_id ? uiT('используется ID CYNTRON') : '';
  }
}

function hcRenderAppliances(d) {
  const list = $('homeconnect-apps-list');
  if (!list) return;
  while (list.firstChild) list.removeChild(list.firstChild);
  const rows = d && d.linked && Array.isArray(d.appliance_list) ? d.appliance_list : [];
  list.hidden = rows.length === 0;
  rows.forEach(function (a) {
    if (!a || typeof a !== 'object') return;
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'homeconnect-app-name';
    name.textContent = String(a.name || a.device_id || '?');
    const meta = document.createElement('span');
    meta.className = 'homeconnect-app-meta';
    meta.textContent = [a.type, a.brand].filter(function (s) { return typeof s === 'string' && s; }).join(' · ');
    const on = document.createElement('span');
    on.className = 'badge ' + (a.connected === true ? 'badge-ok' : 'badge-unk');
    on.textContent = uiT(a.connected === true ? 'в сети' : 'не в сети');
    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(on);
    list.appendChild(li);
  });
}

// The standing explanation for the current state (null = none).
function hcStateLine(d) {
  const st = d.state;
  const r = d.reason || '';
  const until = hcClock(d.rate_limited_until);
  switch (st) {
    case 'not_installed': return [uiT('Нужна полная установка (install.sh)'), null];
    case 'missing_deps':
      // The conf exists but the daemon cannot read it (its read ACL is gone).
      if (r === 'conf_unreadable') return [uiT('Нет доступа к настройкам'), false];
      return [uiT('Нужна полная установка (install.sh)'), false];
    case 'missing_client_id': return [uiT('Введите Client ID'), null];
    case 'unlinked':
      if (r === 'access_denied') return [uiT('Вход отклонён'), false];
      if (r === 'client_id_rejected') return [uiT('Client ID не принят'), false];
      if (r === 'token_store_corrupt') return [uiT('Данные входа повреждены — подключите заново'), false];
      return [uiT('Нажмите «Подключить»'), null];
    case 'link_expired': return [uiT('Нажмите «Подключить» ещё раз'), false];
    case 'rate_limited':
      if (r === 'daily_limit') return [uiT('Суточный лимит исчерпан') + (until ? ' — ' + uiT('до') + ' ' + until : ''), false];
      return [uiT('Облако попросило подождать') + (until ? ' — ' + uiT('до') + ' ' + until : ''), false];
    // Also the bench case (G6): TLS completes from an RU IP, then BSH never answers.
    case 'offline': return [uiT('BSH недоступно из этой сети'), false];
    case 'token_revoked': return [uiT('Подключите аккаунт заново'), false];
    case 'error':
      if (r === 'status_stale') return [uiT('Статус устарел'), false];
      if (r === 'token_store_insecure') return [uiT('Небезопасный файл входа — журнал sa02m-homeconnect'), false];
      return [uiT('Журнал: sa02m-homeconnect'), false];
    default: break;
  }
  if (r === 'stream_down') return [uiT('Нет потока событий — данные могут устареть'), false];
  if (r === 'budget_local_reached') return [uiT('Опрос на паузе до конца суток (UTC)'), null];
  return null;
}

// No usable answer (timeout, 5xx, a non-status body): values «н/д», ONE
// standing line, no sign-in code, every action refused.
function hcRenderUnavailable() {
  _hcLastFailed = true;
  hcSetBadge(uiT('н/д'), 'unk');
  hcSetText('homeconnect-apps', '—');
  hcSetText('homeconnect-budget', '—');
  hcSetText('homeconnect-stream', '—');
  hcClearCode();
  hcSetCardMsg(uiT('Нет ответа от платы'), false);
  if (!_hcLast) {
    const en = $('homeconnect-btn-enable');
    if (en) { en.textContent = uiT('Включить'); en.className = 'btn btn-sm btn-primary'; }
  }
  hcSetButtonsDisabled();
}

function hcRender(d) {
  if (!$('homeconnect-card')) return;
  if (!d || d.ok !== true || typeof d.state !== 'string') {
    hcRenderUnavailable();
    return;
  }
  _hcLast = d;
  _hcLastFailed = false;
  const st = d.state;
  const installed = st !== 'not_installed';

  const stale = st === 'error' && d.reason === 'status_stale';
  let entry = stale ? ['не отвечает', 'err'] : (HOMECONNECT_STATE_MAP[st] || ['н/д', 'unk']);
  let label = uiT(entry[0]);
  if (st === 'rate_limited') {
    const until = hcClock(d.rate_limited_until);
    if (until) label += ' ' + uiT('до') + ' ' + until;
  }
  hcSetBadge(label, entry[1]);

  const linked = d.linked === true;
  const nApps = typeof d.appliances === 'number' ? d.appliances : 0;
  const nOn = typeof d.appliances_connected === 'number' ? d.appliances_connected : 0;
  hcSetText('homeconnect-apps', linked ? String(nApps) + (nApps ? ' · ' + uiT('в сети') + ' ' + nOn : '') : '—');
  const b = d.budget && typeof d.budget === 'object' ? d.budget : null;
  hcSetText('homeconnect-budget', b && typeof b.used === 'number' && typeof b.limit === 'number'
    ? b.used + ' ' + uiT('из') + ' ' + b.limit : '—');
  const stream = d.stream === 'up' ? 'открыт' : d.stream === 'down' ? 'прерван' : '';
  hcSetText('homeconnect-stream', stream ? uiT(stream) : '—');

  hcRenderAppliances(d);
  hcFillClientId(d);

  // Sign-in: «Подключить» while enabled and not linked; the code only in
  // awaiting_user with a valid, unexpired link.
  const link = hcValidLink(d);
  if (link) hcShowCode(link);
  else hcClearCode();
  const canLink = installed && !!d.enabled && !linked && st !== 'awaiting_user' && st !== 'missing_deps'
    && st !== 'disabled' && st !== 'error';
  hcSetHidden('homeconnect-link-row', !canLink);

  const unlink = $('homeconnect-btn-unlink');
  if (unlink) unlink.hidden = !installed || !(linked || st === 'token_revoked' || st === 'awaiting_user');
  const en = $('homeconnect-btn-enable');
  if (en) {
    const on = !!d.enabled;
    en.dataset.action = on ? 'disable' : 'enable';
    en.className = 'btn btn-sm ' + (on ? 'btn-danger' : 'btn-primary');
    en.textContent = uiT(on ? 'Выключить' : 'Включить');
  }

  const line = hcStateLine(d);
  if (line) hcSetCardMsg(line[0], line[1]);
  else hcSetCardMsg('', true);
  hcSetButtonsDisabled();
}

// ── Poll ───────────────────────────────────────────────────────────────────
// Returns the rendered status, or null when nothing was rendered (no answer,
// unauthorized, or a newer request overtook this one).
function hcRefresh() {
  if (_hcInflight) return _hcInflight;
  const seq = ++_hcSeq;
  _hcInflight = hcGet()
    .then(function (d) {
      if (seq !== _hcSeq) return null;
      // The session died: the fetch guard / status poll own the login flow.
      if (d && d.error === 'unauthorized') return null;
      hcRender(d);
      return d && d.ok === true ? d : null;
    }, function () {
      if (seq === _hcSeq) hcRender(null);
      return null;
    })
    .finally(function () { _hcInflight = null; });
  return _hcInflight;
}

function hcPageVisible() {
  return document.visibilityState !== 'hidden';
}

function hcStopPolls() {
  if (_hcPoll) { clearInterval(_hcPoll); _hcPoll = null; }
  if (_hcFastPoll) { clearTimeout(_hcFastPoll); _hcFastPoll = null; }
  _hcFastPollGen += 1;   // retire an in-flight fast-poll chain
}

function hcStartPoll() {
  if (!_hcTabActive || !hcPageVisible() || !$('homeconnect-card')) return;
  if (!_hcPoll && !_hcFastPoll) _hcPoll = setInterval(hcRefresh, HC_POLL_MS);
}

// After an action: 1 s ticks for ≤ 20 s, CHAINED (each tick waits for its own
// answer — the CGI forks python), ending early once `settled(d)` holds. The
// base poll is parked meanwhile.
function hcFastPoll(settled) {
  const until = Date.now() + 20000;
  _hcFastPollGen += 1;
  const gen = _hcFastPollGen;
  if (_hcFastPoll) clearTimeout(_hcFastPoll);
  if (_hcPoll) { clearInterval(_hcPoll); _hcPoll = null; }
  const stop = function () {
    _hcFastPoll = null;
    hcStartPoll();
  };
  const tick = async function () {
    _hcFastPoll = null;
    let done = false;
    try {
      const d = await hcRefresh();
      done = !!d && settled(d);
    } catch (e) {
      /* transport hiccup — keep the window running */
    }
    if (gen !== _hcFastPollGen) return;
    if (done || Date.now() >= until) { stop(); return; }
    _hcFastPoll = setTimeout(tick, 1000);
  };
  _hcFastPoll = setTimeout(tick, 700);
}

// ── Actions ────────────────────────────────────────────────────────────────
// One mutation at a time; the answer carries the merged status, rendered at
// once. Returns the response (null on transport failure / unauthorized).
async function hcMutate(body, okText) {
  if (_hcBusy) return null;
  hcSetBusy(true);
  ++_hcSeq;   // a poll already in flight must not paint over this answer
  const seq = _hcSeq;
  let d = null;
  try {
    d = await hcPost(body);
  } catch (e) {
    hcNotice(uiT('Нет ответа от платы'), false);
    hcSetBusy(false);
    return null;
  }
  hcSetBusy(false);
  if (!d || d.error === 'unauthorized') return null;
  // E_CSRF is handled (refresh/retry/logout) by the global fetch guard.
  if (d.error_code === 'E_CSRF') return null;
  if (d.ok !== true) {
    hcNotice(hcErrorText(d), false);
    if (d.error === 'not_installed' && seq === _hcSeq) hcRender({ ok: true, state: 'not_installed', enabled: false });
    return d;
  }
  if (d.trigger === 'failed' || d.trigger === 'timeout') {
    hcNotice(uiT('Сохранено, служба не ответила'), false);
  } else if (okText) {
    hcNotice(okText, true);
  }
  if (d.status && seq === _hcSeq) hcRender(d.status);
  return d;
}

async function homeconnectToggle() {
  const btn = $('homeconnect-btn-enable');
  const action = btn && btn.dataset.action === 'disable' ? 'disable' : 'enable';
  if (action === 'disable') hcClearCode();
  const d = await hcMutate({ action: action }, uiT('Сохранено'));
  if (d && d.ok) {
    hcFastPoll(action === 'disable'
      ? function (s) { return s.state === 'disabled'; }
      : function (s) { return s.state !== 'connecting' && s.state !== 'disabled'; });
  }
}

async function homeconnectSaveClientId() {
  const inp = $('homeconnect-client-in');
  if (!inp || !_hcLast) return;
  const id = String(inp.value || '').trim();
  if (id && !HC_CLIENT_ID_RE.test(id)) { hcNotice(hcErrorText({ error: 'invalid_client_id' }), false); return; }
  const d = await hcMutate({ action: 'set_client_id', client_id: id }, uiT('Сохранено'));
  if (d && d.ok) {
    const det = $('homeconnect-cid');
    if (det && id) det.open = false;
    hcFastPoll(function (s) { return s.state !== 'connecting'; });
  }
}

async function homeconnectLink() {
  const d = await hcMutate({ action: 'link' }, null);
  if (d && d.ok) hcFastPoll(function (s) { return s.state === 'awaiting_user' || s.state === 'connected'; });
}

async function homeconnectUnlink() {
  if (!window.confirm(uiT('Отключить аккаунт? Приборы пропадут из MQTT до нового входа.'))) return;
  hcClearCode();
  const d = await hcMutate({ action: 'unlink' }, uiT('Аккаунт отключён'));
  if (d && d.ok) hcFastPoll(function (s) { return s.linked !== true; });
}

// ── Tab lifecycle (app.js switchTab) ────────────────────────────────────────
function homeconnectTabInit() {
  if (!$('homeconnect-card')) return;
  _hcTabActive = true;
  if (!hcPageVisible()) return;
  hcRefresh();
  hcStartPoll();
}

// Leaving the tab stops the poll and takes the sign-in code out of the DOM.
function homeconnectTabDestroy() {
  _hcTabActive = false;
  hcStopPolls();
  ++_hcSeq;   // an answer still in flight must not re-draw the code
  hcClearCode();
}

function hcVisibilityChanged() {
  if (!_hcTabActive) return;
  if (!hcPageVisible()) {
    hcStopPolls();
    return;
  }
  hcRefresh();
  hcStartPoll();
}

// Language switch (i18n.js updateControl): re-render the composed strings.
function refreshHomeconnectI18n() {
  if (_hcLastFailed || !_hcLast) {
    if (_hcLastFailed) hcRenderUnavailable();
    return;
  }
  if (_hcTabActive) hcRender(_hcLast);
}

function hcInit() {
  if (!$('homeconnect-card')) return;
  document.addEventListener('visibilitychange', hcVisibilityChanged);
  // A deep link straight to #system switched tabs before this file's init ran.
  const pane = $('tab-system');
  if (pane && pane.classList.contains('active')) homeconnectTabInit();
}

// Only HTML onclick handlers and app.js / i18n.js hooks need a global handle;
// homeconnectQrMatrix is read by the card smoke to check the drawn code.
window.homeconnectTabInit = homeconnectTabInit;
window.homeconnectTabDestroy = homeconnectTabDestroy;
window.homeconnectToggle = homeconnectToggle;
window.homeconnectSaveClientId = homeconnectSaveClientId;
window.homeconnectLink = homeconnectLink;
window.homeconnectUnlink = homeconnectUnlink;
window.homeconnectRefresh = hcRefresh;
window.homeconnectQrMatrix = qrMatrix;
window.refreshHomeconnectI18n = refreshHomeconnectI18n;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', hcInit);
} else {
  hcInit();
}

})();
