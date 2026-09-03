# Contract: статус облачного агента на плате — `/run/sa02m-cloud-status.json`

Машинный контракт **формы статус-файла** агента `sa02m-cloud-agent`
(`opt/sa02m-cloud-agent/sa02m-cloud-agent.py`, `_write_status`) и того, что из
него читают `cloud.cgi` и карточка «Облако» (`www/network_config/static/js/cloud.js`).
Только локальная форма на плате. Семантика отказов облака — строки причин
`403`, маркеры frps в журнале frpc, правило «N отказов подряд одного вида»
и порог — живёт в контракте облака `docs/contracts/cloud-enrollment.md`
(облачный репозиторий; §2 heartbeat, §4 revoke) и здесь **не повторяется**:
где эта форма с ним соприкасается, ниже стоит ссылка.

## Файл и писатель

- Путь: `/run/sa02m-cloud-status.json` (tmpfs). После перезагрузки агент
  восстанавливает состояние stand-down из долговечного маркера в `agent.conf`
  (§Долговечный маркер).
- Писатель: только агент (root), `_write_status(state, **kw)`. **Файл
  переписывается целиком** при каждой записи — ключ прежнего состояния никогда
  не переживает смену состояния.
- Всегда присутствуют `state` (строка из перечня ниже) и `ts` (epoch момента
  записи); почти всегда `serial`.

## Перечень состояний

Одна строка — один дом перечня в этом документе (её читает валидирующий тест;
дом в коде — константа `STATUS_STATES` агента):

`state ∈ standby | pairing | pair_expired | already_claimed | claim_failed | enrolling | enroll_failed | active | revoked | unlinked | unlink_failed`

| `state` | Смысл | Ключи сверх `state` / `ts` / `serial` |
|---|---|---|
| `standby` | не привязана; ждёт запроса привязки (файл-триггер `pair_request`) или токена активации | — |
| `pairing` | код сопряжения запрошен, ждём привязки в облаке | `claim_code`, `device_id`, `expires_at` (epoch) |
| `pair_expired` | код истёк | `device_id` |
| `already_claimed` | облако ответило на запрос кода `409` (устройство числится за владельцем — cloud-enrollment §0.1) | `device_id`, `reason = "already claimed"`, `reason_class = "already_claimed"`, `since` (epoch последней попытки) |
| `claim_failed` | запрос кода не удался (облако недоступно или не `200`) | `device_id` |
| `enrolling` | привязка по токену активации идёт | `device_id` |
| `enroll_failed` | привязка по токену не удалась | `device_id` |
| `active` | **единственное живое состояние**: привязана, heartbeat идут | `device_id`, `tunnel`, `last_heartbeat` (epoch), `identity` (`present` / `absent`) |
| `revoked` | stand-down по отказу класса `revoked` | `reason`, `reason_class`, `unlinked_at`; `restored` — только после восстановления при старте |
| `unlinked` | stand-down по отказу класса `unlinked` или `unknown` | те же, что у `revoked` |
| `unlink_failed` | отказ подтверждён, но стереть привязку не удалось; агент повторяет попытку | `reason = "wipe_failed"`, `detail` (сырое сообщение ОС — только для журнала/отладки), `reason_class`, `refusal` |

### Правило «только в живом состоянии» (`LIVE_ONLY_KEYS`)

`tunnel`, `last_heartbeat`, `identity` пишутся **только** при `state = active`;
в любом другом состоянии писатель отбрасывает их по построению (иначе карточка
показывала «Туннель: Работает» на плате без привязки — стенд 1.135, 2026-09-03).
Тесты: `tests/test_revoke_standdown.py::test_status_writer_drops_live_only_keys_outside_active`,
`::test_every_non_active_writer_path_leaves_no_live_keys`.

### Поля stand-down

- `reason` — что именно сказал сервер, дословно: строка `error` НЕ-200 ответа
  heartbeat либо маркер frps из журнала frpc. Какие строки существуют и что они
  значат — cloud-enrollment §2 / §4; при `unlink_failed` — код `wipe_failed`.
- `reason_class` — `revoked` (владелец отозвал доступ) | `unlinked` (устройство
  отвязано/неизвестно облаку) | `unknown` (отказ по идентичности, класс не
  различим на плате); при `already_claimed` — `already_claimed`. Как строки
  сервера отображаются в классы — cloud-enrollment §4 (на плате — таблицы
  `HEARTBEAT_REFUSALS` / `FRPS_REFUSALS` агента, не пересказываются здесь).
- `unlinked_at` — ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`) момента stand-down.
- `restored: true` — статус восстановлен при старте агента из долговечного
  маркера (после перезагрузки `/run` пуст).
- `refusal` — при `unlink_failed`: та строка отказа, которая привела к
  stand-down (то, что было бы `reason` при удачном стирании).
- `detail` — при `unlink_failed`: сырое сообщение ОС; карточка его не
  показывает (`reason` даёт человеческую фразу через словарь `cloud.js`).

Тесты: `tests/test_revoke_standdown.py::test_stand_down_wipes_binding_and_writes_marker_and_status`,
`::test_stand_down_status_state_by_class`, `::test_failed_wipe_is_an_explicit_error_state_never_done`,
`::test_already_claimed_status_carries_a_reason_and_a_time`,
`::test_restore_stand_down_status_rebuilds_the_status_file`.

## Долговечный маркер и курсор журнала

- `agent.conf [cloud]`: `unlinked_at`, `unlinked_reason`, `unlinked_reason_text`
  (`STAND_DOWN_MARKER_KEYS`) — пишет stand-down, читает старт агента
  (`restore_stand_down_status`). Это идентичность, не конфигурация; кто и когда
  их стирает — `docs/contracts/image-identity-reset.md` §6 (не повторяется здесь).
- `/run/sa02m-cloud-frpc.cursor` — позиция чтения журнала frpc (`CURSOR_FILE`);
  там же, §6.

## Потребители

- `cloud.cgi` (GET) отдаёт файл **как есть** и добавляет только
  `service_active`, `service_enabled`, `has_token_file`, `server_reachable`;
  ключей статуса не переименовывает и не удаляет.
- Карточка «Облако» (`cloud.js`, `cloudRenderStatus`): бейдж «Соединение» по
  `CLOUD_STATE_MAP` (каждое состояние перечня имеет подпись; `activating` /
  `activation_failed` в карте — наследие старого агента, агент их не пишет);
  строки «Туннель» и «Последний отчёт» рисуются **только** при `active` и
  очищаются в любом другом состоянии; строка «Причина» — при `revoked`,
  `unlinked`, `unlink_failed`, `already_claimed`; кнопка привязки заблокирована
  при `unlink_failed`.

## Валидирующий тест

`opt/sa02m-cloud-agent/tests/test_status_contract.py::test_status_state_enum_matches_contract_and_card`
— перечень этого файла, константа `STATUS_STATES` агента, каждый литерал
`_write_status("…")` в исходнике вместе с двумя состояниями, выводимыми из
класса отказа, и ключи `CLOUD_STATE_MAP` в `cloud.js` сравниваются как
множества. Форма ключей по состояниям — тесты, названные в разделах выше.
