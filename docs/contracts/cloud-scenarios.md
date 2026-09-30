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
`test_security.py`, `test_logic_templates.py`, `test_store_hardening.py`
(row `py-unit-rules`, since 1.0.6.39; before that no beat ran them).

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

**Two files, one view (1.0.6.41).** The document (`scenarios.json`) holds
scenarios, library and vars and is written only when that content changes
(a cloud command, a `mode` action / `Vars.home_mode`). Run state — `runs`,
`notify_queue`, per-scenario `last_run` / `last_error` — lives in the
sibling journal **`/etc/sa02m-rules/runs.json`** (`store.journal_path`; the
unit's `ReadWritePaths` tree is the only writable one under
`ProtectSystem=strict`, so the journal needs no second tree). The engine
buffers run state in memory (`store.Journal`) and flushes the journal
read-modify-write on whichever comes first — `RUNS_FLUSH_S=5` s after the
first unflushed record or `RUNS_FLUSH_MAX=32` pending records — plus
immediately after `run_now` (the cloud reads the store as soon as the
`.run` flag is consumed) and at shutdown (SIGTERM / `RulesApp.stop()`). A
scenario run therefore costs no document write, never bumps the document's
mtime and never triggers the service's reload. **What a crash can lose:** at
most the last `RUNS_FLUSH_S` seconds of run records / notify entries (a
`run_now` result is already on disk). A journal that is missing, corrupt or
unwritable never blocks scenarios: readers see empty `runs` /
`notify_queue`, the engine keeps its bounded buffer and retries at the next
cadence, the next successful flush rewrites the file. Readers (`load` →
`listed()` / `_ok()`) merge the journal into the document view, so the
cloud-facing shapes below are unchanged; the view lags the engine's buffer
by at most `RUNS_FLUSH_S`. `ack_notify` drains the journal's queue in place
and the engine's next flush re-reads the file, so drained entries do not
come back. A pre-1.0.6.41 document still carrying `runs` / `notify_queue` /
`last_*` inside is migrated once at engine boot (journal written first, then
the stripped document); until then a reader shows the legacy fields, and the
journal is truth once it exists.

Names that become MQTT topic segments are charset-validated at the store:
`device` matches `[A-Za-z0-9_.:-]{1,64}`, `cap` matches
`[A-Za-z0-9_.:-]{1,32}` (absent ⇒ `on_off`); a trigger, condition or
action failing either is dropped from the row and named in the answer
(§«Проверка при сохранении» below). The engine re-checks both
on every write, whatever the caller (block, scene, logic template, `end`,
`Hub.set`), and the service re-checks them once more where the topic is
actually built (`RulesApp.pub` — the last line before the wire, reached by
both the `block` and the `code` path): a failing pair is logged and
dropped, never published. A publish the broker refuses never escapes
`on_boot()`/`tick()` either (logged, not fatal).

`apply_command` verbs (the Socket.IO handler's single write path —
`sio_handlers._on_scenarios` → `config/api.py`; there is no CGI scenario
path):

| Body | Effect |
|---|---|
| `{library}` (no `name`/`id`) | replace the JS library string (≤16 KiB) |
| `{replace:true, scenarios:[…]}` | full replace; **all** rows validated first — one refused row refuses the call, nothing written |
| `{id, delete:true}` | delete one |
| `{id, get:true}` | full document of one scenario (list rows are summaries) |
| `{upsert:[…]}` | batch upsert, validate **all** first (never half-written) |
| `{ack_notify:true}` | drain `notify_queue` |
| `{id, run_now:true}` | flag file `<path>.run`; wait up to 2.5 s for the engine tick |
| other object with `name` | single upsert |
| `{id, …}` of an existing scenario, none of `trigger`/`condition`/`action`/`code`/`template`/`params` | partial update (§«Частичное обновление») |

Success payload always carries `ok`, `scenarios` (listed summaries),
`runs` (last 20), `notify_queue`, `library`.

Listed row: `id`, `name`, `enabled`, `type`, `order`, `last_run`,
`last_error`, `summary`, plus provenance when present (`template_id`,
`template_version`, `source`, `params`).

