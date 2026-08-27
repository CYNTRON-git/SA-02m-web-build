# Contract: Alice ↔ MQTT mapping (SA-02m)

Machine-facing contract for `opt/sa02m-alice`. Human overview:
`docs/ALICE_INTEGRATION.md`.

## Topics

- State subscribe: `/devices/<id>/controls/<name>` (Wiren Board MQTT shape).
- Command publish: `/devices/<id>/controls/<name>/on` — payload `"0"` / `"1"` /
  numeric string; **no retain** on writes (same rule as `mqtt_set.cgi`).
- The retained burst on a NEW subscription is **cached but never reported**:
  retained values fill the state cache (the query fan-out answers from it, so a
  freshly started client has state immediately), and only the outbound
  `device_state` event is suppressed until that subscription's settle window
  elapses. Caching them was the 1.0.6.16 fix — dropping them left every sensor
  empty in the Alice app until the bridge happened to republish. The rule is
  **per subscription, not per connect**: it covers the burst after Socket.IO
  connect AND the burst for a topic a reload newly subscribes (a bounded grace
  window per added topic, `RETAINED_GRACE_S`).

## Device document (`/etc/sa02m-alice/sa02m-alice-devices.conf`)

```json
{
  "rooms": [{"id": "r1", "name": "Lab", "devices": ["d1"]}],
  "devices": [{
    "id": "d1",
    "name": "Pump",
    "room_id": "r1",
    "type": "devices.types.switch",
    "capabilities": [{
      "type": "devices.capabilities.on_off",
      "mqtt": "/devices/sa02m-SA-02/controls/do",
      "retrievable": true,
      "reportable": true,
      "parameters": {"instance": "on"}
    }],
    "properties": []
  }]
}
```

### Float properties (sensor devices)

A sensor binding is a device whose `properties` carry one
`devices.properties.float` item (topic names: `docs/MQTT_TOPICS.md`
§cyntron-dtv, §CE-02m-3 — never restated here):

```json
{
  "id": "s1",
  "name": "DTV temp",
  "type": "devices.types.sensor.climate",
  "capabilities": [],
  "properties": [{
    "type": "devices.properties.float",
    "mqtt": "/devices/dtv-COM3-1/controls/temp_bme680",
    "retrievable": true,
    "reportable": true,
    "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"}
  }]
}
```

- `parameters` (instance + unit) is REQUIRED for a float property (Yandex
  requires it for discovery). Validation (`config/models.py`):
  `instance` ∈ `FLOAT_INSTANCES` = `temperature | humidity | voltage |
  amperage | power | pressure | co2_level | tvoc | illumination |
  battery_level | water_level | electricity_meter`; `unit` matches
  `^unit\.[a-z0-9_.]{1,32}$` (shell/JSON-safe by construction — the digit
  class is load-bearing for `unit.density.mcg_m3`). A capability's
  `type` must start `devices.capabilities.`, a property's
  `devices.properties.` — a cross-typed item is rejected.
- UI kinds (`app/alice.js` `ALICE_KINDS`): temperature/humidity/pressure/
  co2/tvoc → `devices.types.sensor.climate`; voltage/amperage/power →
  `devices.types.sensor`; motion → `devices.types.sensor.motion`. The
  allowlist is deliberately wider than the UI — the rest is hand-edit surface.
- The bridge publishes engineering units — `float(raw) × scale` is the whole
  conversion; negative values (CE power export) parse as-is.
- An unparseable MQTT payload converts to NO reading (the property is omitted
  from query/state) — never a fabricated `0.0`.
- **A real `power: 0` IS sent, and that is a deliberate deviation from Yandex's
  stated range** (their float table says power «должно быть больше 0»).
  Operator decision, 2026-08-27: an idle line genuinely draws 0 W, and a
  provider inventing a non-zero floor would misreport the customer's own
  installation. The alternative — omitting the property while the reading is 0
  — was rejected because the app then shows "no data", which is a different
  fact from "no load". Recorded here so a moderator's question during the
  skill video test is answered from a decision, not improvised. A measured 0
  and an absent reading stay distinguishable: the unparseable case above still
  omits the property.
- Properties are read-only: they never enter `apply_actions`, so a sensor
  topic gets no `/on` command publish.

### One property per `(type, instance)` per device

A Yandex property is addressed by `(type, instance)` and nothing else — the
state wire format (`{"type":…,"state":{"instance":…,"value":…}}`) carries no
per-property identifier. **Honesty label: derived, not quoted.** The platform
docs do not state that an instance must be unique per device; they provide no
discriminator, which forces it.

`validate_device` therefore rejects a repeated `(type, instance)` within
`capabilities` or within `properties`
(`duplicate property instance: <instance>`). Consequences:

