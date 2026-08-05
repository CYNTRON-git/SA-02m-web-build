# Контракт: полевая шина / BACnet MS/TP (web-сторона флешер-демона)

Домашний адрес веб-контракта на **выбор полевого протокола модуля (рег. 122),
проверку живости BACnet MS/TP и возврат в Modbus** через флешер-демон
(`opt/sa02m-flasher`). Машинная грамматика (маршруты, JSON-поля, значения,
enum, имена BACnet-объектов) — на английском (`PROTOCOL.md` invariant 5);
пояснения — на русском (`docLanguage: ru`).

Это контракт **web-стороны**. Он **не дублирует** он-wire раскладку регистра 122,
MAC-клампы, наборы скоростей, авто-reboot и правила приёмника CRC — они живут в
контрактах прошивок (см. «Источник истины»). Веб пишет рег. 122 и говорит по
MS/TP **строго** по этим контрактам; любое расхождение — находка ревью.

Реализация: `sa02m_flasher/bus_mode.py` (чистая логика регистра/значений),
`sa02m_flasher/bacnet_mstp.py` (MS/TP-кодек: пассивный сниф + ring-join WP/RP),
`sa02m_flasher/device_config.py` (3-state рег. 122 в снимке/записи),
`sa02m_flasher/runner.py` (задачи verify/recover под port-lease),
`sa02m_flasher/service.py` (маршруты). UI — `www/network_config/static/js/flasher.js`.

## Область (Phase 1)

Селектор + verify + recover. **Вне области:** router BACnet/IP↔MS/TP, poller,
чтение объектов ввода-вывода по BACnet, любая правка MQTT-моста, BACnet-поверхность
для CE-02m-3. Полный разбор фаз — план `.ai-dev/plans/bacnet-mstp-web-integration.md`.

## Селектор полевой шины (§5.1)

Рег. **122** — family-common 3-state селектор: `0` classic Modbus · `1` Fast
Modbus (заводской дефолт) · `2` BACnet MS/TP. Значения и семантика идентичны у MR
и DTV; per-family отличается только текст диалога и vendor id (MR `260` / DTV `381`).

- **Показывается только** для семейств с реальным регистром: `family ∈ {mr, dtv}`
  (`bus_mode.selector_supported`). Для **CE-02m-3, WB, bootloader** — скрыт
  (снимок отдаёт `bus_mode_supported: false`). CE-семейство BACnet **не** предлагает.
- Чтение вне диапазона санитизируется в заводской дефолт (`1` Fast, fail-closed) —
  UI не показывает фантомное состояние.
- Переход в `2` (BACnet) требует диалога подтверждения, **называющего путь
  восстановления ДО записи** (flasher-контракт). Значения `0`/`1` — live-переход
  без ребута; `2` персистится и вызывает отложенный ребут прошивки.

## Честность про inert-сборку (§5.3)

После записи `122 = 2` UI считает окно ≥ 6 с состоянием «применяется», затем
перечитывает состояние (`bus_mode.bacnet_switch_verdict`):

| Отклик по Modbus | рег. 122 | Вердикт | Сообщение |
|---|---|---|---|
| нет (тишина) | — | `applied` | переключён в BACnet MS/TP (это ожидаемо) |
| есть | `2` | `inert` | прошивка без поддержки BACnet / не применилось |
| есть | `≠ 2` | `not_applied` | переключение не легло, модуль остался на Modbus |

Ложный успех при оставшемся на Modbus модуле не выдаётся никогда.

## Проверка BACnet — verify (§5.2)

Пассивный сниф MS/TP на арендованном порту, **только чтение, RX-only**. MR — фикс
`38400 8N1`; DTV — сохранённая скорость (дефолт `38400`). ≥ 1 CRC-валидный
8-байтный заголовок ⇒ «MS/TP жив». Тишина и ошибки открытия порта **сообщаются**,
не проглатываются как ложный «alive».

## Возврат в Modbus — recover (§5.4)

Два пути, выбор по факту отклика модуля по Modbus (`bus_mode.force_modbus_recovery_plan`):

- **Окно pending** (модуль ещё отвечает по Modbus) → прямая запись FC06 рег. 122 =
  целевое значение (MR — валидное прошлое не-BACnet, иначе `0`; DTV → `0`).