Types: `block` | `code` | `logic` | `scene`. `scene` is set-only (no
nested scenario/scene/delay that would recurse).

`alice_expose` (scene only) and `captured_from {room_id, group_id}` are
**consumed since 1.0.6.41**: an exposed, enabled scene becomes a virtual
switch in the Yandex account, placed in the room `captured_from.room_id`
names. The projection, the device shape and the lifecycle are the Alice
contract's (`alice-mqtt-mapping.md` §Scene devices) — one home; the store's
job is unchanged (validate, persist, one writer). `captured_from.group_id`
is still **stored and not read on the board**: it is cloud-side provenance of
a group capture. The hub **may** present a scene marked `alice_expose` as
exposed to Alice.

### Проверка при сохранении

Единственный источник правил, по которым плата принимает сценарий; проверка
редактора в облаке перед сохранением ссылается сюда и не ведёт своей копии.
Код — `sa02m_rules.store` (`validate_row`, `_checked_row`).

**Отказ сохранения.** Если в теле пришёл непустой список `trigger`, `action`
или условий (`condition.all` / `condition.any`), а после очистки в нём не
осталось ни одного элемента, сценарий не сохраняется — плата отвечает:

```json
{"ok": false, "error": "invalid_elements",
 "dropped": [{"part": "trigger", "index": 0, "reason": "bad_cap"}]}
```

Для пакетных `upsert` и `replace` отказ одной строки отменяет весь вызов
(ничего не записано), в ответе добавляется `"row": <позиция строки в
запросе>`. Значение `trigger`/`action`, которое не является списком, считается
непустым и выброшенным целиком (`index: null`, `reason: "not_list"`); так же
`condition`, которое не является объектом (`part: "condition"`, `index: null`,
`reason: "not_object"`). Отсутствующие, `null`, `{}` и пустые `all`/`any`
условия — не отказ: у сценария просто нет условий. Опустевший список условий
отклоняется потому, что сохранённый без условий сценарий срабатывал бы чаще,
чем задумано.

**Частично годный список** сохраняется без выброшенных элементов, а
успешный ответ несёт `dropped` с тем же форматом; в ответе пакета у каждого
элемента есть `row`. Ответ без выброшенных элементов ключа
`dropped` не содержит.

Элемент `dropped`: `part` — `trigger` | `condition` | `action`; `index` —
позиция элемента в присланном списке (для условий — в списке `all`/`any`);
`reason` — код из таблиц ниже.

Общие причины: `not_object` — элемент не объект; `unknown_kind` — неизвестный
`kind`; `over_limit` — первый элемент сверх лимита строки (триггеров 8,
условий 8, действий 20): одна запись на список, остаток списка не
просматривается, так что размер ответа ограничен при любом размере запроса.
Лимит записей `set`/`toggle`/`ramp` в строке — `MAX_WRITES=8`; превышение
отклоняет всё сохранение (`too_many_writes`, без `dropped`).

Числа: `NaN`, `±Infinity` (парсер JSON на плате их принимает) и логические
значения числом не считаются. Поле, для которого в таблице указано значение
по умолчанию («нет ⇒ …», «не число ⇒ …»), получает его; поле без такого
значения выбрасывает элемент с `bad_value`. Ни одно числовое поле не
приводит к ответу `error: "internal"`. Поля строки: `order` не число ⇒ 0,
`template_version` ⇒ 1, `end.after_s` вне 60..14400 или не число ⇒ `end` не
хранится.

Поля, общие для адресных элементов: `device` — `[A-Za-z0-9_.:-]{1,64}`
(`bad_device`); `cap` — `[A-Za-z0-9_.:-]{1,32}`, отсутствует ⇒ `on_off`
(`bad_cap`); `instance` — необязателен, тот же набор символов, что у `cap`
(`bad_instance`); смысл `instance` — §«Цель записи».

Триггеры:

