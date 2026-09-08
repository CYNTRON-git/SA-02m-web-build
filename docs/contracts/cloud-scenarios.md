# Contract: cloud catalogue + on-board scenario channel (SA-02m)

Board-side contract for the cloud-profile Socket.IO catalogue
(rename / rooms / groups / scenarios) and the on-board engine
`sa02m-rules` (`rules_engine=2`). Human overview of Alice/cloud
transport: `docs/ALICE_INTEGRATION.md`. MQTT mapping of field
devices: `docs/contracts/alice-mqtt-mapping.md`. Gateway event
names that live in the **cloud** repo (`docs/contracts/alice-gateway.md`)
are cited, not restated.

Landed 1.0.6.37. Validating tests: `opt/sa02m-alice/tests/test_scenario_events.py`,
`opt/sa02m-alice/tests/test_cloud_control_api.py` (row `py-unit-alice`) and
`opt/sa02m-rules/tests/` — `test_engine.py`, `test_engine_v2.py`,
`test_security.py`, `test_logic_templates.py` (row `py-unit-rules`, since
1.0.6.39; before that no beat ran them).

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

Atomic JSON (`os.replace` + fsync, mode 0644). Default path overridable via
`SA02M_RULES_PATH`. Caps (`sa02m_rules.store`): `SCENARIOS_MAX=64` stored
scenarios on **every** write path — `replace` keeps the first 64, a batch
`upsert` (≤16 rows per call) or a single upsert that would grow the store
past 64 answers `too_many`; `MAX_WRITES=8` direct write actions
(`set`/`toggle`/`ramp`) per row — a row with more answers
`too_many_writes` (the engine's per-run cap is the **same constant**, so a
stored row can never half-apply; nested `scenario`/`scene` children count
toward the run's cap); `params` ≤ 4 KiB; `runs` 50; `notify_queue` 20.

Names that become MQTT topic segments are charset-validated at the store:
`device` matches `[A-Za-z0-9_.:-]{1,64}`, `cap` matches
`[A-Za-z0-9_.:-]{1,32}` (absent ⇒ `on_off`); a trigger, condition or
action failing either is dropped from the row. The engine re-checks both
on every write, whatever the caller (block, scene, logic template, `end`,
`Hub.set`), and the service never lets a refused publish escape
`on_boot()`/`tick()` (logged, not fatal).

`apply_command` verbs (the Socket.IO handler's single write path —
`sio_handlers._on_scenarios` → `config/api.py`; there is no CGI scenario
path):

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

`alice_expose` (scene only) and `captured_from {room_id, group_id}` are
validated and persisted but **accepted, not yet consumed**: nothing on the
board lists, exposes or reads them yet (open Operator decision, audit
1.0.6.39 A14). The hub must not present a scene marked `alice_expose` as
exposed to Alice.

---

## Engine v2

Non-blocking scheduler (heap of timers, cancel-by-key generations).
`RUN_S=30` counts **execution** only — scheduler waits are excluded. For
`type=code` it is a hard wall-clock deadline on the body itself
(`code_runner._Deadline`: `signal.setitimer`/SIGALRM on the daemon's main
thread, a `sys.monitoring` line clock elsewhere on CPython ≥ 3.12 — both
keep raising once expired, so a bare `except:` cannot ride them out; the
last-resort `sys.settrace` clock fires once, and `REARMING_MECHANISMS` in
`code_runner` is the one home of that boundary): an expired body aborts
with `last_error="timeout"` — journaled even when the body swallowed the
raise — and the engine keeps ticking. Memory:
`MemoryMax=32M`, bounded heap/rings/trackers (`HEAP_MAX=4096`).

Threading: the service applies MQTT messages on its main thread — paho's
network thread only enqueues (`INBOX_MAX=4096`; overflow is dropped and
logged) and `tick()` drains the queue in arrival order before timers.

Triggers (`kind`): `state`, `time`, `sun`, `boot`, `every` `{minutes}`,
`button` `{device, input?, gesture}`, `presence` arrive/leave on
`Vars.home_mode`.

State operators: level `== != > < >= <= changed`; edge
`rises_above` / `drops_below` / `enters_range` / `leaves_range` (fire
once per crossing); events `motion_detected` / `motion_cleared` /
`opened` / `closed`.

Button gestures: `single` / `long` / `double` from bridge counters
`di_N_short` / `di_N_long` / `di_N_double` (`docs/MQTT_TOPICS.md`) — a
gesture fires only on an observed **increment**; a decrease is a counter
reset (module reboot / firmware upgrade) and re-baselines silently; the
uint16 wrap 65535→small counts as one press. Edge-classifier fallback on
`di_N` fronts (long ≥ 500 ms, double ≤ 400 ms; classifier muted 10 s after
counters). `long_release` is classifier-only. No fire on service start.

Actions: `set` (optional `transition_s`), `toggle`, `ramp` (cancellable;
a direct write cancels), `delay`, `scenario`, `scene {id}`, `mode`
(sets persisted `Vars.home_mode`), `notify`, `http` (target policy below).

End: `{after_s, mode: "off"|"restore"}`. `off` turns off what the run
turned on; `restore` re-applies the pre-run snapshot; re-trigger resets.
End writes bypass the global write-rate window (8 writes / 10 s) so the
safety auto-off lands under unrelated traffic; a refused end write (bad
name) is journaled as `last_error="end write refused"`, never dropped
silently. The mirror control `end_after_s` carries `after_s` while the
timer is armed and `0` once it fired or a re-trigger reset it — it does
not count down (it was `remaining_s` in 1.0.6.37–38, a name that promised
a countdown the engine never published).

### Outbound HTTP (`http` action, sandbox `Http`)

One policy for both (`sa02m_rules/http_guard.py`): `http`/`https` only; the
host is resolved and **every** address must be public — loopback,
link-local, RFC1918/ULA, reserved, multicast and unspecified are refused,
as is a URL carrying `user:pass@`; a refusal is `last_error="http err
refused: …"` and the request is never sent. The operator may list hosts
that skip the address check in `/etc/sa02m-rules/http-allow.json`
(`{"hosts": ["192.168.1.50", "hooks.example"]}`; absent/malformed ⇒ empty,
override path `SA02M_RULES_HTTP_ALLOW`). The store applies the static half
(literal addresses, userinfo) at validation and drops such rows; the engine
re-checks with resolution at request time. Redirects: at most 3 hops, each
re-checked; response bodies are read up to 64 KiB and never returned to a
scenario (only the status); 5 s timeout. Known limit: the name is resolved
once for the check and again by the connect — a DNS answer that changes
between the two is not defended.

### Restricted Python (`type=code`)

Denylist sandbox on the AST: no import/class/lambda/async/yield/global,
no `_`-prefixed name or attribute, no `open`/`exec`/`eval`/`getattr`
family, and no `.format`/`.format_map` on any receiver (a format string
walks attributes the AST cannot see — `'{0._state}'.format(Hub)` reached
the whole object graph in 1.0.6.37–38; `%` formatting stays). Env:
`Hub` (`get`/`set` — names charset-checked, `MAX_WRITES` per run), `Cron`,
`Notify`, `Http` (policy above), `Vars`, `math` and a few builtins. Bound
by the `RUN_S` deadline above. Empty `__builtins__`.

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
topics, never `/on`): `rule_enabled`, `end_after_s`,
`blocked_by_switch`, plus template-specific fields. Writes from the
engine to field devices go to `<topic>/on` with **no retain**, same
rule as `mqtt_set.cgi`. Capabilities marked `writable: false` on the
Alice document are not written.

---

## Deploy / restart

Unit (`etc/systemd/system/sa02m-rules.service`): `User=root`,
`NoNewPrivileges`, `ProtectHome`, `PrivateTmp`, `ProtectSystem=strict` with
`ReadWritePaths=/etc/sa02m-rules` (the store is the only writable tree),
`MemoryMax=32M`; pinned by `test_security.py::UnitHardeningTests`. No
`WatchdogSec=` — the daemon does not link systemd, the time bound on
scenario code is in-process (above). A missing `paho-mqtt` (optional
install tier) is an intentional standby: `exit 0`, no restart storm.

`sa02m-rules` holds `/opt/sa02m-rules` in memory; `sa02m-cloud-control`
and `sa02m-alice-client` import `sa02m_rules.store`. After a `/opt`
refresh, restart the whole set — otherwise a cloud push silently drops
`trigger`/`end` (bench 1.0.6.37). Runbook: `docs/deployment.md`.
OTA/offline from **this** release emit the restart set; the **next**
update after 1.0.6.37 is the first one that actually consumes it
(previous runner applies the pack).