- A DTV maps to ONE device: temperature · humidity · pressure · co2_level ·
  tvoc · motion are six distinct instances.
- A CE-02m-3 does NOT: `voltage_a/b/c/ab/bc/ca` are all instance `voltage`,
  every current is `amperage`, every power is `power`. It needs one device per
  phase plus a totals device.
- The load path is unaffected — `config_store.load_devices` does not validate,
  so a pre-existing hand-edited document keeps loading; only writes are gated.
- **`config/models.py` is the only guard before Yandex**: the gateway forwards
  the payload verbatim and validates nothing (checked against the sibling
  `cloud` repo, 2026-08-27). Widen a pattern there deliberately — nothing
  downstream re-checks it.

### Scale (item level, never sent to Yandex)

An optional `scale` sits beside `mqtt`, NOT inside `parameters`: discovery
copies `parameters` verbatim into the Yandex payload, so a key there would
leak a non-Yandex field to the platform.

```json
{"type":"devices.properties.float","mqtt":"/devices/dtv-COM4-3/controls/pressure_bme680_kpa",
 "parameters":{"instance":"pressure","unit":"unit.pressure.mmhg"},"scale":7.50062}
```

- Absent ⇒ `1.0`, so every pre-existing item is byte-identical and unaffected.
- Validated as a finite number, `0 < |scale| ≤ 1e6`.
- Applied ONCE, in `converters.mqtt_to_float_property`, and rounded to 3
  decimals. No other code multiplies a reading.
- In use: kPa → mmHg `7.50062` (DTV pressure), mg/m³ → µg/m³ `1000` (TVOC).

### Event properties

```json
{"type":"devices.properties.event","mqtt":"/devices/dtv-COM4-3/controls/presence",
 "retrievable":true,"reportable":true,
 "parameters":{"instance":"motion","events":[{"value":"detected"},{"value":"not_detected"}]}}
```

- `parameters.events` is REQUIRED by Yandex for discovery: a non-empty list of
  `{"value": …}`, each value in the instance's closed set, no duplicates.
  `instance` ∈ `EVENT_INSTANCES` (`models.py`) = `motion | open | button |
  vibration | smoke | gas | water_leak | battery_level | food_level |
  water_level`.
- State shape: `{"type":"devices.properties.event","state":{"instance":"motion","value":"detected"}}`.
- **Payload mapping** (`converters.BOOL_EVENT_VALUES`): a payload already
  equal to an allowed value passes through unchanged; otherwise it is read as
  boolean-ish (numeric first, then `on/off/true/false/yes/no`) and mapped to
  the instance's (false, true) pair. The numeric branch is what the real bus
  needs — the bridge publishes DTV presence as `"1.0"` / `"0.0"` (a scaled
  register), which Yandex refuses verbatim.
- An unmappable payload, an unknown instance, or a pair slot with no event
  yields NO block — the same "omit rather than fabricate" rule as floats.
- Yandex has **no `presence` instance**; `motion` is the platform's home for
  presence detection. There is **no distance/length instance at all**, so a
  radar distance reading cannot be represented — nothing is substituted.

Validating tests: `opt/sa02m-alice/tests/test_models.py`,
`test_converters.py`, `test_device_registry.py`, `test_reload_watch.py`,
`test_state_sender.py`, `test_topics_inventory.py`.

### Binding edits apply without a restart (1.0.6.19)

A mutation of the device document (`upsert_device` / `delete_device` /
`upsert_room` / `delete_room`) made **while the client is connected** is applied
in place, without restarting it and without dropping the Socket.IO session:

- The client polls the document's `(inode, mtime, size)` on its existing
  one-second watchdog tick and re-reads it within ~1 s of the atomic write.
  Every writer is covered — the CGI, a hand edit, an offline restore.
- **That poll lives in the watchdog loop, which runs only while the Socket.IO
  session is up.** In the reconnect backoff, the error state and the
  missing-cert wait the document is not polled at all; an edit made then is
  picked up at the next connect, which re-reads before subscribing.
- Subscriptions are **diffed**: added topics are subscribed at QoS 1 (their
  retained burst under the grace window above), removed topics unsubscribed,
  unchanged topics left alone. A document that fails to parse is logged and the
  previous device set keeps serving.
- **No discovery push exists** (the event table below is gateway→controller
  pull only), so a NEW device reaches Alice when the user runs «Обновить список
  устройств» — unchanged from before.
- **The restart is skipped only for a client proven to be watching.** The
  privileged trigger's `restart` verb is sent by two CGI call sites — the
  binding mutation and the `complete_link` cert nudge — so it requires
  `state == connected` on top of the capability flag and the heartbeat. Outside
  a live session the client is not polling anything, and the restart happens as
  it always did: `enable` / `disable` use their own verbs and always restart,
  and `complete_link` on a client in missing-cert standby (the only state the
  panel offers it in — «Завершить привязку» appears only while NOT linked)
  restarts too, so a freshly enrolled cert is picked up at once.
  Residual, stated rather than hidden: a `complete_link` issued straight at the
  API while a session is already live is not restarted, and that session keeps
  its previous cert until it drops — the SSL context is built in
  `sio_connection.connect()`.

