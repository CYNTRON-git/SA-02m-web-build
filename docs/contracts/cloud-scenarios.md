# Contract: cloud catalogue + on-board scenario channel (SA-02m)

Board-side contract for the cloud-profile Socket.IO catalogue
(rename / rooms / groups / scenarios) and the on-board engine
`sa02m-rules` (`rules_engine=2`). Human overview of Alice/cloud
transport: `docs/ALICE_INTEGRATION.md`. MQTT mapping of field
devices: `docs/contracts/alice-mqtt-mapping.md`. Gateway event
names that live in the **cloud** repo (`docs/contracts/alice-gateway.md`)
are cited, not restated.

Landed 1.0.6.37. Validating tests: `opt/sa02m-alice/tests/test_scenario_events.py`,
`opt/sa02m-alice/tests/test_cloud_control_api.py`,
`opt/sa02m-rules/tests/test_engine.py`.

---

## Channel (Socket.IO, profile `cloud`)

Events (constants in `sa02m_alice/common/constants.py`):

| Event | Who | Body → board | Success patch keys |
|---|---|---|---|
| `alice_devices_list` | both profiles | `{request_id}` | `payload.devices`; **cloud only** also `rooms`, `groups`, and the scenario snapshot below |
| `alice_devices_rename` | both | `{request_id, device\|id, name}` | `{ok, name}` — hub patches the catalogue name; bindings/type/icon stay |
| `alice_devices_rooms` | both | upsert `{name, id?, devices?}` or `{id, delete:true}` | `{ok, room?, rooms?, devices?}` |
| `alice_devices_groups` | **cloud only** | upsert `{name, id?, device_ids?\|devices?, icon?}` or `{id, delete:true}` | `{ok, group?, groups?}` |
| `alice_devices_scenarios` | both (no-op without the rules stack) | store command, `request_id` stripped | store `_ok` payload + `rules_engine` |
| `controller_unlink` | **yandex only** | — | not registered on cloud (must not touch the Alice cert) |

Every mutating handler echoes `request_id`. Unknown / failed writes answer
`ok: false` plus `error` (and `message` where the local CGI already did).

### List snapshot — scenarios

`alice_devices_list` on the cloud profile **omits** the scenario keys when
the rules stack is absent or `/etc/sa02m-rules/scenarios.json` has never
been written. The hub treats a missing `scenarios` key as «controller
does not support scenarios» (UI: «контроллер не поддерживает сценарии»).
When present:

```json
{
  "scenarios": [],
  "scenario_runs": [],
  "scenario_notify": [],
  "scenario_library": "",
  "rules_engine": 2
}
```

`rules_engine` is `sa02m_rules.store.RULES_ENGINE` (currently **2**). It
also rides every successful `alice_devices_scenarios` response so the hub
cache does not wait for the next list.

### Rooms

Atomic full-membership rebind. `devices: [...]` is the complete set for
that room: members get `room_id`, former members **drop** the key (empty
string must not serialise). Unknown device ids → `not_found`. Delete
clears `room_id` on remaining devices.

### Groups

Membership lives **only** on the group (`device_ids`). Delete drops the
group, never the devices. `icon` is `light` | `ahu` when sent; omitted
preserves the stored value. Unknown device ids → `not_found`.

### Rename

Patches only `name`. Success payload is exactly `{ok, name}` — the hub
cache patches from those keys.

---

## Store (`/etc/sa02m-rules/scenarios.json`)

Atomic JSON (`os.replace` + fsync). Default path overridable via
`SA02M_RULES_PATH`. Caps: 64 scenarios on `replace`, 16 on batch
`upsert`, `params` ≤ 4 KiB, `runs` 50, `notify_queue` 20.

`apply_command` verbs (same function the CGI path and the Socket.IO
handler call — one write path):

