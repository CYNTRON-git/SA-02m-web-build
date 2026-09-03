# Контракт: единая модель устройства SA-02m (`sh-model`)

Домашний адрес контракта единой модели устройства — аналога модели Sprut.Hub,
приспособленного под промышленный парк SA-02m (MR-02м / cyntron-dtv / CE-02m-3 +
сторонние Modbus-модули по шаблону). Модель описывает устройство как
`Device → Function → Point` и служит опорой ДО затрат на код: демон модели
(этап B), UI (этап C), профиль Алисы (этап F) строятся против неё.

Машинная грамматика (имена типов, полей, значения перечислений, формы JSON) — на
английском (`PROTOCOL.md` invariant 5); прозаические пояснения — на русском.
Машинный артефакт модели — `sh-model.schema.json` (JSON Schema draft 2020-12),
эталонный экземпляр — `sh-model.example.json` (WB-MR6C, выраженный в этой модели).

## 0. Обещание контракта (сначала обещание, потом механизм)

Появляется ОДИН словарь типов устройства + JSON-схема модели. Три вещи, которые
контракт держит:

1. **Единый словарь, а не пятый.** На устройстве уже сосуществуют несколько
   «моделей»: WB-control-типы на проводе (`switch/value/range/temperature/
   voltage/current/rel_humidity/text`, `docs/MQTT_TOPICS.md`); плоский
   `channels[]`-формат шаблона (`docs/contracts/template-device.md`); модель
   Яндекса (`docs/contracts/alice-mqtt-mapping.md`). `sh-model` **сшивает** их
   одной моделью `Function`/`Point`, а не добавляет ещё одну параллельную. Если
   ревью найдёт, что контракт вводит новый словарь вместо сшивки существующих —
   обещание нарушено, это блокирующая находка. Гарант сшивки — обязательная
   таблица соответствия §4.5.
2. **`quality` (годность значения) — явная, а не по умолчанию.** То, чего нет у
   Sprut.Hub. «Значение 0» и «связи нет» перестают быть неразличимы (§4.4).
3. **Проекция вниз не ломает бой.** Модель проецируется в живой контракт Алисы
   (`alice-mqtt-mapping.md`) как БУМАЖНОЕ отображение (§4.7); ни строки кода
   `alice-*` этот контракт не трогает.

## 1. Terminology anchor — extend, а не второй словарь (§4.1)

Единый дом вокабуляра иерархии — `cloud/docs/glossary.md`. `sh-model` НЕ заводит
второй словарь: он вводит два под-уровня существующего уровня 4
(Свойства/Умения), уточняя, из чего те состоят.

```
Site → Controller → Device → Function → Point
 ур.1     ур.2       ур.3      ур.5        ур.6
                   (глоссарий) (НОВОЕ)     (НОВОЕ, уточняет Properties/Capabilities ур.4)
```

- **`Device`** = строка ростера RS-485 (`docs/contracts/rs485-roster.md`:
  `model`, `addr`, `online`, `source`). Виртуальное устройство, отдаваемое Алисе
  (glossary ур.3).
- **`Function`** = виджет (аналог Spruthub Service / HSQLDB `Services` с
  `gridOrder/Width/Height`): семантический класс + раскладка (`layout`).
- **`Point`** = атомарная «ручка» (аналог HomeKit Characteristic / WB-control):
  типизированное значение + роль + `quality` + привязка.

`Properties`/`Capabilities` Яндекса (glossary ур.4) — это ПРОЕКЦИЯ `Point`'ов
(§4.7), а не отдельная сущность. Расширение глоссария живёт в своём доме
(`cloud/docs/glossary.md`, deliverable D5); здесь — только ссылка на него.

## 2. Затронутые контракты (ссылки, не пересказ)

- `template-device.md` — существующий `channels[]`-формат; `sh-model`
  СОГЛАСУЕТ его со своим слоем привязки таблицей §4.5. Формат template-device
  на этом этапе НЕ переписывается.
- `alice-mqtt-mapping.md` — ЦЕЛЬ проекции (§4.7). Enum'ы и правила берутся ОТТУДА
  дословно. Файл не трогается.
