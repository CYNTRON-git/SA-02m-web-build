# Контракт: устройство `type: template` (Modbus→MQTT мост)

Домашний адрес контракта для данных-управляемого типа устройства `template` в
`/etc/sa02m-modbus-mqtt.yaml`. Мост читает JSON-шаблон в WB-формате, разворачивает
его каналы в список опроса и публикует их по тем же конвенциям Wiren Board MQTT,
что и штатные семейства `mr02m`/`dtv`/`ce02m3` — без ручной карты регистров.
Машинная грамматика (имена и формы JSON, схема шаблона) — на английском
(`PROTOCOL.md` invariant 5); прозаические пояснения — на русском.

**Только механизм (решение Оператора 2026-08-17).** Мост читает любой JSON,
лежащий в каталоге шаблонов; ни один файл-шаблон Wiren Board в репозиторий НЕ
вкладывается — их LICENSE ограничивает использование оборудованием Wiren Board,
а SA-02m — это Allwinner A40i. Шаблоны реальных устройств поставляет интегратор
(см. `opt/sa02m-modbus-mqtt/templates/README.md`).

Реализация: `opt/sa02m-modbus-mqtt/bridge_template.py` (`TemplatePoller`);
регистрация типа: `POLLER_CLASSES["template"]`
(`opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`).

## 1. YAML-запись устройства

```yaml
- id: example-COM5-30       # MQTT device id (/devices/<id>/...)
  type: template
  template: example         # bare name → templates/config-<name>.json
  port: /dev/COM5
  baudrate: 9600            # WB default line is 9600; per device
  address: 30               # Modbus slave address
  name: "Example (COM5 addr=30)"
  poll_s: 2                 # base poll cadence for the template channels
  # optional availability keys inherited from DevicePoller:
  # offline_after_fails, backoff_base_s, backoff_max_s
```

`template` — **bare name**; резолвер ищет `config-<name>.json`, затем
`<name>.json` в каталоге шаблонов. Каталог: `templates/` рядом с
`bridge_template.py` (на устройстве `/opt/sa02m-modbus-mqtt/templates/`),
переопределяется переменной окружения `SA02M_WB_TEMPLATES_DIR` (тест-шов).

## 2. Резолвер пути и безопасность обхода каталога

`_resolve_template_path(name)` — **fail-closed**. Значение `template:` приходит
из YAML, записываемого веб-CGI, и считается недоверенным. Правила:

- допускается только имя `^[A-Za-z0-9._-]+$` (без `/`, без `..`, не абсолютное);
- итоговый путь обязан лежать строго внутри каталога шаблонов (проверка
  `realpath` — родитель разрешённого файла == каталог шаблонов);
- имя, не разрешаемое в существующий файл, → устройство **пропускается** с
  записью ERROR в лог; остальные устройства продолжают опрос (fail-open для
  флота, fail-closed для плохого устройства — оно не публикует ничего).

## 3. Поддерживаемое подмножество схемы (v1)

Каждый канал берётся из `device.channels` (плоский список; вложенная группа с
собственными `channels` разворачивается на один уровень).

| Поле | Значения v1 | По умолчанию |
| --- | --- | --- |
| `reg_type` | `coil`, `discrete`, `input`, `holding` | — (обязательно) |
| `address` | целое (номер регистра/бита) | — (обязательно) |
| `format` | `u16`, `s16`, `u32`, `s32`, `float` (для `input`/`holding`) | `u16` |
| `word_order` | `big_endian`, `little_endian` (для 32-бит) | `big_endian` |
| `scale` | число | `1` |
| `offset` | число | `0` |
| `units` | строка (WB units → авто-precision) | `""` |
| `type` | WB control type (`value`/`switch`/`temperature`/…) | `value` (бит: `switch`) |
| `readonly` | bool | `true` |
| `enabled` | bool (`false` — канал исключён молча) | `true` |

- `coil`/`discrete` — 1 бит, `format` игнорируется, значение `0`/`1`.
- 32-бит `u32`/`s32`/`float` читают 2 слова; `big_endian` = слово по младшему
  адресу является старшим словом.
- Значение = `raw * scale + offset`; целочисленный формат без scale/offset
  публикуется как целое, иначе округляется по WB-precision единиц.
- `device.setup` — список инициализирующих записей в holding-регистры
  (`{address, value}`, только целые из самого файла-шаблона — доверенный вход,
  никогда не из запроса/пользователя).