| Body | Effect |
|---|---|
| `{library}` (no `name`/`id`) | replace the JS library string (≤16 KiB) |
| `{replace:true, scenarios:[…]}` | full replace; **all** rows validated first |
| `{id, delete:true}` | delete one |
| `{id, get:true}` | full document of one scenario (list rows are summaries) |
| `{upsert:[…]}` | batch upsert, validate **all** first (never half-written) |
| `{ack_notify:true}` | drain `notify_queue` |
| `{id, run_now:true}` | flag file `<path>.run`; wait up to 2.5 s for the engine tick |
| other object with `name` | single upsert |

Success payload always carries `ok`, `scenarios` (listed summaries),
`runs` (last 20), `notify_queue`, `library`.

Listed row: `id`, `name`, `enabled`, `type`, `order`, `last_run`,
`last_error`, `summary`, plus provenance when present (`template_id`,
`template_version`, `source`, `params`).

Types: `block` | `code` | `logic` | `scene`. `scene` is set-only (no
nested scenario/scene/delay that would recurse).

---

## Engine v2

Non-blocking scheduler (heap of timers, cancel-by-key generations).
`RUN_S=30` counts **execution** only — scheduler waits are excluded.
Memory: `MemoryMax=32M`, bounded heap/rings/trackers (`HEAP_MAX=4096`).

Triggers (`kind`): `state`, `time`, `sun`, `boot`, `every` `{minutes}`,
`button` `{device, input?, gesture}`, `presence` arrive/leave on
`Vars.home_mode`.

State operators: level `== != > < >= <= changed`; edge
`rises_above` / `drops_below` / `enters_range` / `leaves_range` (fire
once per crossing); events `motion_detected` / `motion_cleared` /
`opened` / `closed`.

Button gestures: `single` / `long` / `double` from bridge counters
`di_N_short` / `di_N_long` / `di_N_double` (`docs/MQTT_TOPICS.md`);
edge-classifier fallback on `di_N` fronts (long ≥ 500 ms, double ≤ 400 ms;
classifier muted 10 s after counters). `long_release` is classifier-only.
No fire on service start.

Actions: `set` (optional `transition_s`), `toggle`, `ramp` (cancellable;
a direct write cancels), `delay`, `scenario`, `scene {id}`, `mode`
(sets persisted `Vars.home_mode`), `notify`, `http`.

End: `{after_s, mode: "off"|"restore"}`. `off` turns off what the run
turned on; `restore` re-applies the pre-run snapshot; re-trigger resets.

Conditions: `time_window` presets `day`/`night` (sun), weekday
`days`/`workday`/`weekend`, `mode` via `Vars.home_mode`, `state` `for_s`.

Unknown `logic` template names are **stored** (the cloud catalog may be
newer) and rejected at run time with `last_error="unknown template"`.

### Logic templates (this engine)

`switch_light`, `motion_light`, `circadian`, `thermostat`,
`humidity_fan`, `co2_ventilation`, `humidifier`, `away_home`
(`sa02m_rules/logic_templates.py`). Cloud catalog ids that are not in
this tuple stay in the store and do not run.

---

## MQTT mirror

Virtual device `/devices/sa02m-rules-<id>/controls/*` (retained state
topics, never `/on`): `rule_enabled`, `remaining_s`,
`blocked_by_switch`, plus template-specific fields. Writes from the
engine to field devices go to `<topic>/on` with **no retain**, same
rule as `mqtt_set.cgi`. Capabilities marked `writable: false` on the
Alice document are not written.

---

## Deploy / restart

`sa02m-rules` holds `/opt/sa02m-rules` in memory; `sa02m-cloud-control`
and `sa02m-alice-client` import `sa02m_rules.store`. After a `/opt`
refresh, restart the whole set — otherwise a cloud push silently drops
`trigger`/`end` (bench 1.0.6.37). Runbook: `docs/deployment.md`.
OTA/offline from **this** release emit the restart set; the **next**
update after 1.0.6.37 is the first one that actually consumes it
(previous runner applies the pack).