- **Залатчен в BACnet** (по Modbus молчит) → ring-join WriteProperty по MS/TP:
  - MR: `MSV:1` present-value = `mode + 1` (unsigned) — путь подтверждён
    `../MR-02m/scripts/hw/bacnet_recover.py`;
  - DTV: `AV:122` present-value = `0` (REAL, ASHRAE-тип Analog Value) — точная
    приёмная декодировка на проводе **проверяется на стенде** (§7 плана, риск §9.1);
  затем ждём окно отложенного сброса и подтверждаем возврат на Modbus.

Предусловия (fail-closed): порт арендован (СА-02м владеет сегментом; без аренды —
без передачи), по одному модулю за раз (в диалоге). Ring-join инжектит фейковую
станцию — за вторым явным подтверждением. Если модуль не вернулся — сообщается
путь физической кнопки сброса на устройстве.

## Маршруты (демон, за nginx `/api/flasher/*`)

Все за той же session-аутентификацией и той же port-lease (flock + stop/restore
managed-services + `flasher_busy`-гейтинг), что scan/flash/device_config.

| Маршрут | Тип | Тело | Ответ |
|---|---|---|---|
| `POST /bus_mode` | синхронный (leased) | `{port, device, mode∈{0,1,2}}` | `{ok, requested_mode, prior_mode, reboot_pending, snapshot?}` |
| `POST /bacnet/verify` | задача (SSE) | `{port, device, family, duration_s?}` | `{job_id}`; итог в `job.result` |
| `POST /bacnet/recover` | задача (SSE) | `{port, device, family, address, prior_mode}` | `{job_id}`; итог в `job.result` |

Прогресс задач verify/recover — по существующему SSE
(`GET /api/flasher/jobs/<id>/events`); финальный `job.result` переживает
переподключение (F5) и читается из снимка `GET /jobs/<id>`.

## Идиома ошибок (HTTP-200 + `ok:false` / коды состояний)

Как в `mqtt-set-endpoint.md`: транспорт остаётся управляемым, ошибка — в теле.
Синхронный `POST /bus_mode`:

- невалидное семейство/значение / порт занят / gateway-lock / ошибка открытия —
  отказ с названной причиной (`ValueError → 400`, `RuntimeError → 409`), тело
  `{error: "<причина>"}`.
- успех — `{ok: true, ...}`.

Задачи verify/recover: приём — `{job_id}`; исход (жив/тишина/восстановлен/не
вернулся/ошибка порта) — в `job.result`, тишина и ошибки не выдаются за успех.

## Безопасность (кратко; модель — `docs/threat-model.md`)

Auth-first до любой работы с портом. Untrusted input: `port` → allow-list из
`ports_map`; `family` → enum `{mr, dtv}`; `mode`/PV → enum; `address` → 1..247
(BACnet MAC — клампится 1..127); `duration_s` → bounded int. MS/TP TX-кадры —
только из валидированных enum; опасная операция (ring-join) — за подтверждением и
только на арендованном порту (без аренды — без передачи; сниф RX-only). Без
конструкции shell, без пути из входов запроса, без новых секретов/слушателей
(unix-socket). Все serial-операции и ожидание WP — с явными таймаутами под
`MAX_JOB_SECONDS`.

## Источник истины (он-wire детали — НЕ дублировать здесь)

- `../MR-02m/docs/contracts/bus-protocol.md` — рег. 122 (3-state), vendor `260`,
  MSV:1 «Bus Protocol» (PV = mode+1), inert `WITH_BACNET`-сборка, gap восстановления MR.
- `../cyntron-dtv/docs/contracts/bus-protocol.md` + `bacnet-objects.md` — family-common
  рег. 122 (DTV, 1.0.1.52; рег. 127 retired), vendor `381`, объект `AV:122`, авто-reboot,
  factory reset → Modbus.
- `../MR-02m-flasher/docs/contracts/bus-mode-selector.md` — UX-зеркало (селектор,
  диалог подтверждения, матрица восстановления), которое повторяет этот веб.
- `docs/contracts/rs485-roster.md` — §5.5 добавляет только one-click-предложение
  снифа при пустом скане; формат ростера не меняется.
