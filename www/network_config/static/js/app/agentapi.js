/* SA-02m — card «API для ИИ-агентов». Talks only to cgi-bin/sa02m_agent_api.cgi.
   The daemon can be off; this CGI is what turns it on. The secret is shown once. */
(function () {
'use strict';

function uiT(s) {
  return window.sa02mI18n ? window.sa02mI18n.t(String(s)) : String(s);
}

function $(id) { return document.getElementById(id); }

var API = 'cgi-bin/sa02m_agent_api.cgi';
var secret = '';
var busy = false;

function say(text) {
  var el = $('agentapi-msg');
  if (el) el.textContent = text || '';
}

function post(body) {
  return fetch(API, {
    method: 'POST',
    headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'same-origin',
    cache: 'no-store',
    body: JSON.stringify(body)
  }).then(function (r) { return r.json(); });
}

function scopes() {
  var out = [];
  ['read', 'control', 'config', 'admin'].forEach(function (name) {
    var box = $('agentapi-scope-' + name);
    if (box && box.checked) out.push(name);
  });
  return out.length ? out : ['read'];
}

function fmtDate(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleDateString(); } catch (e) { return ''; }
}

function render(data) {
  var on = data && data.active === 'active';
  var state = $('agentapi-state');
  if (state) {
    var text = on ? uiT('Служба API включена') : uiT('Служба API выключена');
    if (data && data.nginx_routed === false) text += '. ' + uiT('Маршрут /api/v1 в nginx не настроен');
    state.textContent = text;
  }
  var toggle = $('agentapi-toggle');
  if (toggle) {
    toggle.textContent = on ? uiT('Выключить') : uiT('Включить');
    toggle.setAttribute('data-on', on ? '1' : '0');
  }
  var box = $('agentapi-rows');
  if (!box) return;
  box.textContent = '';
  var rows = (data && data.tokens) || [];
  rows.forEach(function (row) {
    var line = document.createElement('div');
    line.className = 'agentapi-row';
    var label = document.createElement('span');
    var parts = [row.name || row.id || '', (row.scopes || []).join(',')];
    if (row.root_capable) parts.push('root');
    if (row.expires) parts.push(uiT('до') + ' ' + fmtDate(row.expires));
    label.textContent = parts.filter(Boolean).join(' · ');
    label.title = row.id || '';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm';
    btn.textContent = uiT('Отозвать');
    btn.addEventListener('click', function () { revoke(row.id); });
    line.appendChild(label);
    line.appendChild(btn);
    box.appendChild(line);
  });
}

function load() {
  return fetch(API, { method: 'GET', credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data && data.ok) render(data);
      else say(uiT('Не удалось выполнить действие'));
    })
    .catch(function () { say(uiT('Не удалось выполнить действие')); });
}

function toggle() {
  if (busy) return;
  var on = $('agentapi-toggle') && $('agentapi-toggle').getAttribute('data-on') === '1';
  busy = true;
  post({ action: on ? 'disable' : 'enable' }).then(function (data) {
    busy = false;
    if (!data || data.ok === false) say(uiT('Не удалось выполнить действие'));
    return load();
  }).catch(function () { busy = false; say(uiT('Не удалось выполнить действие')); });
}

function create() {
  if (busy) return;
  var root = $('agentapi-root') && $('agentapi-root').checked;
  var body = {
    action: 'create',
    name: ($('agentapi-name') && $('agentapi-name').value) || 'agent',
    days: Number($('agentapi-days') && $('agentapi-days').value) || 0,
    scopes: scopes(),
    root_capable: !!root
  };
  if (root) body.root_password = ($('agentapi-pass') && $('agentapi-pass').value) || '';
  busy = true;
  post(body).then(function (data) {
    busy = false;
    if (!data || !data.token) {
      var why = data && data.error;
      if (why === 'root_auth') say(uiT('Неверный пароль root'));
      else if (why === 'E_CSRF' || (data && data.error_code === 'E_CSRF')) say(uiT('Сессия устарела, обновите страницу'));
      else say(uiT('Не удалось выполнить действие'));
      return;
    }
    if ($('agentapi-pass')) $('agentapi-pass').value = '';
    secret = data.token;
    var box = $('agentapi-secret');
    if (box) {
      box.hidden = false;
      box.textContent = uiT('Секрет показан один раз. Сохраните его.') + ' ' + secret;
    }
    var copy = $('agentapi-copy');
    if (copy) copy.disabled = false;
    return load();
  }).catch(function () { busy = false; say(uiT('Не удалось выполнить действие')); });
}

function revoke(id) {
  if (busy || !id) return;
  busy = true;
  post({ action: 'revoke', id: id }).then(function (data) {
    busy = false;
    say(data && data.ok ? uiT('Токен отозван') : uiT('Не удалось выполнить действие'));
    return load();
  }).catch(function () { busy = false; say(uiT('Не удалось выполнить действие')); });
}

function copyConfig() {
  if (!secret) {
    say(uiT('Сначала создайте токен'));
    return;
  }
  var url = (location.origin || '') + '/' + 'mcp';
  var text = JSON.stringify({
    mcpServers: {
      sa02m: { url: url, headers: { 'X-SA02M-Token': secret } }
    }
  }, null, 2);
  var done = function () { say(uiT('Конфиг MCP скопирован')); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(function () { say(text); });
  } else {
    say(text);
  }
}

function onRoot() {
  var wrap = $('agentapi-pass-wrap');
  if (wrap) wrap.hidden = !($('agentapi-root') && $('agentapi-root').checked);
}

window.agentApiTabInit = function () { load(); };
window.agentApiTabDestroy = function () {};

function bind() {
  var toggleBtn = $('agentapi-toggle');
  var createBtn = $('agentapi-create');
  var copyBtn = $('agentapi-copy');
  var root = $('agentapi-root');
  if (toggleBtn) toggleBtn.addEventListener('click', toggle);
  if (createBtn) createBtn.addEventListener('click', create);
  if (copyBtn) copyBtn.addEventListener('click', copyConfig);
  if (root) root.addEventListener('change', onRoot);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
else bind();
})();