- `mqtt-set-endpoint.md` — путь записи командных точек. `Point` с ролью
  `command`/`setpoint` проецируется в этот эндпоинт; его грамматика здесь НЕ
  дублируется — только ссылка (§6).
- `rs485-roster.md` — откуда берётся `Device` (`ours`/`model`/`online`).
- `docs/MQTT_TOPICS.md` — источник WB-control-типов и живых имён контролов
  (MR/DTV/CE); каждый Point-тип и `quality`-сигнал заземляются сюда.
- `cloud/docs/contracts/alice-gateway.md` — ЖИВОЙ контракт Яндекса, который
  проекция обязана уважать (не ломать). На этом этапе — только граница чтения.

## 3. Объём

Бумага (+один валидирующий тест, §«Проверка контракта»): словарь `Function`-типов
(§4.2) + словарь `Point`-типов/ролей (§4.3) + поле `quality` (§4.4) + JSON-схема
(`sh-model.schema.json`) + таблица привязки (§4.5) + таблица проекции в Яндекс
(§4.7). Рантайм устройства этот этап не меняет. Вне объёма: демон модели (B), UI
(C), сценарии (D), мост в облако (E), переписывание проекции Алисы в код (F),
MCP (G), HomeKit (H); Zigbee/Z-Wave — вне профиля продукта.

## 4.2. Словарь `Function`-типов (v1 — WIDE-набор, решение Оператора Q1)

Каждый тип ОБЯЗАН быть заземлён: реальный WB-модуль каталога, живой SA-02m-контрол
(`docs/MQTT_TOPICS.md`), или каталог сервисов HomeKit/Spruthub
(типовой набор HAP-сервисов, изученный в исследовании Sprut.Hub на стороне
`cloud`). Тип-композиция или виртуальный — помечается
честно (`composition:true` / `virtual:true` в схеме), чтобы группировку не выдать
за отдельный драйвер.

**Промышленное ядро (12 типов):**

| `Function.type` | Заземление (реальное) | Point-роли | Проекция в Яндекс (§4.7) |
|---|---|---|---|
| `DiscreteOutput` | MR-02м `do_*` (type=switch); WB-MR6C `Switch/On/Coil` (каталог) | 1 `command`(bool) [+ `feedback`] | `capabilities.on_off` |
| `DiscreteInput` | MR-02м `di_*`; WB-MR6C `ContactSensor/Discrete` (каталог) | 1 `measurement`(bool) | `properties.event`/`float` по инстансу |
| `AnalogInput` | MR-02м `ai_*` (temp/voltage/current по коду датчика); DTV-сенсоры | 1 `measurement`(number)+`unit` | `properties.float` |
| `AnalogOutput` | MR-02м `ao_*` (type=range 0..1000 = 0..10 В); `mqtt-set-endpoint.md` | 1 `setpoint`(number) | `capabilities.range` |
| `TemperatureSensor` | DTV `temp_bme680`; MR `mcu_temp` (type=temperature) | 1 `measurement` °C | `float` `temperature` |
| `HumiditySensor` | DTV `humidity_bme680` (type=rel_humidity) | 1 `measurement` % | `float` `humidity` |
| `Meter` | CE-02m-3 `voltage_*/current_*/power_*/pf_*/frequency`; HomeKit `C_VoltMeter/C_AmpereMeter/C_WattMeter/C_KiloWattHourMeter` | N `measurement` (V/A/W/…) | `float` `voltage/amperage/power/electricity_meter` |
| `Pump` `composition` | MR `do`(пуск)+`di`(работа)+авария | `command`+`feedback`+`alarm` | `on_off` (проекция вниз тривиальна, обратно — нет) |
| `Valve` `composition` | HomeKit `Valve`; MR `do`(открыть/закрыть)+`di`(концевик) | `command`+`feedback` | `on_off` / `range` |
| `Drive` `composition` | ПЧ/VFD: MR `ao`(range)+`do`(пуск)+`di`(авария) | `setpoint`+`command`+`feedback`+`alarm` | `range` + `on_off` |
| `Alarm` `composition` | DTV `presence`; HomeKit `SecuritySystem`; MR `di` как авария | `measurement`(bool) + `quality`-critical | `properties.event` (`smoke/water_leak/motion/…`) |
| `MotionSensor` | DTV `presence` (type=switch readonly) | `measurement`(bool) | `event` `motion` |