| `kind` | Обязательно | Допустимо / как хранится | Причины |
|---|---|---|---|
| `state` | `device` | `cap`, `instance`; `op` ∈ `== != > < >= <= changed rises_above drops_below enters_range leaves_range motion_detected motion_cleared opened closed`, неизвестный ⇒ `==`; `value` для `enters_range`/`leaves_range` — `{min, max}` из чисел (обязательно); для `motion_*`/`opened`/`closed` не хранится; для остальных — любое значение, не проверяется (отсутствует ⇒ `null`) | `bad_device`, `bad_cap`, `bad_instance`, `bad_value` |
| `time` | `at` — `HH:MM` | `days` — список 0..6 (пн = 0), прочие значения отбрасываются | `bad_value` |
| `sun` | — | `event` `sunrise` \| `sunset` (иное ⇒ `sunrise`); `offset` — минуты, обрезается до −180..180 (не число ⇒ 0); `lat` −90..90 и `lon` −180..180 — числа, необязательны; `days` — как у `time` | `bad_value` (`lat`/`lon` не число или вне диапазона) |
| `boot` | — | — | — |
| `every` | `minutes` 1..60 | — | `bad_value` |
| `button` | `device`, `gesture` ∈ `single double long long_release` | `input` `di_N` (неверный игнорируется) | `bad_device`, `bad_value` |
| `presence` | `event` `arrive` \| `leave` | — | `bad_value` |

Условия (`{"all": […]}` или `{"any": […]}`):

| `kind` | Обязательно | Допустимо / как хранится | Причины |
|---|---|---|---|
| `time_window` | `preset` `any`/`day`/`night` или `from` + `to` в `HH:MM` | — | `bad_value` |
| `weekday` | непустой `days` (0..6) или `preset` `workday`/`weekend` | — | `bad_value` |
| `mode` | `value` ∈ `home away night holiday` | — | `bad_value` |
| `state` | `device` | как триггер `state`, но без `changed` (⇒ `==`); `rises_above` ⇒ `>`, `drops_below` ⇒ `<`, `motion_detected`/`opened` ⇒ `== 1`, `motion_cleared`/`closed` ⇒ `== 0`; `for_s` ≥ 1 (до 86400) | `bad_device`, `bad_cap`, `bad_instance`, `bad_value` |

Действия (в `scene` допустим только `set`, остальные — `not_in_scene`):

| `kind` | Обязательно | Допустимо / как хранится | Причины |
|---|---|---|---|
| `set` | `device` | `cap`, `instance`; `value` — любое значение, не проверяется; `transition_s` > 0 ⇒ до 300 | `bad_device`, `bad_cap`, `bad_instance`, `unknown_target`, `ambiguous_target` |
| `toggle` | `device` | `cap`, `instance` | как у `set` |
| `ramp` | `device`, `to` (число), `seconds` 1..300 | `cap`, `instance` | как у `set` + `bad_value` |
| `delay` | — | `seconds` — число, приводится к целому и обрезается до 1..300 (нет ⇒ 1) | `bad_value` |
| `scenario`, `scene` | `id` — `[A-Za-z0-9_.:-]{1,64}` | — | `bad_id` |
| `mode` | `value` ∈ `home away night holiday` | — | `bad_value` |
| `notify` | непустой `text` | обрезается до 240 символов | `bad_value` |
| `http` | `url` по политике §Outbound HTTP (статическая часть) | `method` `GET` \| `POST`; `body` до 2000 символов | `bad_url` |

### Цель записи: `device`, `cap`, `instance`

Действия `set`/`toggle`/`ramp` при сохранении сверяются с документом
устройств Алисы (`/etc/sa02m-alice/sa02m-alice-devices.conf`, переопределение
`SA02M_ALICE_DEVICES`; код — `sa02m_rules/device_index.py`). Цель записи —
только **умение** (`capabilities`) устройства; свойства (`properties`,
датчики) целью записи не бывают:

- `cap` без `instance` находит умение этого типа, если оно у устройства
  **одно**. Если умений типа несколько (у Carel `range` — скорость
  вентилятора и уставка), цель без `instance` неоднозначна —
  `ambiguous_target`;
- `cap` + `instance` находит умение этого типа с таким
  `parameters.instance` — тем самым, которое облако берёт из документа
  устройства при сборке сценария (`{"kind":"set","device":…,"cap":"range",
  "instance":<parameters.instance>,"value":…}`);
