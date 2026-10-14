/* MTDx62-MB presence sensor settings window — the flasher tab's «mtd» kind
   (plan mtd-settings-window, release 1.0.7.3; contract
   docs/contracts/mtd-presence.md).

   Same modal, same entry point (double-click on the scan row), same leased
   daemon session as the MR-02m window. Three tabs:
     «Сведения»  model, firmware/radar versions, status code + meaning, live
                 presence / illuminance / distance, the active-reporting flag
                 (warning when on), report interval and MAC code — READ-ONLY
                 (holding 12/13: plan F5);
     «Настройки» every setting the daemon lists in the snapshot, with ITS
                 min/max/step (the template's range — nothing typed here);
     «Сеть»      address and baud; parity N only (the bridge opens every
                 RS-485 handle 8N1 — F1); a baud the bridge could not serve on
                 this COM is not offered (F2, flasher/bridge-relink.js).

   What lives here and what does not:
     * NO register map and NO range: labels, units, ranges, the status text
       arrive in the snapshot from the daemon (mtd_config.py, which reads the
       template). The daemon is the authority; the browser's checks are a
       convenience.
     * Settings ride the EXISTING POST /device_config/holding {reg, value} with
       the raw word, one request per changed field (the MR idiom); the link
       change rides POST /device_config/network.
     * After a link change the window moves the bridge entry to the line the
       daemon REPORTS (sa02mBridgeRelink.relink, `restart:false`; the bridge
       reloads it when the port lease is restored on close). While the change
       and the relink run, Close and F5 are guarded (host.setLinkBusy — F4).
     * Live values are patched IN PLACE; a focused or edited field
       (dataset.mtdDirty) is never overwritten by the 1 s poll.

   Classic script with ONE global, `window.sa02mMtdWindow.create(host)`; the
   host is flasher.js's injection of the modal plumbing (the flasher/led.js
   seam), plus `bridge` (= window.sa02mBridgeRelink) and `setLinkBusy`.
   Verified by scripts/dev/test-flasher-mtd-window.mjs (row
   js-unit-flasher-mtd-window). */