**Расширенный набор (v2, втянут в v1 решением Оператора Q1):**

| `Function.type` | Заземление | Честная пометка | Проекция в Яндекс |
|---|---|---|---|
| `WindowCovering` | HomeKit `WindowCovering`; на проводе — MR `ao`(позиция)/`do`(вверх/вниз)+`di`(концевики) | `composition` | `capabilities.range` (открытие %) |
| `Thermostat` | HomeKit `Thermostat`; композиция `TemperatureSensor` + `AnalogOutput`(уставка)/`DiscreteOutput`(нагрев) | `composition` | `float temperature` + `capabilities.range` |
| `HeaterCooler` | HomeKit `HeaterCooler`; та же композиция, что `Thermostat`, с режимом нагрев/охлаждение | `composition` | `float temperature` + `capabilities.range`/`on_off` |
| `LeakSensor` | HomeKit `LeakSensor`; на проводе — MR `di` (сухой контакт датчика протечки) | заземлён на `di` | `event` `water_leak` |
| `SmokeSensor` | HomeKit `SmokeSensor`; MR `di` (выход дымового извещателя) | заземлён на `di` | `event` `smoke` |
| `AirQualitySensor` | DTV `iaq_bme680`/`tvoc_zmod`/`eco2_*` (type=value units=IAQ/mg/m³/ppm); HomeKit `AirQualitySensor` | заземлён на DTV | `float` `tvoc`/`co2_level` (IAQ — см. §4.7, потолок) |
| `SecuritySystem` | HomeKit `SecuritySystem`; композиция `Alarm`-точек + `DiscreteOutput`(постановка) | `composition`+`virtual` возможен | `event` + `on_off` |

**Виртуальные устройства (Q1).** Группа или вычисляемое значение (аналог
Spruthub `virtual:true`) моделируется флагом `virtual:true` на `Function`; её
`Point`'ы вычисляются из других точек и МОГУТ не нести `binding` (схема это
допускает только для `virtual`-функции — §4.5, §4.6). Физическая функция всегда
несёт `binding` на каждой точке.

> **Честность (semantic-correctness: real-or-marked).** `Pump/Valve/Drive/Alarm/
> WindowCovering/Thermostat/HeaterCooler/SecuritySystem` — семантические
> ГРУППИРОВКИ над реально существующими Point-ролями (`do/di/ao/ai`), а не новые
> железки; каждая помечена `composition`. Так модель промышленная, но каждый тип
> раскладывается в точки, которые уже есть на проводе.

## 4.3. Словарь `Point`-типов и ролей

`Point` несёт две ОРТОГОНАЛЬНЫЕ оси — не смешивать (иначе неоднозначность):

1. **`valueType`** (тип значения на проводе): `bool | int | float | string` —
   заземлён на Spruthub `common.js` (`boolValue/intValue/floatValue/stringValue`)
   и на WB-форматы (`u16/s16/u32/s32/float`, `template-device.md` §3).
2. **`role`** (что точка делает):

   | `role` | Что | Заземление |
   |---|---|---|
   | `measurement` | чтение показания | `ai_*`, сенсоры DTV, `di_*` |
   | `command` | запись, мгновенное действие | `do_*` (реле) |
   | `setpoint` | запись, уставка | `ao_*` (0..1000 = 0..10 В) |
   | `feedback` | чтение, подтверждение `command`'а | `di_*` (концевик/работа) |
   | `alarm` | чтение, критично к `quality` | `di_*` как авария, `presence` |

Дополнительные поля `Point` (полный список — `sh-model.schema.json`):
`id`, `unit` (WB-units, `MQTT_TOPICS.md`), флаги `read`/`write`/`events` (как
HomeKit Characteristic `control.write/read/events`), ограничения
`min`/`max`/`step`/`enum[]` (Spruthub `minValue/maxValue/minStep/validValues`),
`quality` (§4.4), `ts`, `binding` (§4.5).

## 4.4. Поле `quality` — вычисляемое, явное (ядро новизны; Q2 = вариант C)

`quality` — ЯВНОЕ перечисление на каждом `Point`'е:

`good | bad | stale | uncertain`