- не найдено — `unknown_target` (устройства нет в документе, у него нет
  такого умения или такого `instance`).

Документ не читается (нет файла, битый JSON, не объект с `devices`) —
проверка целей пропускается, остальные правила действуют; предупреждение
пишется в журнал один раз, пока документ не станет читаемым. Читаемый
документ без устройств (`{"devices": []}`) — не «нечитаемый»: любая цель
записи в нём `unknown_target`.

**При выполнении** то же правило держит сервис (`RulesApp.pub`): для
устройства из документа неоднозначный тип без `instance` и неизвестный
`instance` **не публикуются** — запуск получает `last_error`
`"ambiguous target"` / `"unknown target"`, никакого угадывания. Это верно для
`set`, `toggle`, `ramp` и `set` с `transition_s` (плавное изменение
отклоняется до первого шага); отказанная запись события `end` журналируется
как `"end write refused"` (§Engine v2). `Hub.set` в `type=code` не
принимает `instance`: такая запись в неоднозначный тип — ошибка запуска.
Устройство, которого нет в документе, и не перечисленный в документе `cap`
известного устройства (оба — без `instance`) публикуются по сырому топику
`/devices/<device>/controls/<cap>/on`; такие цели бывают только в строках,
сохранённых до проверки целей или пока документ не читался.

Логические шаблоны называют умение именем его `instance` (`brightness`):
имя указывает на умение `<тип>|<instance>`, если этот `instance` носит ровно
одно умение устройства и у устройства нет умения-типа или свойства с тем же
именем (у Carel `temperature` — и уставка, и датчик: имя остаётся датчиком).
Так память яркости и диммирование `switch_light` пишут в умение `range` с
`parameters.instance: brightness`.

**Чтение состояния.** Сообщение на топике из документа обновляет все ключи,
привязанные к нему: тип умения (если умение этого типа одно),
`<тип>|<instance>` для умения с `instance` и `instance` свойства
(`temperature`, `motion`, …). Триггер/условие `state` с `instance` реагирует
только на своё умение; неоднозначный тип без `instance` не срабатывает
никогда. Свойство адресуется `cap` = его `instance` (так облако и собирает
триггеры по датчикам). Топик, привязанный в документе только как свойство,
вдобавок доходит до сырого пути — `device`/`cap` = имя устройства и контрола
MQTT: на нём работают счётчики кнопок `di_N_*`, классификатор фронтов `di_N`
и триггеры по сырому имени. Топик умения обновляет сырое имя в зеркале
состояния без срабатываний.

### Частичное обновление

Одиночное сохранение с `id` существующего сценария, в котором нет ни одного
из `trigger`, `condition`, `action`, `code`, `template`, `params` (`null` —
то же, что «не прислан»), меняет только присланные ключи (`name`, `enabled`,
`order`, `type`, `end`, …) и сохраняет остальное тело сценария — так кнопка
«включить/выключить» облака (`{id, name, enabled, type}`) не трогает
триггеры и действия. `name` в частичном обновлении не обязателен. Тело, в
котором хотя бы один из этих ключей не `null`, — полная замена строки. Цели
записи при частичном обновлении повторно не сверяются (документ устройств
мог измениться — выключение сценария это не блокирует). Строки пакетных
`upsert` и `replace` — всегда полные тела: частичного обновления в пакете
нет.

---

## Engine v2

Linked from the cloud's own contract («State operators», cloud commit `5e26fff`,
cloud #125): renaming this heading or the «State operators» paragraph below
breaks that link — tell the cloud session first.

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
`Vars.home_mode`. Fields and limits of each kind: §«Проверка при сохранении».

`sun` считается по собственным `lat`/`lon` триггера, если они есть, иначе по
координатам платы (`/etc/sa02m-alice/location.json`, затем
`/etc/sa02m/location.json`, затем `SA02M_LAT`/`SA02M_LON`); `days` (0..6,
пн = 0) ограничивает дни недели так же, как у `time`.