(function () {
  'use strict';

  const MTD_TABS = ['mtd_info', 'mtd_settings', 'mtd_network'];
  // The scan row's signature (mtd_config.MTD_MODEL — the test parses that file).
  const MTD_MODEL = 'MTDx62-MB';
  // The bridge entry the relink moves: `type: template`, this template name.
  const MTD_TEMPLATE = 'mtdx62-mb';
  const MTD_UNITS = { m: 'м', s: 'с', lux: 'лк' };
  const MTD_HINTS = {
    11: '0 — функция выключена. При ненулевом пороге присутствие выдаётся, только если освещённость ниже порога и человек обнаружен.',
  };

  function create(host) {
    const esc = host.escapeHtml;
    const el = function (id) { return host.configModalEl(id); };
    // Per-device state kept across renders: the bridge's view of this COM
    // (F2) and the outcome of the last link change (shown until the window
    // closes). Both are tied to the device object the window edits.
    let bridgeView = null;   // {dev, state: 'loading'|'ok'|'error', rule, error}
    let linkNote = null;     // {dev, text, kind: 'error'|'info'}

    function block(snap) { return (snap && snap.mtd) || {}; }
    function isNum(v) { return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v)); }
    function dev() { return host.currentConfigDevice(); }

    function tabs() {
      return [
        { id: 'mtd_info', label: 'Сведения' },
        { id: 'mtd_settings', label: 'Настройки' },
        { id: 'mtd_network', label: 'Сеть' },
      ];
    }
    function title() { return 'Датчик присутствия MTD'; }
    function kicker() { return 'Настройка датчика присутствия'; }

    function lineText(address, line) {
      const l = line || {};
      return `${isNum(address) ? address : '—'} адр. · ${l.baudrate || '—'} ${l.parity || 'N'}${l.stopbits || 1}`;
    }

    // ── readouts (one function per id, shared by render and patch) ──────────
    function presenceText(live) { return isNum(live.presence) ? (Number(live.presence) > 0 ? 'Есть' : 'Нет') : '—'; }
    function luxText(live) { return isNum(live.illuminance_lux) ? Number(live.illuminance_lux).toFixed(1) : '—'; }
    function distText(live) {
      // The radar reports no target while nobody is present: its distance
      // register then holds a stale value, so it is not shown.
      if (!isNum(live.target_distance_m) || !(Number(live.presence) > 0)) return '—';
      return Number(live.target_distance_m).toFixed(2);
    }
    function repText(rep) { return rep ? (rep.on ? 'Включена' : 'Выключена') : '—'; }

    function readouts(snap) {
      const info = snap.info || {};
      const m = block(snap);
      const live = m.live || {};
      return {
        'cfg-mtd-model': info.model || MTD_MODEL,
        'cfg-mtd-app': info.app_version || '—',
        'cfg-mtd-radar': info.radar_version || '—',
        'cfg-mtd-status-code': isNum(live.device_status) ? String(live.device_status) : '—',
        'cfg-mtd-status-text': live.device_status_text || '',
        'cfg-mtd-presence': presenceText(live),
        'cfg-mtd-lux': luxText(live),
        'cfg-mtd-dist': distText(live),
        'cfg-mtd-rep': repText(m.active_reporting),
        'cfg-mtd-interval': isNum(m.report_interval_s) ? String(m.report_interval_s) : '—',
        'cfg-mtd-mac': isNum(m.mac_code) ? String(m.mac_code) : '—',
        'cfg-mtd-net-current': lineText(info.address, info.line),
      };
    }

    function silent(snap) { return block(snap).answered === false; }
    function repOn(snap) { const r = block(snap).active_reporting; return !!(r && r.on); }

    function renderInfo(snap) {
      const r = readouts(snap);
      const v = function (id) { return esc(r[id]); };
      return `
        <div class="flasher-config-banner is-error" id="cfg-mtd-silent" ${silent(snap) ? '' : 'hidden'}>Датчик не отвечает</div>
        <div class="flasher-config-grid">
          <section class="flasher-config-card">
            <h4>Устройство</h4>
            <dl class="flasher-config-kv">
              <div><dt>Тип</dt><dd>Датчик присутствия</dd></div>
              <div><dt>Модель</dt><dd class="mono" id="cfg-mtd-model">${v('cfg-mtd-model')}</dd></div>
              <div><dt>Версия ПО</dt><dd class="mono" id="cfg-mtd-app">${v('cfg-mtd-app')}</dd></div>
              <div><dt>Версия радара</dt><dd class="mono" id="cfg-mtd-radar">${v('cfg-mtd-radar')}</dd></div>
              <div><dt>Состояние</dt><dd><span class="mono" id="cfg-mtd-status-code">${v('cfg-mtd-status-code')}</span> <span id="cfg-mtd-status-text">${v('cfg-mtd-status-text')}</span></dd></div>
            </dl>
          </section>
          <section class="flasher-config-card">
            <h4>Показания</h4>
            <dl class="flasher-config-kv">
              <div><dt>Присутствие</dt><dd id="cfg-mtd-presence">${v('cfg-mtd-presence')}</dd></div>
              <div><dt>Освещённость</dt><dd><span class="mono" id="cfg-mtd-lux">${v('cfg-mtd-lux')}</span> <span>лк</span></dd></div>
              <div><dt>Дистанция</dt><dd><span class="mono" id="cfg-mtd-dist">${v('cfg-mtd-dist')}</span> <span>м</span></dd></div>
            </dl>
          </section>
          <section class="flasher-config-card">
            <h4>Активная передача</h4>
            <dl class="flasher-config-kv">
              <div><dt>Передача</dt><dd id="cfg-mtd-rep">${v('cfg-mtd-rep')}</dd></div>
              <div><dt>Интервал, с</dt><dd class="mono" id="cfg-mtd-interval">${v('cfg-mtd-interval')}</dd></div>
              <div><dt>MAC-код</dt><dd class="mono" id="cfg-mtd-mac">${v('cfg-mtd-mac')}</dd></div>
            </dl>
            <div class="flasher-config-banner is-error" id="cfg-mtd-rep-warn" ${repOn(snap) ? '' : 'hidden'}>Датчик сам шлёт кадры в линию и мешает остальным устройствам. Отключите передачу пультом производителя и перезапустите питание датчика.</div>
            <div class="flasher-config-note">Панель эти значения не записывает.</div>
          </section>
        </div>
      `;
    }

    function unitText(units) { return MTD_UNITS[units] || ''; }

    function renderField(f) {
      const id = 'cfg-mtd-f-' + Number(f.reg);
      const unit = unitText(f.units);
      const range = f.writable && isNum(f.min) && isNum(f.max) ? `${f.min} … ${f.max}` : '';
      const hint = MTD_HINTS[Number(f.reg)];
      return `
        <label for="${id}"><span>${esc(f.title || f.name || '')}</span>${range ? ` <span class="mono">${esc(range)}</span>` : ''}${unit ? ` <span>${esc(unit)}</span>` : ''}</label>
        <input id="${id}" type="number" data-mtd-reg="${Number(f.reg)}"${f.writable ? ` min="${esc(f.min)}" max="${esc(f.max)}" step="${esc(f.step)}"` : ' disabled'} value="${esc(f.value == null ? '' : f.value)}" />
        ${hint ? `<div class="flasher-config-note">${esc(hint)}</div>` : ''}`;
    }

    function renderSettings(snap) {
      const m = block(snap);
      const fields = m.settings || [];
      const anyWritable = fields.some(function (f) { return f.writable; });
      return `
        <div class="flasher-config-grid">
          <section class="flasher-config-card">
            <h4>Настройки датчика</h4>
            ${m.template_ok === false ? '<div class="flasher-config-banner is-error" id="cfg-mtd-tpl-missing">Шаблон датчика не найден — запись недоступна</div>' : ''}
            <div class="flasher-config-form">${fields.map(renderField).join('')}</div>
            <div class="flasher-config-actions">
              <button class="btn btn-primary" type="button" id="cfg-mtd-apply-btn"${anyWritable ? '' : ' disabled'}>Записать</button>
            </div>
            <div class="flasher-config-note">Каждое значение записывается отдельно и сразу читается обратно — окно показывает то, что хранит датчик.</div>
          </section>
        </div>
      `;
    }

    function myNote() {
      return linkNote && linkNote.dev === dev() ? linkNote : null;
    }

    function renderNetwork(snap) {
      const info = snap.info || {};
      const line = info.line || {};
      const net = snap.network || {};
      const bauds = Array.isArray(net.bauds) && net.bauds.length ? net.bauds : [Number(line.baudrate) || 9600];
      const cur = Number(line.baudrate) || Number(net.baudrate) || 9600;
      const parities = Array.isArray(net.parities) && net.parities.length ? net.parities : ['N'];
      const note = myNote();
      return `
        <div class="flasher-config-grid">
          <section class="flasher-config-card">
            <h4>Сеть RS-485</h4>
            <dl class="flasher-config-kv">
              <div><dt>Сейчас</dt><dd class="mono" id="cfg-mtd-net-current">${esc(lineText(info.address, line))}</dd></div>
            </dl>
            <div class="flasher-config-form">
              <label for="cfg-mtd-net-addr">Modbus-адрес</label>
              <input id="cfg-mtd-net-addr" type="number" min="1" max="247" value="${esc(isNum(info.address) ? info.address : 1)}" data-mtd-net="1" />
              <label for="cfg-mtd-net-baud">Скорость</label>
              <select id="cfg-mtd-net-baud" data-mtd-net="1" disabled>
                ${bauds.map(function (b) { return `<option value="${Number(b)}"${Number(b) === cur ? ' selected' : ''}>${Number(b)}</option>`; }).join('')}
              </select>
              <label for="cfg-mtd-net-parity">Чётность</label>
              <select id="cfg-mtd-net-parity" data-mtd-net="1">
                ${parities.map(function (p) { return `<option value="${esc(p)}">${esc(p)}</option>`; }).join('')}
              </select>
            </div>
            <div class="flasher-config-note" id="cfg-mtd-net-baud-why">Проверка записей MQTT на этом порту…</div>
            <div class="flasher-config-actions">
              <button class="btn btn-primary" type="button" id="cfg-mtd-net-save-btn">Применить</button>
              <button class="btn btn-sm" type="button" id="cfg-mtd-net-refresh-btn">Обновить</button>
            </div>
            <div class="${note && note.kind === 'error' ? 'flasher-config-banner is-error' : 'flasher-config-note'}" id="cfg-mtd-link-note" ${note ? '' : 'hidden'}>${esc(note ? note.text : '')}</div>
            <div class="flasher-config-note">Датчик работает не быстрее 57600 бод. Мост опрашивает линию без контроля чётности, поэтому датчик переводится только на чётность N.</div>
            <div class="flasher-config-note">Окно само находит датчик на новых параметрах и переносит его запись на вкладке MQTT; мост применит её после закрытия окна.</div>
          </section>
        </div>
      `;
    }

    function render(snap, tab) {
      if (tab === 'mtd_settings') return renderSettings(snap);
      if (tab === 'mtd_network') return renderNetwork(snap);
      return renderInfo(snap);
    }

    /** Same key ⇒ the skeleton did not change: patch live values in place. */
    function renderKey(snap, tab) {
      const m = block(snap);
      const fields = (m.settings || []).map(function (f) { return f.reg + (f.writable ? 'w' : 'r'); }).join(',');
      const note = myNote();
      return ['mtd', tab || '', fields, m.template_ok === false ? 'notpl' : 'tpl', note ? note.text : ''].join('|');
    }

    function focused(node) {
      const a = host.activeElement ? host.activeElement() : null;
      return !!node && a === node;
    }

    function patch(snap) {
      const r = readouts(snap);
      Object.keys(r).forEach(function (id) {
        const n = el(id);
        if (n) n.textContent = r[id];
      });
      const sil = el('cfg-mtd-silent');
      if (sil) sil.hidden = !silent(snap);
      const warn = el('cfg-mtd-rep-warn');
      if (warn) warn.hidden = !repOn(snap);
      (block(snap).settings || []).forEach(function (f) {
        const input = el('cfg-mtd-f-' + Number(f.reg));
        if (!input || focused(input) || input.dataset.mtdDirty) return;
        input.value = f.value == null ? '' : String(f.value);
      });
    }

    // ── settings ────────────────────────────────────────────────────────────
    /** {raw} or {error}: the browser's convenience check (the daemon decides). */
    function fieldRaw(field, text) {
      const s = String(text == null ? '' : text).trim().replace(',', '.');
      if (s === '' || !isNum(s)) return { error: 'Введите число' };
      const v = Number(s);
      if (v < Number(field.min) || v > Number(field.max)) return { error: 'Вне диапазона' };
      const step = Number(field.step) || 1;
      const raw = Math.round(v / step);
      if (Math.abs(v / step - raw) > 1e-6) return { error: 'Значение не на шаге настройки' };
      return { raw: raw };
    }

    function body() { return { port: host.currentPort(), device: dev() }; }
    function label(f) { return f.title || f.name || String(f.reg); }

    async function applySettings() {
      const snap = host.getState().configSnapshot;
      const fields = (block(snap).settings || []).filter(function (f) { return f.writable; });
      const todo = [];
      for (const f of fields) {
        const input = el('cfg-mtd-f-' + Number(f.reg));
        if (!input || !input.dataset.mtdDirty) continue;
        const got = fieldRaw(f, input.value);
        if (got.error) {
          if (input.setAttribute) input.setAttribute('aria-invalid', 'true');
          host.toast(host.t(label(f)) + ': ' + host.t(got.error), 'error');
          return;
        }
        if (input.removeAttribute) input.removeAttribute('aria-invalid');
        todo.push({ f: f, raw: got.raw, input: input });
      }
      if (!todo.length) {
        host.toast('Изменённых настроек нет', 'info');
        return;
      }
      const b = body();
      if (!b.port || !b.device) return;
      host.setConfigBusy(true);
      host.invalidatePolls();
      let last = null;
      try {
        for (const item of todo) {
          last = await host.configApi('/device_config/holding',
            { port: b.port, device: b.device, reg: Number(item.f.reg), value: item.raw });
          delete item.input.dataset.mtdDirty;
          const w = (last && last.write) || {};
          if (w.applied === false && (w.read_back === null || w.read_back === undefined)) {
            // The write went out but the read-back did not come: what the
            // sensor holds is unknown — say so, never «… — null».
            host.toast(host.t(label(item.f)) + ': ' + host.t('чтение после записи не удалось'), 'warn');
          } else if (w.applied === false) {
            host.toast(host.t(label(item.f)) + ': ' + host.t('датчик сохранил другое значение') +
              ' — ' + String(w.read_back), 'warn');
          }
        }
        host.getState().configBodyKey = '';
        if (last) host.applyConfigSnapshot(last, false);
        host.toast('Настройки датчика записаны', 'success');
      } catch (err) {
        host.setConfigBanner('Запись настройки: ' + host.t(err.message), 'error');
        host.toast('Настройка датчика: ' + host.t(err.message), 'error');
      } finally {
        host.setConfigBusy(false);
      }
    }

    // ── network (F2 rule, the link change, the relink) ──────────────────────
    function selfMatch() {
      return host.bridge.templateMatch(MTD_TEMPLATE);
    }

    function baudRule(cfg, snap) {
      const info = snap.info || {};
      const line = info.line || {};
      const net = snap.network || {};
      return host.bridge.allowedBauds(cfg, host.currentPort(),
        { address: info.address, match: selfMatch() },
        Number(line.baudrate), net.bauds || []);
    }

    function ruleText(rule) {
      if (!rule || !rule.reason) return '';
      if (rule.reason === 'port') {
        return 'На этом порту мост опрашивает устройства на ' + Number(rule.port_baud) +
          ' бод — датчик можно перевести только на эту скорость.';
      }
      if (rule.reason === 'mixed') return 'Мост уже опрашивает этот порт на разных скоростях — скорость датчика не меняется.';
      return 'У записи на этом порту не указана скорость — скорость датчика не меняется.';
    }

    /** Patch the baud select in place from the bridge view (no re-render: the
        operator may already be typing the address). */
    function applyBaudRule() {
      const sel = el('cfg-mtd-net-baud');
      const why = el('cfg-mtd-net-baud-why');
      const snap = host.getState().configSnapshot;
      if (!sel || !snap) return;
      const line = (snap.info || {}).line || {};
      const cur = Number(line.baudrate);
      const view = bridgeView && bridgeView.dev === dev() ? bridgeView : null;
      let allowed = [cur];
      let text = 'Проверка записей MQTT на этом порту…';
      if (view && view.state === 'ok') {
        allowed = view.rule.bauds;
        text = ruleText(view.rule);
      } else if (view && view.state === 'error') {
        text = 'Не удалось прочитать конфигурацию MQTT — скорость датчика не меняется.';
      }
      const opts = Array.prototype.slice.call(sel.options || []);
      for (const o of opts) {
        const ok = allowed.indexOf(Number(o.value)) >= 0;
        o.disabled = !ok;
        o.hidden = !ok;
      }
      sel.allowed = allowed.slice();
      sel.disabled = allowed.length < 2;
      if (allowed.indexOf(Number(sel.value)) < 0) sel.value = String(cur);
      if (why) {
        why.textContent = host.t(text);
        why.hidden = !text;
      }
    }

    async function loadBridgeView() {
      const d = dev();
      const snap = host.getState().configSnapshot;
      if (!d || !snap) return null;
      bridgeView = { dev: d, state: 'loading' };
      applyBaudRule();
      const got = await host.bridge.fetchConfig();
      if (bridgeView.dev !== d) return bridgeView;
      bridgeView = got.ok
        ? { dev: d, state: 'ok', rule: baudRule(got.cfg, host.getState().configSnapshot || snap) }
        : { dev: d, state: 'error', error: got.error };
      applyBaudRule();
      return bridgeView;
    }

    function setNote(text, kind) {
      linkNote = text ? { dev: dev(), text: host.t(text), kind: kind } : null;
      const n = el('cfg-mtd-link-note');
      if (n) {
        n.textContent = linkNote ? linkNote.text : '';
        n.hidden = !text;
        n.className = kind === 'error' ? 'flasher-config-banner is-error' : 'flasher-config-note';
      }
    }

    async function relinkAfter(port, oldLine, reported) {
      const res = await host.bridge.relink(
        { port: port, address: oldLine.address },
        { address: reported.address, baudrate: reported.baudrate },
        selfMatch());
      // The F2 rule of the NEW line, from the config the relink just read (or
      // wrote) — no second request; on an error the next tab visit re-reads.
      const snapNow = host.getState().configSnapshot;
      bridgeView = res.cfg && snapNow
        ? { dev: dev(), state: 'ok', rule: baudRule(res.cfg, snapNow) }
        : null;
      applyBaudRule();
      if (res.status === 'moved') {
        setNote('Запись MQTT перенесена: ' + res.id, 'info');
      } else if (res.status === 'none') {
        setNote('В MQTT датчик не настроен — запись не требуется', 'info');
      } else if (res.status === 'unchanged') {
        setNote('Запись MQTT уже на этих параметрах', 'info');
      } else if (res.status === 'ambiguous') {
        setNote('В MQTT несколько записей с этим адресом — ни одна не изменена: ' +
          res.ids.join(', '), 'error');
      } else {
        setNote('Датчик на адресе ' + Number(reported.address) +
          ', а запись MQTT не обновлена: исправьте её на вкладке MQTT', 'error');
      }
      res.failed = res.status === 'error' || res.status === 'ambiguous';
      return res;
    }

    async function saveNetwork() {
      const b = body();
      if (!b.port || !b.device) return;
      const snap = host.getState().configSnapshot || {};
      const info = snap.info || {};
      const oldLine = { address: Number(info.address), baudrate: Number((info.line || {}).baudrate) };
      const network = {
        address: parseInt((el('cfg-mtd-net-addr') || {}).value, 10),
        baudrate: parseInt((el('cfg-mtd-net-baud') || {}).value, 10),
        parity: String((el('cfg-mtd-net-parity') || {}).value || 'N'),
      };
      if (!(network.address >= 1 && network.address <= 247)) {
        host.toast('Modbus-адрес: только 1..247', 'error');
        return;
      }
      if (network.parity !== 'N') {
        host.toast('Чётность: только N', 'error');
        return;
      }
      if (network.address === oldLine.address && network.baudrate === oldLine.baudrate) {
        host.toast('Параметры связи не изменились', 'info');
        return;
      }
      host.setLinkBusy(true);
      host.setConfigBusy(true);
      host.invalidatePolls();
      try {
        if (network.baudrate !== oldLine.baudrate) {
          // F2 on a FRESH read: the MQTT tab may have changed the port since
          // the tab was drawn.
          const view = await loadBridgeView();
          if (!view || view.state !== 'ok' || view.rule.bauds.indexOf(network.baudrate) < 0) {
            const why = view && view.state === 'ok' ? ruleText(view.rule) : 'Не удалось прочитать конфигурацию MQTT — скорость датчика не меняется.';
            host.setConfigBanner(why, 'error');
            host.toast('Скорость не изменена', 'error');
            return;
          }
        }
        let next;
        try {
          next = await host.configApi('/device_config/network',
            { port: b.port, device: b.device, network: network });
        } catch (err) {
          host.setConfigBanner('Сохранение сети: ' + host.t(err.message), 'error');
          host.toast('Сеть устройства: ' + host.t(err.message), 'error');
          return;
        }
        host.getState().configNetworkDirty = false;
        host.getState().configBodyKey = '';
        host.applyConfigSnapshot(next, false);
        const na = (next && next.network_apply) || {};
        const reported = na.device || {};
        // The bridge follows the line the sensor answers on NOW (the daemon's
        // report), on the port this call used — never the requested line and
        // never a port picked while the call ran.
        const moved = isNum(reported.address) &&
          (Number(reported.address) !== oldLine.address || Number(reported.baudrate) !== oldLine.baudrate);
        const relink = moved ? await relinkAfter(b.port, oldLine, reported) : null;
        if (na.error) {
          // Stopped half-way: the snapshot above was read where the sensor
          // answers now.
          host.setConfigBanner('Сеть применена не полностью: ' + host.t(String(na.error)), 'error');
          host.toast('Параметры связи записаны не полностью', 'error');
        } else if (na.applies_after_restart) {
          // Some registers wait for a power cycle; the bridge entry is on the
          // line the sensor answers on now and must be fixed after it.
          // On a failed relink the note says so; the banner then claims nothing
          // about the entry.
          const entry = relink && relink.failed ? '' : ' (запись MQTT — на этой линии)';
          host.setConfigBanner('Сейчас датчик отвечает на адресе ' + Number(reported.address) + ', ' +
            Number(reported.baudrate) + ' бод' + entry + '; после перезапуска питания он перейдёт на адрес ' +
            network.address + ', ' + network.baudrate + ' бод — тогда исправьте запись на вкладке MQTT.', 'info');
          if (relink && relink.failed) host.toast('Датчик на новом адресе, но запись MQTT не обновлена', 'error');
          else host.toast('Параметры связи записаны — перезапустите питание датчика', 'info');
        } else if (relink && relink.failed) {
          host.toast('Датчик на новом адресе, но запись MQTT не обновлена', 'error');
        } else {
          host.toast('Параметры связи изменены. Проверьте программы MPLC4/CODESYS, которые опрашивают датчик.', 'success');
        }
      } finally {
        host.setConfigBusy(false);
        host.setLinkBusy(false);
      }
    }

    function wire(root) {
      root.querySelectorAll('[data-mtd-reg]').forEach(function (input) {
        const mark = function () { input.dataset.mtdDirty = '1'; };
        input.addEventListener('input', mark);
        input.addEventListener('change', mark);
      });
      root.querySelectorAll('[data-mtd-net]').forEach(function (input) {
        const mark = function () { host.getState().configNetworkDirty = true; };
        input.addEventListener('input', mark);
        input.addEventListener('change', mark);
      });
      const apply = root.querySelector('#cfg-mtd-apply-btn');
      if (apply) apply.addEventListener('click', applySettings);
      const save = root.querySelector('#cfg-mtd-net-save-btn');
      if (save) save.addEventListener('click', saveNetwork);
      const refresh = root.querySelector('#cfg-mtd-net-refresh-btn');
      if (refresh) refresh.addEventListener('click', function () { host.refreshSnapshot(); });
      if (root.querySelector('#cfg-mtd-net-baud')) {
        const view = bridgeView && bridgeView.dev === dev() ? bridgeView : null;
        if (view && view.state !== 'loading') applyBaudRule();
        else if (!view) loadBridgeView();
      }
    }

    return {
      tabs: tabs, title: title, kicker: kicker, render: render, renderKey: renderKey,
      patch: patch, wire: wire, applySettings: applySettings, saveNetwork: saveNetwork,
      fieldRaw: fieldRaw, loadBridgeView: loadBridgeView, applyBaudRule: applyBaudRule,
    };
  }

  window.sa02mMtdWindow = {
    create: create, MTD_TABS: MTD_TABS, MTD_MODEL: MTD_MODEL, MTD_TEMPLATE: MTD_TEMPLATE,
    MTD_UNITS: MTD_UNITS, MTD_HINTS: MTD_HINTS,
  };
})();