**Ключ (Q2 = вариант C): `quality` ВЫЧИСЛЯЕТСЯ моделью из сигналов, которые уже
есть на проводе** — ничего нового на MQTT-шину не добавляется, существующее
нормализуется в enum на уровне модели:

| Живой сигнал WB (`docs/MQTT_TOPICS.md`) | → `quality` |
|---|---|
| `/controls/<name>/meta/error = ""` и свежий `ts` | `good` |
| `/controls/<name>/meta/error = "r"` (ошибка чтения) | `bad` |
| `/controls/<name>/meta/error = "w"` (ошибка записи) | `bad` (для `command`/`setpoint`) |
| `/devices/<id>/meta/error = "r"` (устройство offline, back-off) | `stale` (последнее good-значение держится, но не живое) |
| payload не парсится | значение ОТСУТСТВУЕТ + `quality = bad` (не фабриковать 0) |
| источник неизвестен / сигнала нет | `uncertain` (см. fail-closed ниже) |

Это смыкается с правилом `alice-mqtt-mapping.md` «unparseable → omit, never
fabricate 0.0» и «real `power:0` IS sent»: `quality` делает различие «честный 0»
vs «нет связи» ЯВНЫМ полем модели, тогда как сегодня оно выражено только через
отсутствие свойства.

**Инвариант fail-closed (безопасность/честность).** Отсутствующий или неизвестный
`quality` трактуется потребителем как `bad`/`uncertain`, НИКОГДА как `good`.
Поэтому в схеме `quality` — `required` и БЕЗ `default` (§4.6): модель обязана
проставить годность осознанно, схема не «допишет» `good` молча.

*Отвергнутые варианты (для истории):* (A) отдельный enum-топик
`/controls/<name>/meta/quality` — явно, но новый топик на каждую точку тяжелит
брокер; (B) булев `valid`+причина — теряет различие `stale` vs `bad`. Вариант C
дёшев, переиспользует живую инфраструктуру, ничего не ломает.

## 4.5. Слой привязки `binding` — reuse Spruthub link + согласование с `channels[]`

`Point.binding` — ТЕГИРОВАННОЕ ОБЪЕДИНЕНИЕ (дискриминатор `kind`): точка привязана
к Modbus-регистру ЛИБО к MQTT-топику (Q3 = адаптировать структуру Spruthub +
сохранить богатые поля SA-02m).

- **`binding.modbus`** — адаптирует Spruthub `link` (`address`, `function` =
  `Coil/Discrete/Holding/Input`, `pollingTime` мс, `valueType` =
  `u16/s16/u32/s32/float`), РАСШИРЕН полями, которых у Spruthub нет, но которые
  уже есть в нашем `channels[]`: `scale`, `offset`, `word_order`, `units`.
- **`binding.mqtt`** — топик WB (`/devices/<id>/controls/<name>`), для случая,
  когда модель строится ИЗ живой MQTT-шины (реальная система: мост даёт
  Modbus→MQTT, Алиса читает MQTT), а не напрямую из Modbus. Командная точка
  несёт `writeTopic` (`…/on`) — но сам путь записи это `mqtt-set-endpoint.md`,
  здесь только ссылка (§6).

> **Два слоя привязки не путать (находка для этапа B).** В живой системе их два:
> (1) template→Modbus — карта регистров модуля, её потребляет `bridge_template.py`
> (Spruthub `link`-формата); (2) `Function`/`Point`→MQTT — как демон модели (B)
> подписывается на шину. `binding` — union именно поэтому: модель может быть
> привязана к Modbus-регистру ЛИБО к MQTT-топику. Без этого разделения этап B
> упрётся в двусмысленность.

**Обязательная таблица соответствия** (гарант сшивки §0.1 — без неё получился бы
5-й словарь):

| Spruthub `link[]` | SA-02m `template-device.md channels[]` | `sh-model` `binding.modbus` | Совпадает? |
|---|---|---|---|
| `function: Coil/Discrete/Holding/Input` | `reg_type: coil/discrete/holding/input` | `function` | да (регистр ↔ нижний регистр) |
| `valueType: u16/s16/u32/…` | `format: u16/s16/u32/s32/float` | `valueType` | да |
| `address` | `address` | `address` | да |
| `pollingTime` (мс, на канал) | `poll_s` (с, на устройство) | `pollingTime` (мс) | различие гранулярности (канал/устройство, мс/с) |
| — | `scale`/`offset`/`word_order`/`units` | `scale`/`offset`/`word_order`/`units` | только у нас (богаче) |
| `options[]` (Holding + `values[]`) | `device.setup` (init-записи) | `Device.setup[]` (`{address,value,name?}`) | близко, не тождественно (перечисление значений теряется — берётся default) |