- Записываемый канал: `holding`/`coil` с `readonly:false` → подписывается
  writeback (`/devices/<id>/controls/<name>/on`), плечо базового `DevicePoller`.
  **Writeback только 16-битный (v1):** записываемый `holding`-канал с 32-битным
  форматом (`u32`/`s32`/`float`) **пропускается ГРОМКО** (WARN, writeback не
  регистрируется) — фактически остаётся read-only. Одно-регистровая запись
  затронула бы лишь младшее слово (`address+1` осталось бы старым), а echo вернул
  бы задуманное значение — тихая мис-запись. Много-регистровая запись требует
  проверки на оборудовании и в v1 не реализуется.

## 4. Отложено — пропуск ГРОМКО (WARN + пропуск канала), НИКОГДА не мис-опрос

Мост не выдумывает значение, которое не может честно декодировать. Каждый такой
канал выбрасывается из списка опроса с одной записью WARN:

- битовые адреса-поля (`"address": "reg:shift:width"` — любая строка с `:`);
- форматы `bcd`, `string`, `u64`/`s64`, `u8`/`s8`;
- `byte_order` (перестановка байт внутри регистра; в v1 только `word_order`);
- `consists_of` (составные каналы);
- `condition` (условные выражения включения);
- под-устройства (`device_type`/вложенный `device`);
- Jinja-шаблоны (`*.json.jinja`).

**Guard rail:** если пропущены ВСЕ каналы (полностью неподдерживаемый шаблон =
мис-импорт), мост пишет ERROR и не публикует ни одного control — сбой виден, а
не «наполовину работает».

## 5. Выходное отображение WB-MQTT

Совпадает со штатными семействами:

```
/devices/<id>/meta/name              = cfg.name | device.name | id
/devices/<id>/meta/driver            = "template"
/devices/<id>/controls/<name>                     ← значение канала
/devices/<id>/controls/<name>/meta/type           = WB control type
/devices/<id>/controls/<name>/meta/readonly       = "1" | "0"
/devices/<id>/controls/<name>/meta/units          = units (+ авто precision)
```

Опрос — только классический (v1): `fmb_event_ranges()` наследует `[]`, Fast
Modbus для шаблонов не арминится. Инвариант аренды порта RS-485 унаследован
структурно — `TemplatePoller` не открывает порт, а пользуется FC-обёртками
`DevicePoller` (`sa02m-domain.md ## Subsystems`).

## 6. Ростер RS-485

`write_bridge_roster` (`modbus_mqtt_bridge.py`) не меняется: `_OUR_DEVICE_TYPES`
остаётся `("mr02m","dtv","ce02m3")`, поэтому устройство `template` корректно
считается сторонним (`ours:false`), а `_roster_model_name` возвращает `""` — это
допустимо (сторонний модуль, модель не из наших таблиц).

## 7. Честность: не проверено на оборудовании

Карты регистров шаблонов **невозможно проверить на стенде без физического
устройства**. Тесты (`opt/sa02m-modbus-mqtt/tests/test_template_poller.py`)
проверяют РАНТАЙМ (разбор → декод формата/word_order/scale → набор публикаций
MQTT против `FakeSerial`), а НЕ соответствие реальному прибору. Веб-пикер
помечает каждый шаблон `verified:false` («не проверено на оборудовании»), пока
Оператор не подтвердит показания на реальном устройстве.

## 8. Ограничение последовательной линии: только 8N1 (v1)

Мост открывает COM-порт **жёстко 8N1** (`bridge_serial.py`: `parity=NONE,
stopbits=ONE`), поле для parity/stopbits в YAML НЕ поддерживается. Задаётся только
`baudrate`. Многие устройства Wiren Board по умолчанию работают на **9600 8N2** —
такое устройство на 8N2 в v1 **не ответит** (тихий отказ; смягчение — метка
`verified:false` и стендовая проверка). Настраиваемые parity/stopbits — отложенный
follow-up (см. бэклог). Не выдавайте 8N2-устройство за поддерживаемое.

## Проверка контракта

- `opt/sa02m-modbus-mqtt/tests/test_template_poller.py` — резолвер и обход
  каталога, golden-декод u16/s16/u32/s32/float × word_order, scale/offset,
  громкий пропуск неподдерживаемых полей, guard rail «все пропущены», полный
  проход опроса parse→poll→publish и writeback.
- `opt/sa02m-modbus-mqtt/tests/test_entry_surface.py` — `TemplatePoller` и
  модуль `bridge_template` в замороженной поверхности импорта.
- Веб-эндпойнт каталога: `www/network_config/cgi-bin/mqtt_templates.cgi` (GET,
  auth, read-only).