## Socket.IO events (controller ↔ `alice.cyntron.ru`)

| Direction | Event | Body |
|---|---|---|
| G→C | `alice_devices_list` | `{request_id}` |
| C→G | `alice_devices_response` | `{request_id, payload:{devices:[]}}` |
| G→C | `alice_devices_query` | `{request_id, devices:[{id}]}` |
| G→C | `alice_devices_action` | `{request_id, payload:{devices:[…]}}` |
| C→G | `device_state` | `{ts, payload:{devices:[]}}` |
| G→C | `controller_unlink` | `{}` |

## Error codes (Yandex)

`DEVICE_UNREACHABLE` | `INVALID_ACTION` | `INVALID_VALUE` | `INTERNAL_ERROR`

Action capability result: `{status:"DONE"|"ERROR", error_code?}`.

## Rate limits (`event_rates.json`)

- capabilities (on_off/range/…): 0.75 s, last_value
- properties.float: 300 s
- properties.event: 0.01 s + fast batch 0.1 s
- batch flush: 1.0 s normal / 0.1 s fast

The budget is **per `(device, type, instance)`**, not per device — a
multi-reading device's readings each hold their own window, and the outbound
batch keeps one row per instance. Keyed by type alone (through 1.0.6.17) all
float readings of one device shared a single 300 s slot AND collapsed into a
single batch row, so a six-reading card refreshed one value per window. The
limit is a floor between reports of the SAME reading, not a refresh period;
`query` answers from the MQTT cache and is unrated, so a card is never stale
on open.

## Offline / Phase 0

- `client_enabled=false` → client process exits 0.
- Gateway probe failure → API returns `gateway_unavailable`; UI must not show
  linked/paired success.
- Config CRUD and MQTT topic inventory work without the gateway.

## Client status file → web API cert / link truth (1.0.5.80)

The cert dir `/var/lib/sa02m-alice` is root-only (key 0600 root) and stays so;
the web API runs as `www-data` and MUST NOT probe it (an `isfile()` there is
permission-blind and returns a false "absent").

- **Status file** `/run/sa02m-alice/status.json` (client, root, mode 0644,
  rewritten on every state change): `{state, ts, version, cert_present:
  bool, config_watch: bool, …}` — `cert_present` is evaluated by the client on
  EVERY write (`sa02m_alice/client/main.py::_write_status`), so the file is the
  source of cert truth for unprivileged readers. Additive: older keys unchanged.
- `config_watch` is `true` when the running client re-reads the device document
  without a restart. The privileged web trigger
  (`usr/local/sbin/sa02m-alice-web-trigger.sh`) reads it — together with unit
  liveness and `ts` freshness (`STATUS_STALE_S`, 3× the 30 s heartbeat) — to
  decide whether a binding edit still needs a restart. **Absent, false, or
  stale ⇒ restart**, the pre-1.0.6.19 behaviour: the client and the helper ship
  together in one `06-alice.sh` run, while the CGI arrives by web update, so a
  newer CGI against an older client is normal and must not change anything.
- **API** (`sa02m_alice_api.cgi` GET / `full_config`) `mtls.cert_present` is
  tri-state — `true` / `false` when known, `null` when this process cannot tell —
  with `mtls.cert_check` naming the source: `"client"` (status file),
  `"local"` (own `isfile()`, only when the process can traverse the dir; a
  missing dir is a definite `false`), `"unreadable"` (dir present, not
  traversable → `null`). Never a false `false`.
- `link.linked` = enabled ∧ gateway available ∧ `status.state == connected` ∧
  `cert_present is not false` — a live mTLS session is itself proof of the cert;
  only an explicit `false` vetoes.
- UI (`app/alice.js`): «Сертификат» renders `true`→«Есть», `false`→«Нет»,
  unknown→«н/д»; «Статус: привязан» when `state == connected` OR
  `cert_present === true`; a session-local pending mark yields to linked.
- `pending_claim.json` is KEPT after a successful issue (`issued: true`) — the
  gateway requires `claim_token` for `/controller/unlink`; the status/link view
  never reads it.

Validating tests: `opt/sa02m-alice/tests/test_cert_status.py`,
`test_reload_watch.py`; the cross-language handshake is pinned by the
`alice-reload-handshake` quality row.

## Non-goals

- Copying `wb-mqtt-alice` source (MIT-WB).
- Storing Yandex OAuth tokens on the controller.
- On-device Home Assistant.