**Q3 = АДАПТИРОВАТЬ, не дословно.** Взята СТРУКТУРА Spruthub
(`services[].type` → `characteristics[].type` → `link[]`) — она несёт
семантический слой типов, которого нет у плоского `channels[]`, — но сохранены
богатые поля привязки SA-02m (`scale/offset/word_order/units`). Дословный Spruthub
потерял бы `scale/offset`, критичные для CE/DTV (`alice-mqtt-mapping.md §scale`).

## 4.6. JSON-схема (`sh-model.schema.json`)

- Draft: **JSON Schema draft 2020-12** (валидатор в CI — Python `jsonschema`,
  поддерживает 2020-12).
- Корень: `Device` → `functions[]` → `points[]`, с `$defs` для `Function.type`
  (enum §4.2 WIDE-набора), `Point.role`, `Point.valueType`, `quality`, `binding`
  (`oneOf`: `modbus` | `mqtt`, дискриминатор `kind`).
- **fail-closed:** `additionalProperties: false` на всех объектах модели
  (незнакомое поле — ошибка, не тихо принято); `quality` — `required`, `default`
  запрещён (§4.4); `binding` — `required` на точке ФИЗИЧЕСКОЙ функции (условие
  `if not virtual then points require binding`), у `virtual`-функции может
  отсутствовать.
- Эталон (`sh-model.example.json`) — WB-MR6C: 6× `DiscreteOutput` (Coil 0..5) +
  7× `DiscreteInput` (Discrete 0..6) + `options`→`setup`. Точный перевод шаблона
  Modbus-модуля WirenBoard WB-MR6C из каталога Sprut.Hub (изучен в исследовании
  на стороне `cloud`) — пример воспроизводит реальную карту регистров модуля.

## 4.7. Проекция в Яндекс — БУМАЖНАЯ, лоссовая, однонаправленная (не ломать `alice-gateway`)

Таблица «`sh-model` → Yandex», ноль кода. Значения берутся ДОСЛОВНО из
`alice-mqtt-mapping.md` (`FLOAT_INSTANCES`, `EVENT_INSTANCES`, правила
`parameters`, `scale` на уровне item, «одно `(type,instance)` на устройство»).

| `sh-model` (`Function.type` + `Point.role`) | Yandex `type` | Yandex capability/property + `instance` |
|---|---|---|
| `DiscreteOutput` (`command`) | `devices.types.switch` | `capabilities.on_off` (`instance: on`) |
| `AnalogOutput` (`setpoint`) | — | `capabilities.range` |
| `TemperatureSensor` (`measurement`) | `devices.types.sensor.climate` | `properties.float` (`instance: temperature`, `unit.temperature.celsius`) |
| `HumiditySensor` | `devices.types.sensor.climate` | `float` (`instance: humidity`) |
| `Meter` (voltage/amperage/power) | `devices.types.sensor` | `float` (`instance: voltage/amperage/power/electricity_meter`) |
| `AirQualitySensor` (tvoc/eco2) | `devices.types.sensor.climate` | `float` (`instance: tvoc` / `co2_level`) |
| `MotionSensor` / `Alarm`(presence) | `devices.types.sensor.motion` | `properties.event` (`instance: motion`) |
| `LeakSensor` | `devices.types.sensor` | `event` (`instance: water_leak`) |
| `SmokeSensor` | `devices.types.sensor` | `event` (`instance: smoke`) |

`FLOAT_INSTANCES` (дословно `alice-mqtt-mapping.md`): `temperature | humidity |
voltage | amperage | power | pressure | co2_level | tvoc | illumination |
battery_level | water_level | electricity_meter`.
`EVENT_INSTANCES`: `motion | open | button | vibration | smoke | gas |
water_leak | battery_level | food_level | water_level`.