State operators: level `== != > < >= <=` (a level holds on every value,
including the first one observed after the engine starts); edge `changed` /
`rises_above` / `drops_below` / `enters_range` / `leaves_range` (fire once
per crossing). `changed`: the first value observed after an engine start (the
retained MQTT snapshot) is the baseline and never fires — only values arriving
on the state topics count as observed, never the engine's own writes (a `boot`
scenario that sets the device before its snapshot arrives does not make that
snapshot a change); the trigger fires on each later value that differs from the
previous one. So the echo of a `boot` write depends on arrival order at start:
boot write → snapshot → the bridge's echo of the write fires `changed` once
(snapshot → echo is a real observed change); snapshot → boot write → echo
fires nothing (the echo equals the engine's mirror and is dropped as a repeat).
Version-scoped: boards below 1.0.6.54 evaluate `changed` as a level that
always holds, so every engine start (service restart, update, reboot) fires
every `changed` scenario once. Events `motion_detected` / `motion_cleared` /
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

One policy for both (`sa02m_rules/http_guard.py`), **aligned with the cloud
half** (decision 2026-09-09 — the two must refuse the same set, or a
scenario the cloud accepts dies silently on the board).

**Refused** — the request is never sent, `last_error="http err refused: …"`:
a scheme other than `http`/`https`; a URL longer than **500 characters**
(judged before any resolve); `user:pass@` (whatever the target, including an
allow-listed one); loopback, link-local (169.254.169.254 included),
multicast, reserved, unspecified and `0.0.0.0/8` addresses; the name
`localhost` and the whole `.localhost` tree. Addresses are recognised in
every spelling a resolver accepts, so `2130706433`, `0177.0.0.1`, `127.1`,
`[::1]` and `[::ffff:127.0.0.1]` are all caught as loopback. An IPv6 form
that CARRIES an IPv4 address — mapped, 6to4 (`2002::/16`) and Teredo
(`2001::/32`) — is judged on the address it carries, so `[2002:7f00:1::]`
is loopback while a wrapped LAN address stays allowed.

**Allowed:** the operator's own LAN — RFC1918 and IPv6 ULA targets (a NAS, a
panel, another board). Reaching an unintended LAN service is accepted risk;
the compensating fences are the ones below.

**Fences the cloud half does not have:** the host is RESOLVED and the answer
judged (a public name aimed at loopback is refused), and every redirect hop
is re-checked — at most 3 hops. **At most 8 requests per engine run**
(`store.HTTP_PER_RUN_MAX`), shared by the `http` action and the sandbox
`Http` and spanning nested `scenario`/`scene` children: the 9th is refused
with `last_error="http cap"` / `"http err http cap"`. Stated exactly: the
cap counts *calls*, a policy-refused call spends the budget like any other
(so a run cannot probe by looping over refused targets), and each call may
still follow up to 3 redirect hops — 32 network requests is the arithmetic
ceiling of one run. Response bodies are read up to 64 KiB and never
returned to a scenario (only the status); 5 s timeout.

The operator allow-list `/etc/sa02m-rules/http-allow.json`
(`{"hosts": ["127.0.0.1", "hooks.example"]}`; absent/malformed ⇒ empty,
override path `SA02M_RULES_HTTP_ALLOW`) is now the way to **permit** a
refused target — a loopback or link-local host named deliberately.

The store applies the static half (no DNS: literal addresses in any
spelling, the loopback names, userinfo, length, scheme) at validation and
drops such actions from the row; the engine re-checks with resolution at
request time. Known limit: the name is resolved once for the check and again
by the connect — a DNS answer that changes between the two is not defended.

### Restricted Python (`type=code`)

Denylist sandbox on the AST: no import/class/lambda/async/yield/global,
no `_`-prefixed name or attribute, no `open`/`exec`/`eval`/`getattr`
family, and no `.format`/`.format_map` on any receiver (a format string
walks attributes the AST cannot see — `'{0._state}'.format(Hub)` reached
the whole object graph in 1.0.6.37–38; `%` formatting stays). Env:
`Hub` (`get`/`set` — names charset-checked, `MAX_WRITES` per run), `Cron`,
`Notify`, `Http` (policy above, `HTTP_PER_RUN_MAX` per run), `Vars`, `math`
and a few builtins. Bound by the `RUN_S` deadline above. Empty
`__builtins__`.

