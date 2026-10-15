/* Bridge relink — the MQTT bridge entry follows a device whose RS-485 link the
   flasher window just changed (plan mtd-settings-window F3; type-agnostic so
   the MR-02m window can reuse it).

   Why the browser and not the daemon: the bridge config is written ONLY through
   cgi-bin/mqtt_config.cgi (auth + CSRF + the bridge's own validator + the root
   apply helper — the MQTT tab's path). The flasher daemon runs unprivileged and
   gets no new grant. The bridge reads the file when the window's port lease is
   restored on close, so the post carries `restart:false`.

   Rules (docs/contracts/mtd-presence.md):
     * relink(): GET the config; refuse to write anything when the GET reply is
       not a config (an `error` key, no `devices` array) — posting it back would
       wipe the fleet. Exactly ONE entry on that COM port at the old address
       that `match()` accepts gets the NEW address/baud (the line the daemon
       REPORTS, not the requested one). Zero → nothing to do; two or more →
       nothing is written and the ids are named.
     * allowedBauds(): a baud change is offered only when every OTHER bridge
       entry on that port already uses the target baud — the bridge cannot
       serve two bauds on one COM (modbus_mqtt_bridge.mixed_baud_port_conflicts).
       An entry without a numeric baud, or a port already at mixed bauds, keeps
       the device on its current baud (fail closed).

   Classic script, ONE global: window.sa02mBridgeRelink. Verified by
   scripts/dev/test-flasher-mtd-window.mjs (row js-unit-flasher-mtd-window). */
(function () {
  'use strict';

  const CONFIG_URL = 'cgi-bin/mqtt_config.cgi';

  function portPath(portKey) {
    return '/dev/' + String(portKey || '');
  }

  function onPort(entry, portKey) {
    return !!entry && typeof entry === 'object' && entry.port === portPath(portKey);
  }

  /** The bridge entries of one RS-485 device: on `portKey` at `address` and
      accepted by `match`. */
  function entriesFor(cfg, portKey, address, match) {
    const devices = (cfg && Array.isArray(cfg.devices)) ? cfg.devices : [];
    return devices.filter(function (e) {
      return onPort(e, portKey) && Number(e.address) === Number(address) && match(e);
    });
  }

  async function fetchConfig() {
    let res;
    let data = null;
    try {
      res = await fetch(CONFIG_URL, { credentials: 'include', cache: 'no-store' });
      data = await res.json();
    } catch (err) {
      return { ok: false, error: (err && err.message) || 'network' };
    }
    if (!res.ok || !data || typeof data !== 'object' || data.error || !Array.isArray(data.devices)) {
      return { ok: false, error: (data && data.error) || ('HTTP ' + (res && res.status)) };
    }
    delete data.capabilities;
    return { ok: true, cfg: data };
  }

  async function postConfig(cfg) {
    let res;
    let data = null;
    try {
      res = await fetch(CONFIG_URL, {
        method: 'POST',
        credentials: 'include',
        headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(Object.assign({}, cfg, { restart: false })),
      });
      data = await res.json();
    } catch (err) {
      return { ok: false, error: (err && err.message) || 'network' };
    }
    if (res.ok && data && data.ok === true) return { ok: true };
    return { ok: false, error: (data && data.error) || ('HTTP ' + (res && res.status)) };
  }

  /** {bauds, reason}: which of `candidates` the device may move to on
      `portKey`. `self` (address + match) names the device's own entry, which is
      not an "other". The current baud is always kept. */
  function allowedBauds(cfg, portKey, self, current, candidates) {
    const cur = Number(current);
    const devices = (cfg && Array.isArray(cfg.devices)) ? cfg.devices : [];
    const others = devices.filter(function (e) {
      return onPort(e, portKey) && !(Number(e.address) === Number(self.address) && self.match(e));
    });
    const list = (candidates || []).map(Number);
    if (!others.length) return { bauds: list, reason: '', others: [] };
    const bauds = others.map(function (e) { return Number(e.baudrate); });
    if (bauds.some(function (b) { return !Number.isFinite(b) || b <= 0; })) {
      return { bauds: [cur], reason: 'unknown', others: others };
    }
    const unique = bauds.filter(function (b, i) { return bauds.indexOf(b) === i; });
    if (unique.length !== 1) return { bauds: [cur], reason: 'mixed', others: others, port_bauds: unique };
    const only = unique[0];
    const allowed = list.filter(function (b) { return b === only || b === cur; });
    return { bauds: allowed.length ? allowed : [cur], reason: 'port', others: others, port_baud: only };
  }

  /** Move the bridge entry of the device from `oldLine` to `newLine`.
      oldLine: {port, address}; newLine: {address, baudrate}.
      → {status: 'moved'|'none'|'unchanged'|'ambiguous'|'error', id, ids, error,
         cfg: the config as it now stands on the board (absent on 'error')} */
  async function relink(oldLine, newLine, match) {
    const got = await fetchConfig();
    if (!got.ok) return { status: 'error', error: got.error, stage: 'read' };
    const found = entriesFor(got.cfg, oldLine.port, oldLine.address, match);
    if (!found.length) return { status: 'none', cfg: got.cfg };
    if (found.length > 1) {
      return { status: 'ambiguous', ids: found.map(function (e) { return String(e.id || '?'); }), cfg: got.cfg };
    }
    const entry = found[0];
    const addr = Number(newLine.address);
    const baud = Number(newLine.baudrate);
    if (Number(entry.address) === addr && Number(entry.baudrate) === baud) {
      return { status: 'unchanged', id: String(entry.id || ''), cfg: got.cfg };
    }
    entry.address = addr;
    entry.baudrate = baud;
    const put = await postConfig(got.cfg);
    if (!put.ok) return { status: 'error', error: put.error, stage: 'write', id: String(entry.id || '') };
    return { status: 'moved', id: String(entry.id || ''), cfg: got.cfg };
  }

  /** A `match` for a template-driven device (`type: template`). */
  function templateMatch(name) {
    return function (e) { return !!e && e.type === 'template' && e.template === name; };
  }

  window.sa02mBridgeRelink = {
    CONFIG_URL: CONFIG_URL,
    fetchConfig: fetchConfig,
    allowedBauds: allowedBauds,
    entriesFor: entriesFor,
    relink: relink,
    templateMatch: templateMatch,
  };
})();