**Инварианты проекции (согласованность, не деградация):**

- Проекция ВНИЗ лоссовая и однонаправленная. `quality` в модель Яндекса **НЕ
  проецируется** (у Яндекса нет понятия годности); `bad`/отсутствие значения →
  свойство ОПУСКАЕТСЯ — ровно текущее поведение `alice-mqtt-mapping.md` «omit
  rather than fabricate». Измеренный `power: 0` при этом ОТПРАВЛЯЕТСЯ (осознанное
  отклонение от диапазона Яндекса — решение Оператора 2026-08-27), а «нет связи»
  даёт `bad` → опускание: измеренный 0 и отсутствие показания остаются различимы.
- **Потолки Яндекса, называемые честно** (`alice-mqtt-mapping.md`): у Яндекса
  НЕТ инстанса `presence` (presence → `motion`) и НЕТ инстанса расстояния/длины
  вообще (радар-дистанция DTV непредставима — ничего не подставляется); IAQ как
  индекс не имеет прямого float-инстанса (проецируется в `tvoc`/`co2_level`, где
  есть физическая величина, иначе не проецируется).
- «Одно `(type,instance)` на устройство» (`alice-mqtt-mapping.md`): CE-02m-3
  (все `voltage_*` = инстанс `voltage`) требует ОДНО устройство на фазу + устройство
  «итого»; DTV укладывается в одно устройство (temperature/humidity/pressure/
  co2_level/tvoc/motion — разные инстансы).
- Этот этап НЕ меняет ни `alice-gateway.md`, ни `alice-mqtt-mapping.md` — живой
  документ устройств Алисы остаётся источником истины до этапа F.

## 6. Поверхность безопасности

Этап рантайм-входов не добавляет, но контракт ЗАДАЁТ инварианты для недоверенного
входа этапа B — записаны сейчас, пока дёшево:

- **`quality` fail-closed** (§4.4): unknown/absent → `bad`/`uncertain`, никогда
  `good`.
- **Схема fail-closed** (§4.6): `additionalProperties:false`, `quality` required,
  `binding` required на физической точке — незнакомое/неполное отвергается, не
  принимается тихо.
- **Граница доверия шаблона переносится вперёд:** модель, построенная из шаблона,
  наследует fail-closed резолвер `template-device.md` §2 (имя `^[A-Za-z0-9._-]+$`,
  realpath строго внутри каталога) — контракт ссылается на него, не переопределяет.
- **Командные `Point`'ы проецируются ТОЛЬКО через `mqtt-set-endpoint.md`**
  (auth → CSRF → allow-list → publish, без retain) — этот контракт НЕ открывает
  второй путь записи. `binding.mqtt.writeTopic` — лишь адрес топика; грамматика,
  проверки и запрет retain живут в `mqtt-set-endpoint.md` (ссылка, не дубль).
- Секретов/сети/новых слушателей этап не вводит. Остальные поверхности
  threat-модели — considered, not exposed.

## Проверка контракта

Реестровая строка `sh-model-schema` (`.ai-dev/quality/checks/sh-model-schema.sh`,
beat `build`) — единственный исполняемый владелец схемы. Гоняет валидацию
`sh-model.example.json` против `sh-model.schema.json` (Python `jsonschema`,
draft 2020-12) и ТРИ намеренно битых варианта, каждый из которых обязан быть
ОТВЕРГНУТ: точка без `quality`, точка с незнакомым полем, точка физической
функции без `binding`. Негативная половина — несущая: она доказывает, что
fail-closed-гарантии схемы (§4.6) реально работают, а не только описаны. Строка
deps-guarded: чисто пропускается, если `jsonschema` не установлен локально
(как `pytest-suite.sh`), и REAL в CI (`web-quality.yml` ставит `jsonschema`).

Ручной повтор (без CI): установить `jsonschema`, затем из корня репо
`python -m jsonschema -i docs/contracts/sh-model.example.json
docs/contracts/sh-model.schema.json` (валидный пример проходит; убрать `quality`
у любой точки — падает). Примечание: CLI `python -m jsonschema` объявлен
deprecated в свежих версиях библиотеки; исполняемая строка `sh-model-schema`
использует стабильный API `Draft202012Validator` — та же гарантия, устойчивый
вызов.
