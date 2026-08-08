/* SA-02m Web Interface -- MISC (log, IP validation, status-URL toast, time
   sync, web credentials, theme, hardware variant). Extracted from app.js (F10
   decomposition). Plain classic script sharing the global scope; original load
   order preserved. See index.html for the ordered <script> tags. */
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   LOG
   ══════════════════════════════════════════════════════════════════════════ */
function renderLogText(box, text) {
  box.innerHTML = text.split('\n').map(line => {
    if (/error|ошибк|failed|timeout|timed out|broken pipe|banner exchange|reset|refused/i.test(line)) {
      return '<span class="log-err">' + escHtml(line) + '</span>';
    }
    if (/warn|degrad|inactive|missing|unavailable/i.test(line)) {
      return '<span class="log-warn">' + escHtml(line) + '</span>';
    }
    if (/ok|успешн|applied|reboot|started|active|listening/i.test(line)) {
      return '<span class="log-ok">' + escHtml(line) + '</span>';
    }
    return escHtml(line);
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}

function loadLog() {
  const box = document.getElementById('log-box');
  if (!box) return;
  box.classList.remove('log-box-ssh-debug');
  fetch('cgi-bin/log.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => r.text())
    .then(t => renderLogText(box, t))
    .catch(() => { if (box) box.textContent = 'Не удалось загрузить журнал'; });
}

function loadSshDebug() {
  const box = document.getElementById('log-box');
  if (!box) return;
  box.classList.remove('log-box-ssh-debug');
  box.textContent = 'Загрузка SSH-диагностики (до ~2 мин)';
  fetch('cgi-bin/ssh_debug.cgi', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then(t => {
      const trimmed = (t || '').replace(/^\uFEFF/, '');
      if (/^\s*</.test(trimmed)) {
        box.classList.add('log-box-ssh-debug');
        box.replaceChildren();
        const frame = document.createElement('iframe');
        frame.className = 'ssh-debug-iframe';
        frame.title = 'SSH-диагностика';
        frame.setAttribute('sandbox', 'allow-same-origin');
        frame.srcdoc = trimmed;
        box.appendChild(frame);
      } else {
        renderLogText(box, t);
      }
    })
    .catch(() => {
      if (!box) return;
      box.classList.remove('log-box-ssh-debug');
      box.textContent = 'Не удалось загрузить SSH-диагностику';
    });
}

/* ══════════════════════════════════════════════════════════════════════════
   IP INPUT VALIDATION (blur)
   ══════════════════════════════════════════════════════════════════════════ */
function initValidation() {
  document.querySelectorAll('input[pattern]').forEach(inp => {
    inp.addEventListener('blur', () => {
      const v = inp.value.trim();
      if (v && !new RegExp('^' + inp.pattern + '$').test(v))
        inp.classList.add('invalid');
      else
        inp.classList.remove('invalid');
    });
    inp.addEventListener('input', () => inp.classList.remove('invalid'));
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   STATUS URL TOAST (after form redirect)
   ══════════════════════════════════════════════════════════════════════════ */
function handleUrlStatus() {
  const params = new URLSearchParams(window.location.search);
  const s = params.get('status');
  if (!s) return;
  const map = {
    applied:             ['Настройки применены', 'success'],
    applied_tz_failed:   ['Время применено; таймзона не изменилась', 'warn'],
    error_tz:            ['Ошибка: неверная таймзона', 'error'],
    error_time:          ['Ошибка: не удалось установить время', 'error'],
    services:            ['Службы перезапущены', 'success'],
    reboot:              ['Перезагрузка запущена…', 'info'],
  };
  const [msg, type] = map[s] || ['Статус: ' + s, 'info'];
  toast(msg, type);
  history.replaceState(null, '', window.location.pathname);
}

/* ══════════════════════════════════════════════════════════════════════════
   TIME — синхронизация с браузером (ПК)
   ══════════════════════════════════════════════════════════════════════════ */
function pad2(n) { return n < 10 ? '0' + n : String(n); }

function fmtLocalDateTimeForDevice(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
    pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/** @param {boolean} applyNow — если true, сразу POST в apply.cgi */
function syncTimeFromPC(applyNow) {
  const ft = document.getElementById('time-form');
  if (!ft) return;
  const tz = browserIanaTz();
  if (tz) {
    const sel = document.getElementById('f-tz');
    if (sel) {
      const known = Array.from(sel.options).some(o => o.value === tz);
      if (known) sel.value = tz;
      else {
        const o = document.createElement('option');
        o.value = tz;
        o.textContent = tz + ' (этот ПК)';
        sel.appendChild(o);
        sel.value = tz;
      }
    }
  }
  setVal('f-datetime', fmtLocalDateTimeForDevice(new Date()));
  if (!applyNow) {
    toast('Дата и время подставлены с этого ПК. При необходимости нажмите «Применить вручную».', 'success');
    return;
  }
  const data = new URLSearchParams(new FormData(ft));
  const btn = ft.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;
  fetch('cgi-bin/apply.cgi', {
    method: 'POST',
    body: data,
    redirect: 'manual',
    credentials: 'same-origin'
  })
    .then((r) => {
      if (!r.ok && r.status !== 302 && r.status !== 301) {
        toast('Ошибка сервера: ' + r.status, 'error');
        return;
      }
      const pr = parseApplyRedirect(r);
      if (pr.kind === 'error_time') {
        toast('Не удалось установить время с этого ПК.', 'error');
        return;
      }
      if (pr.kind === 'error_tz') {
        toast('Таймзона не применена.', 'error');
        return;
      }
      if (pr.kind === 'applied_tz_failed') {
        configLoaded = false;
        toast('Время синхронизировано; таймзона не изменилась.', 'warn', 6000);
        setTimeout(loadConfig, 400);
        return;
      }
      configLoaded = false;
      toast('Время синхронизировано с этим ПК', 'success');
      setTimeout(loadConfig, 400);
    })
    .catch(() => toast('Ошибка отправки', 'error'))
    .finally(() => { if (btn) btn.disabled = false; });
}

function exportInstallLog() {
  window.location.href = 'cgi-bin/log_export.cgi';
}

/* ══════════════════════════════════════════════════════════════════════════
   COMMAND LINE
   ══════════════════════════════════════════════════════════════════════════ */
const CMD_HISTORY_KEY = 'sa02m_cmd_history';
let cmdHistory = [];
let cmdHistoryPos = -1;
let cmdMode = 'web';
let cmdRootPassword = '';
let cmdAwaitPassword = false;
let cmdPendingCommand = '';

function loadCommandHistory() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(CMD_HISTORY_KEY) || '[]');
    cmdHistory = Array.isArray(parsed) ? parsed.filter(x => typeof x === 'string').slice(-30) : [];
  } catch (e) {
    cmdHistory = [];
  }
  cmdHistoryPos = cmdHistory.length;
}

function saveCommandHistory(cmd) {
  if (!cmd) return;
  cmdHistory = cmdHistory.filter(x => x !== cmd);
  cmdHistory.push(cmd);
  cmdHistory = cmdHistory.slice(-30);
  cmdHistoryPos = cmdHistory.length;
  try { sessionStorage.setItem(CMD_HISTORY_KEY, JSON.stringify(cmdHistory)); } catch (e) {}
}

function setCommandOutput(text) {
  const out = document.getElementById('cmd-output');
  if (!out) return;
  out.textContent = text;
  out.scrollTop = out.scrollHeight;
}

function clearCommandInput() {
  const input = document.getElementById('cmd-input');
  if (input) input.value = '';
}

function clearStoredRootPassword() {
  cmdRootPassword = '';
  const passInput = document.getElementById('cmd-root-password');
  if (passInput) passInput.value = '';
}

function setCommandMode(mode) {
  cmdMode = mode === 'root' ? 'root' : 'web';
  if (cmdMode !== 'root') {
    cmdAwaitPassword = false;
    cmdPendingCommand = '';
    clearStoredRootPassword();
  }
  updateCommandPasswordVisibility();
}

function updateCommandPasswordVisibility() {
  const wrap = document.getElementById('cmd-root-pass-wrap');
  if (!wrap) return;
  wrap.hidden = !(cmdAwaitPassword || (cmdMode === 'root' && !cmdRootPassword));
}

function beginRootPasswordPrompt(pendingCmd) {
  cmdMode = 'root';
  cmdAwaitPassword = true;
  cmdPendingCommand = pendingCmd || '';
  clearCommandInput();
  updateCommandPasswordVisibility();
  const passInput = document.getElementById('cmd-root-password');
  if (passInput) {
    passInput.value = '';
    passInput.focus();
  } else {
    document.getElementById('cmd-input')?.focus();
  }
  setCommandOutput(uiT(pendingCmd
    ? 'Для команды нужен пароль root. Введите пароль и нажмите Enter.'
    : 'Введите пароль root и нажмите Enter.'));
}

function isRootLoginCommand(cmd) {
  return /^(su|login)\s+root$/i.test(cmd) ||
    /^\s*sudo\s+(root|su(?:\s+-)?|su\s+root|-i|-s|--login)\s*$/i.test(cmd) ||
    /^\s*sudo\s*$/i.test(cmd);
}

function handleCommandBuiltin(cmd) {
  if (isRootLoginCommand(cmd)) {
    saveCommandHistory(cmd);
    clearCommandInput();
    if (cmdRootPassword) {
      setCommandMode('root');
      setCommandOutput(uiT('Режим root уже активен. Вводите команды напрямую, например: id -u -n'));
      return true;
    }
    beginRootPasswordPrompt('');
    return true;
  }
  if (/^(exit|logout|su\s+web)$/i.test(cmd)) {
    saveCommandHistory(cmd);
    setCommandMode('web');
    clearCommandInput();
    setCommandOutput(uiT('Режим web включён.'));
    return true;
  }
  return false;
}

function isInteractiveShellCommand(cmd) {
  // Kept for resolveExecCommand safety net; login-like sudo forms are builtins.
  return /^\s*((ba)?sh\s+-i)\s*$/i.test(cmd);
}

/** Resolve what to run under CGI. Never leave a bare sudo-flag like `-i` as the command. */
function resolveExecCommand(cmd, mode) {
  if (isRootLoginCommand(cmd) || isInteractiveShellCommand(cmd)) {
    return {
      mode: 'root',
      cmd: null,
      error: uiT('Используйте su root для входа, затем обычные команды.\nПример: id -u -n')
    };
  }
  const sudoMatch = cmd.match(/^\s*sudo(?:\s+(.*))?$/i);
  if (!sudoMatch) {
    return { mode, cmd, error: null };
  }
  const rest = (sudoMatch[1] || '').trim();
  // Never treat a bare username as a command (sudo root → "root: command not found").
  if (!rest || rest === 'root' || /^-/.test(rest.split(/\s+/, 1)[0] || '')) {
    // e.g. sudo -u www-data id — keep full sudo under root
    if (rest && rest !== 'root' && /^-/.test(rest)) {
      return { mode: 'root', cmd, error: null };
    }
    return {
      mode: 'root',
      cmd: null,
      error: uiT('Используйте su root для входа, затем обычные команды.\nПример: id -u -n')
    };
  }
  // e.g. sudo systemctl status x — run the command directly as root
  return { mode: 'root', cmd: rest, error: null };
}

function executeCommandLine(cmd, mode, rootPassword) {
  const resolved = resolveExecCommand(cmd, mode);
  const prompt = (resolved.mode === 'root' || mode === 'root') ? '# ' : '$ ';
  clearCommandInput();
  if (resolved.error) {
    saveCommandHistory(cmd);
    setCommandOutput(prompt + cmd + '\n' + resolved.error);
    return;
  }
  const execMode = resolved.mode;
  const execCmd = resolved.cmd;
  const started = new Date().toLocaleTimeString();
  setCommandOutput(prompt + cmd + '\n[' + started + '] ' + uiT('выполнение…'));
  fetch('cgi-bin/cmd_exec.cgi', {
    method: 'POST',
    body: new URLSearchParams({
      cmd: execCmd,
      mode: execMode,
      root_password: execMode === 'root' ? (rootPassword || '') : ''
    }),
    credentials: 'same-origin',
    cache: 'no-store'
  })
    .then(r => r.json())
    .then(j => {
      if (!j || !j.ok) {
        setCommandOutput(prompt + cmd + '\n' + uiT('Ошибка:') + ' ' + ((j && j.error) || 'server_error'));
        return;
      }
      if (execMode === 'root' && /root authentication failed/i.test(j.output || '')) {
        cmdRootPassword = '';
        cmdAwaitPassword = true;
        updateCommandPasswordVisibility();
        setCommandOutput(prompt + cmd + '\n' + uiT('Ошибка: неверный пароль root. Введите пароль снова.'));
        document.getElementById('cmd-root-password')?.focus();
        return;
      }
      saveCommandHistory(cmd);
      const tail = j.truncated ? '\n' + uiT('[output truncated to last 32768 bytes]') : '';
      setCommandOutput(prompt + cmd + '\n[mode ' + (j.mode || execMode) + ', exit ' + j.rc + ']\n' + (j.output || '') + tail);
    })
    .catch(err => setCommandOutput(prompt + cmd + '\n' + uiT('Ошибка запроса:') + ' ' + err.message));
}

function acceptRootPassword(password) {
  if (!password) {
    setCommandOutput(uiT('Введите пароль root.'));
    return;
  }
  cmdRootPassword = password;
  cmdAwaitPassword = false;
  cmdMode = 'root';
  clearCommandInput();
  const passInput = document.getElementById('cmd-root-password');
  if (passInput) passInput.value = '';
  updateCommandPasswordVisibility();
  const pending = cmdPendingCommand;
  cmdPendingCommand = '';
  if (pending) {
    executeCommandLine(pending, 'root', cmdRootPassword);
    return;
  }
  // Verify password once after su root.
  executeCommandLine('id -u -n', 'root', cmdRootPassword);
}

function runCommandLine() {
  const input = document.getElementById('cmd-input');
  const passInput = document.getElementById('cmd-root-password');
  if (!input) return;

  if (cmdAwaitPassword) {
    const fromPass = passInput && passInput.value ? passInput.value : '';
    const fromCmd = input.value;
    // Prefer dedicated password field; otherwise treat command-box text as password.
    const password = fromPass || fromCmd;
    acceptRootPassword(password);
    return;
  }

  const cmd = input.value.trim();
  if (!cmd) {
    setCommandOutput(uiT('Введите команду.'));
    return;
  }
  if (handleCommandBuiltin(cmd)) return;

  const wantsRoot = cmdMode === 'root' || /^\s*sudo\b/i.test(cmd);
  if (wantsRoot && !cmdRootPassword) {
    beginRootPasswordPrompt(cmd);
    return;
  }
  executeCommandLine(cmd, wantsRoot ? 'root' : cmdMode, cmdRootPassword);
}

function clearCommandLine() {
  clearCommandInput();
  cmdAwaitPassword = false;
  cmdPendingCommand = '';
  if (cmdMode !== 'root') clearStoredRootPassword();
  else {
    const passInput = document.getElementById('cmd-root-password');
    if (passInput) passInput.value = '';
  }
  updateCommandPasswordVisibility();
  setCommandOutput('');
}

function initCommandLine() {
  loadCommandHistory();
  setCommandMode('web');
  const input = document.getElementById('cmd-input');
  const passInput = document.getElementById('cmd-root-password');
  if (!input) return;
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      runCommandLine();
      return;
    }
    if (cmdAwaitPassword) return;
    if (ev.key === 'ArrowUp' && cmdHistory.length) {
      ev.preventDefault();
      cmdHistoryPos = Math.max(0, cmdHistoryPos - 1);
      input.value = cmdHistory[cmdHistoryPos] || '';
    }
    if (ev.key === 'ArrowDown' && cmdHistory.length) {
      ev.preventDefault();
      cmdHistoryPos = Math.min(cmdHistory.length, cmdHistoryPos + 1);
      input.value = cmdHistory[cmdHistoryPos] || '';
    }
  });
  if (passInput) {
    passInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        runCommandLine();
      }
    });
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   WEB CREDENTIALS
   ══════════════════════════════════════════════════════════════════════════ */
function initWebCredsForm() {
  const form = document.getElementById('web-creds-form');
  if (!form) return;
  form.addEventListener('submit', e => {
    e.preventDefault();
    const body = new URLSearchParams(new FormData(form));
    const btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    fetch('cgi-bin/web_creds.cgi', {
      method: 'POST',
      body,
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(r => r.json())
      .then(j => {
        if (j.ok) {
          toast('Сохранено. При следующем входе используйте новый логин и пароль.', 'success', 6500);
          const cur = document.getElementById('wc-cur');
          const p1 = document.getElementById('wc-p1');
          const p2 = document.getElementById('wc-p2');
          if (cur) cur.value = '';
          if (p1) p1.value = '';
          if (p2) p2.value = '';
        } else {
          const map = {
            unauthorized: 'Сессия истекла. Войдите снова.',
            wrong_password: 'Неверный текущий пароль',
            mismatch: 'Новый пароль и повтор не совпадают',
            bad_username: 'Недопустимый логин (латиница, цифры, . _ - , до 32 символов)',
            bad_password_len: 'Длина пароля 4–128 символов',
            bad_password_char: 'Пароль не может содержать символы \' $ ` ; | & < > ( )',
            no_password: 'Укажите новый пароль',
            no_user: 'Укажите логин',
            no_current: 'Укажите текущий пароль',
            no_auth_file: 'Файл учётных данных на устройстве недоступен',
            save_failed: 'Не удалось сохранить настройки',
          };
          toast(map[j.error] || ('Ошибка: ' + (j.error || 'unknown')), 'error');
        }
      })
      .catch(() => toast('Нет связи с сервером', 'error'))
      .finally(() => { if (btn) btn.disabled = false; });
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   THEME (кнопка в шапке, как gw-lwip)
   ══════════════════════════════════════════════════════════════════════════ */
const THEME_MOON_SVG = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 9A5.5 5.5 0 1 1 7 3a4 4 0 0 0 6 6z"/></svg>';
const THEME_SUN_SVG = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><line x1="8" y1="1" x2="8" y2="2.5"/><line x1="8" y1="13.5" x2="8" y2="15"/><line x1="1" y1="8" x2="2.5" y2="8"/><line x1="13.5" y1="8" x2="15" y2="8"/><line x1="3.1" y1="3.1" x2="4.1" y2="4.1"/><line x1="11.9" y1="11.9" x2="12.9" y2="12.9"/><line x1="12.9" y1="3.1" x2="11.9" y2="4.1"/><line x1="4.1" y1="11.9" x2="3.1" y2="12.9"/></svg>';

function themeTargetLabel(isDark) {
  const ru = isDark ? 'Светлая' : 'Тёмная';
  return window.sa02mI18n ? window.sa02mI18n.t(ru) : ru;
}

function updateThemeBtn() {
  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const icon = document.getElementById('theme-toggle-icon');
  const label = document.getElementById('theme-toggle-label');
  const btn = document.getElementById('theme-toggle');
  if (icon) icon.innerHTML = isDark ? THEME_MOON_SVG : THEME_SUN_SVG;
  if (label) label.textContent = themeTargetLabel(isDark);
  if (btn) {
    const en = window.sa02mI18n && window.sa02mI18n.lang === 'en';
    btn.title = en ? 'Toggle theme' : 'Переключить тему';
    btn.setAttribute('aria-label', en ? 'Toggle theme' : 'Переключить тему');
  }
}

function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
    theme = 'dark';
  }
  // Keep the browser status-bar / safe-area tint in sync with the page
  // background (Operator 2026-07-19: iPhone top/bottom must match the theme).
  var tc = document.getElementById('theme-color-meta');
  if (tc) tc.setAttribute('content', theme === 'light' ? '#ececf3' : '#161618');
  try { localStorage.setItem('sa02m-theme', theme); } catch (_) {}
  updateThemeBtn();
}

function initThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  let stored = 'dark';
  try { stored = localStorage.getItem('sa02m-theme') || 'dark'; } catch (_) {}
  applyTheme(stored === 'light' ? 'light' : 'dark');
  btn.addEventListener('click', () => {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    applyTheme(isLight ? 'dark' : 'light');
  });
}

window.updateThemeBtn = updateThemeBtn;

/* ══════════════════════════════════════════════════════════════════════════
   HARDWARE VARIANT
   ══════════════════════════════════════════════════════════════════════════ */
const VARIANT_STATUS_AUTO_CLEAR_MS = 3000;
let _variantStatusTimer = null;
let _variantStatusGen = 0;

function cancelVariantStatusAutoClear() {
  if (_variantStatusTimer) {
    clearTimeout(_variantStatusTimer);
    _variantStatusTimer = null;
  }
  _variantStatusGen += 1;
}

function scheduleVariantStatusAutoClear(el) {
  cancelVariantStatusAutoClear();
  const gen = _variantStatusGen;
  _variantStatusTimer = setTimeout(function () {
    _variantStatusTimer = null;
    if (_variantStatusGen !== gen || !el) return;
    el.textContent = '';
  }, VARIANT_STATUS_AUTO_CLEAR_MS);
}

function variantDisplayLabel(variant) {
  const map = {
    'sa02m-1eth': 'СА-02м',
    'sa02m-2eth': 'СА-02м-2',
  };
  return map[variant] || String(variant || '').replace(/^sa02m-/i, 'СА-02м-');
}

function formatComPortCount(n) {
  n = Math.abs(parseInt(n, 10) || 0);
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return n + ' COM-порт';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return n + ' COM-порта';
  return n + ' COM-портов';
}

async function loadVariant() {
  try {
    const r = await fetch('cgi-bin/variant.cgi');
    if (!r.ok) return;
    const d = await r.json();
    const sel = document.getElementById('hw-variant-select');
    if (sel) sel.value = d.variant || 'sa02m-1eth';
    applyVariantVisibility(d.variant || 'sa02m-1eth');
  } catch (_) {}
}

async function applyVariant() {
  const sel = document.getElementById('hw-variant-select');
  const status = document.getElementById('variant-status');
  if (!sel || !status) return;
  const variant = sel.value;
  cancelVariantStatusAutoClear();
  status.textContent = 'Применяю';
  status.style.color = 'var(--text-sec)';
  try {
    const r = await fetch('cgi-bin/variant.cgi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'variant=' + encodeURIComponent(variant)
    });
    const d = await r.json();
    if (d.ok) {
      status.textContent = '\u2713 Применено: ' + variantDisplayLabel(d.variant) + ', ' + formatComPortCount(d.serial_count);
      status.style.color = 'var(--green, #4caf50)';
      scheduleVariantStatusAutoClear(status);
      await loadVariant();
    } else {
      status.textContent = '\u2717 Ошибка: ' + (d.error || 'неизвестно');
      status.style.color = 'var(--red, #f44336)';
    }
  } catch (e) {
    status.textContent = '\u2717 ' + e.message;
    status.style.color = 'var(--red, #f44336)';
  }
}

