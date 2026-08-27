# Contract: Alice ↔ MQTT mapping (SA-02m)

Machine-facing contract for `opt/sa02m-alice`. Human overview:
`docs/ALICE_INTEGRATION.md`.

## Topics

- State subscribe: `/devices/<id>/controls/<name>` (Wiren Board MQTT shape).
- Command publish: `/devices/<id>/controls/<name>/on` — payload `"0"` / `"1"` /
  numeric string; **no retain** on writes (same rule as `mqtt_set.cgi`).
- On Socket.IO connect the retained burst is **cached but never reported**:
  retained values fill the state cache (the query fan-out answers from it, so a
  freshly started client has state immediately), and only the outbound
  `device_state` event is suppressed until the settle window elapses. Caching
  them was the 1.0.6.16 fix — dropping them left every sensor empty in the
  Alice app until the bridge happened to republish.

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
  amperage | power | pressure | co2_level | battery_level`; `unit` matches
  `^unit\.[a-z_.]{1,32}$` (shell/JSON-safe by construction). A capability's
  `type` must start `devices.capabilities.`, a property's
  `devices.properties.` — a cross-typed item is rejected.
- UI kinds (`app/alice.js` `ALICE_KINDS`): temperature/humidity →
  `devices.types.sensor.climate`; voltage/amperage/power →
  `devices.types.sensor`. The bridge publishes engineering units (°C, %RH,
  V/A/W) — `float(raw)` is the whole conversion; negative values (CE power
  export) parse as-is.
- An unparseable MQTT payload converts to NO reading (the property is omitted
  from query/state) — never a fabricated `0.0`.
- Properties are read-only: they never enter `apply_actions`, so a sensor
  topic gets no `/on` command publish.

Validating tests: `opt/sa02m-alice/tests/test_models.py`,
`test_converters.py`, `test_device_registry.py`, `test_topics_inventory.py`.

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
  bool, …}` — `cert_present` is evaluated by the client on EVERY write
  (`sa02m_alice/client/main.py::_write_status`), so the file is the source of
  cert truth for unprivileged readers. Additive: older keys unchanged.
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

Validating tests: `opt/sa02m-alice/tests/test_cert_status.py`.

## Non-goals

- Copying `wb-mqtt-alice` source (MIT-WB).
- Storing Yandex OAuth tokens on the controller.
- On-device Home Assistant.