The shared `library` and the scenario body execute in **one** namespace, so
a function defined in the library sees the env (`Hub`, `Notify`, …) and the
library's other functions — as does a comprehension in the body. Before
1.0.6.41 they were separate globals/locals and any such call raised
`name 'Notify' is not defined`.

Conditions: `time_window` presets `day`/`night` (sun), weekday
`days`/`workday`/`weekend`, `mode` via `Vars.home_mode`, `state` `for_s`.

Unknown `logic` template names are **stored** (the cloud catalog may be
newer) and rejected at run time with `last_error="unknown template"`.

### Logic templates (this engine)

`switch_light`, `motion_light`, `circadian`, `thermostat`,
`humidity_fan`, `co2_ventilation`, `humidifier`, `away_home`
(`sa02m_rules/logic_templates.py`). Cloud catalog ids that are not in
this tuple stay in the store and do not run.

`switch_light` и фиксируемый выключатель: при `switch_type` `latching` или
`auto` изменение `on_off` выключателя **ставит группу света в положение
выключателя** — 1 включает (яркость по `memory`: последняя яркость диммера,
`memory: false` — без записи яркости; яркость пишется, если у светильника
есть умение яркости — §«Цель записи»), 0 выключает. Светильник, который уже
в нужном положении, не трогается: свет, включённый из приложения, остаётся
включённым, когда выключатель переходит в 1. Первое значение после старта
(снимок MQTT) и повтор того же значения ничего не делают. Кнопочный
выключатель (жесты `di_N`, в том числе при `auto`): одиночное
нажатие переключает группу, двойное выключает, долгое — диммирование.

---

## MQTT mirror

Virtual device `/devices/sa02m-rules-<id>/controls/*`: retained STATE the
engine publishes — `rule_enabled`, `end_after_s`, `blocked_by_switch`, plus
template-specific fields — never written by anyone else. Writes from the
engine to field devices go to `<topic>/on` with **no retain**, same
rule as `mqtt_set.cgi`. Capabilities marked `writable: false` on the
Alice document are not written.

**`run` — the ONE command control (1.0.6.41).** `…/controls/run/on` (no
retain) is the single inbound topic under this prefix and the seam the Alice
scene switch commands through (`alice-mqtt-mapping.md` §Scene devices):

| Payload | Effect |
|---|---|
| `1` / `on` / `true` | run the scene — `run_now`, `source="external"` |
| anything else | switch off every `on_off` output the scene's definition sets to a truthy value, and cancel a pending `end` (`end_after_s` → 0) |

Both verbs answer **only for a row that exists, is `scene`-typed and
enabled** — a `block`/`logic`/`code` id, an unknown id or a disabled scene is
a no-op, so the LAN reach of this topic is narrower than the cloud's
type-agnostic `run_now`. Both spend the same `MAX_WRITES` / rate window as
any run, and journal a record (`source="external"`; the off verb carries
`reason: "off"`). Off does NOT replay a `restore`-mode scene's pre-run
snapshot: that snapshot lives in the end timer this cancels, and «выключи»
means off. `RulesApp.pub` **refuses** to publish any `/on` under
`sa02m-rules-*`, so a stored action naming a scenario device cannot command
the engine through the broker.

**Availability gap, stated:** with `sa02m-rules` down, a `run/on` publish
lands on the broker, nobody consumes it, and the caller (Alice) has already
been told `DONE`. An LWT-backed `/devices/sa02m-rules-<id>/meta/error` is the
fix; the device-level error flag is already honoured by the Alice registry,
so the hook exists and only the publisher is missing.

---

## Deploy / restart

Unit (`etc/systemd/system/sa02m-rules.service`): `User=root`,
`NoNewPrivileges`, `ProtectHome`, `PrivateTmp`, `ProtectSystem=strict` with
`ReadWritePaths=/etc/sa02m-rules` (the store — document and journal — is the
only writable tree),
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
