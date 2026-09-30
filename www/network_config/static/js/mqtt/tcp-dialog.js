/* SA-02m MQTT tab — the add-device dialog's Modbus TCP decisions (pure ES module).
 *
 * Every non-DOM rule of the dialog's Ethernet path lives here so it can be unit
 * tested without a browser (scripts/dev/test-mqtt-tcp-dialog.mjs, row
 * js-unit-mqtt-tcp-dialog): which types go over TCP, which rows show, the
 * device id, the ONE entry shape a save and «Проверить связь» both send, and
 * the operator's wording for a refusal or a probe verdict. mqtt.js keeps the
 * DOM glue and the window.* shims (docs/decisions/es-modules.md).
 *
 * The address rules themselves are the board's (bridge_bus, one home:
 * docs/contracts/bridge-modbus-tcp.md) — none are copied here. The translator
 * is injected and named `uiT` on purpose: i18n-dict-contract sweeps literal
 * `uiT('…')` arguments, so every wording below is gated for its DICT entry.
 * No document/window access in this file.
 */

/** Modbus TCP id: `<template>-tcp-<a_b_c_d>-<addr>` / `carel-tcp-<a_b_c_d>-<addr>`.
 *  A Carel id MUST start with `carel-` (the «Устройства» glob, the Alice prefix). */
export function makeTcpDeviceId(type, templatePrefix, host, addr) {
  const host_ = String(host || '').trim().replace(/\./g, '_');
  const prefix = type === 'carel' ? 'carel' : templatePrefix;
  return `${prefix}-tcp-${host_}-${addr}`;
}

/** May the «Тип устройства» option `value` be offered? Over TCP only the types
 *  the server advertised (`capabilities.tcp_types`); on RS-485 everything but
 *  Carel (the bridge takes Carel over the network only). */
export function typeOptionAllowed(value, tcp, tcpTypes) {
  if (tcp) return Array.isArray(tcpTypes) && tcpTypes.includes(value);
  return value !== 'carel';
}

/** Which dialog rows are shown. `tcp` = Ethernet picked; `capsPresent` = the
 *  board advertised TCP at all (without it the dialog is RS-485 only). */
export function dialogRows(tcp, type, capsPresent) {
  const net = !!capsPresent && !!tcp;
  return {
    transport: !!capsPresent,
    port: !net,
    host: net,
    tcpPort: net,
    carelFamily: net && type === 'carel',
    template: type === 'template',
    probe: net,
  };
}

/** Unit 0 is the Modbus broadcast id; over TCP 1..255, on RS-485 1..247. */
export function addrMax(tcp) {
  return tcp ? 255 : 247;
}

/** The one TCP entry shape: what «Добавить» pushes into the YAML and what
 *  «Проверить связь» posts, so the probe judges exactly what a save would.
 *  Key order is the YAML's (the save writes the list as-is). */
export function buildTcpEntry({ id, type, template, family, host, tcpPort, address, name }) {
  const host_ = String(host || '').trim();
  const port = parseInt(tcpPort, 10) || 502;
  const dev = { id, type };
  if (type === 'template') dev.template = template;
  if (type === 'carel') dev.family = family;
  Object.assign(dev, { transport: 'tcp', host: host_, tcp_port: port, address });
  dev.name = name || (type === 'carel'
    ? `Carel ${family === 'uaria' ? 'uAria' : 'c.pCOmini'} (${host_}:${port} addr=${address})`
    : id);
  dev.poll_s = 2;
  return dev;
}

// mqtt_config.cgi / mqtt_tcp_probe.cgi refusal of a Modbus TCP entry → the
// operator's words. Keys are the bridge's codes (bridge_bus.REASONS) — the unit
// test fails when the two sets drift apart.
export const TCP_REFUSAL_TEXT = {
  transport_unknown: uiT => uiT('Неизвестный способ подключения устройства'),
  type_not_tcp_capable: uiT => uiT('По сети (Modbus TCP) подключаются только «Шаблон устройства» и Carel'),
  carel_family_required: uiT => uiT('Для Carel по сети укажите семейство: c.pCOmini или uAria'),
  host_missing: uiT => uiT('Укажите IP-адрес устройства'),
  host_not_ipv4_literal: uiT => uiT('IP-адрес: четыре числа через точку, без ведущих нулей, например 192.168.1.20'),
  // One code for loopback, 0.0.0.0, multicast and reserved (incl. 255.255.255.255)
  // — the server does not say which, so the text names the three classes. The
  // board's own LAN address is NOT judged here (it is refused at connect time).
  host_forbidden: uiT => uiT('Адрес не допускается: служебный, групповой или адрес самой платы'),
  tcp_port_invalid: uiT => uiT('TCP-порт должен быть от 1 до 65535'),
  unit_invalid: uiT => uiT('Адрес Modbus по сети должен быть от 1 до 255'),
  timeout_invalid: uiT => uiT('Таймаут Modbus TCP должен быть от 0,2 до 5 с'),
  serial_keys_on_tcp: uiT => uiT('У сетевого устройства не указывают COM-порт и скорость'),
  tcp_endpoint_limit: uiT => uiT('Слишком много сетевых устройств: не больше 16 разных адресов'),
};

/** A refused save/probe → its wording, or '' when the reply is not a TCP-entry
 *  refusal (the caller falls back to its generic error). */
export function saveRefusalText(res, uiT) {
  if (!res) return '';
  if (res.error === 'transport_validator_unavailable') {
    return uiT('Проверка сетевых устройств недоступна: обновите мост MQTT на плате');
  }
  if (res.error !== 'invalid_device') return '';
  const text = TCP_REFUSAL_TEXT[res.reason];
  if (!text) return '';
  return res.id ? `${text(uiT)} (${res.id})` : text(uiT);
}

// «Проверить связь» verdicts (closed enum: docs/contracts/bridge-modbus-tcp.md
// §10) → the result line. Only device_ok / device_exception may read «отвечает».
export const PROBE_VERDICT_VIEW = {
  device_ok: (r, uiT) => ({ state: 'ok', text: `${uiT('Устройство отвечает по Modbus TCP')} (${uiT('адрес')} ${r.address})` }),
  // An exception PDU from this unit: alive, register 0 just is not readable.
  device_exception: (r, uiT) => ({ state: 'ok', text: `${uiT('Устройство отвечает по Modbus TCP')} (${uiT('адрес')} ${r.address})` }),
  unit_silent: (r, uiT) => ({ state: 'warn', text: `${uiT('Хост отвечает, но устройство с адресом')} ${r.address} ${uiT('молчит — проверьте адрес Modbus')}` }),
  not_modbus: (r, uiT) => ({ state: 'warn', text: uiT('Порт открыт, но это не Modbus TCP — проверьте TCP-порт') }),
  tcp_refused: (r, uiT) => ({ state: 'warn', text: `${uiT('Соединение отклонено: нет службы на TCP-порту')} ${r.tcp_port}` }),
  tcp_timeout: (r, uiT) => ({ state: 'warn', text: uiT('Нет ответа за 1 с: адрес недоступен или выключен') }),
  tcp_unreachable: (r, uiT) => ({ state: 'warn', text: uiT('Нет маршрута к адресу — проверьте подсеть платы') }),
  self: (r, uiT) => ({ state: 'warn', text: uiT('Адрес не допускается: служебный, групповой или адрес самой платы') }),
};

/** Any probe reply (or a transport failure shaped `{ok:false, error}`) →
 *  `{state: 'ok'|'warn'|'err'|'unavailable', text}`. An unknown verdict from a
 *  newer board is never 'ok'. */
export function probeResultView(res, uiT) {
  const failed = code => ({ state: 'err', text: `${uiT('Проверка связи не выполнена')}: ${code}` });
  if (!res || typeof res !== 'object') return failed('invalid_response');
  if (res.ok === true) {
    const view = Object.prototype.hasOwnProperty.call(PROBE_VERDICT_VIEW, res.verdict)
      ? PROBE_VERDICT_VIEW[res.verdict] : null;
    return view ? view(res, uiT) : failed(String(res.verdict));
  }
  const err = String(res.error || 'unknown');
  if (err === 'invalid_device') {
    const why = saveRefusalText(res, uiT);
    return why ? { state: 'warn', text: why } : failed(`${err} ${res.reason || ''}`.trim());
  }
  if (err === 'probe_busy') return { state: 'warn', text: uiT('Проверка уже выполняется — подождите') };
  // An older board has no endpoint (nginx 404), or the probe module is missing.
  if (err === 'probe_unavailable' || err === 'HTTP 404') {
    return { state: 'unavailable', text: uiT('Проверка связи недоступна на этой плате: обновите веб и мост MQTT') };
  }
  return failed(err);
}

/** Entries sharing a `host:tcp_port` that disagree on `tcp_timeout_s` (absent
 *  = 1.0, the bridge default): the bridge keeps the first and logs one WARN
 *  (contract §2). The panel has no timeout field today — this is the rule's
 *  cheap test and the hook for a future panel warning. */
export function tcpTimeoutConflicts(devices) {
  const byEndpoint = new Map();
  for (const d of devices || []) {
    if (!d || d.transport !== 'tcp') continue;
    const endpoint = `${String(d.host || '').trim()}:${parseInt(d.tcp_port, 10) || 502}`;
    const t = d.tcp_timeout_s == null ? 1.0 : Number(d.tcp_timeout_s);
    if (!byEndpoint.has(endpoint)) byEndpoint.set(endpoint, { timeouts: new Set(), ids: [] });
    const g = byEndpoint.get(endpoint);
    g.timeouts.add(t);
    g.ids.push(d.id);
  }
  const out = [];
  for (const [endpoint, g] of byEndpoint) {
    if (g.timeouts.size > 1) out.push({ endpoint, timeouts: [...g.timeouts].sort((a, b) => a - b), ids: g.ids });
  }
  return out;
}
