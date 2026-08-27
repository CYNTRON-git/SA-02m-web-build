# MQTT Topics — СА-02м

## Конвенция

Используется **Wiren Board MQTT** — `/devices/<id>/controls/<name>`.

| Поле | Правило |
|------|---------|
| QoS | 1 для всех измерений |
| retain | true для всех `/controls/*` и `/meta/*` |
| Ошибка чтения | `/controls/{name}/meta/error` = `"r"` |
| Ошибка записи | `/controls/{name}/meta/error` = `"w"` |
| Восстановление | `/controls/{name}/meta/error` = `""` (cleared) |
| Запись значения | `/controls/{name}/on` ← write `0` / `1` / float; публикуют внешние клиенты **и веб-интерфейс** (`mqtt_set.cgi`, без retain — контракт `docs/contracts/mqtt-set-endpoint.md`) |
| Публикация meta | Один раз при старте (retained) — И отдельные субтопики `/meta/<key>`, И сводный `/meta` JSON-блоб (см. ниже) |
| **Устройство offline** | `/devices/{id}/meta/error` = `"r"` (device-level, retained) |
| **Устройство online** | `/devices/{id}/meta/error` = `""` (cleared) |

---

## `/meta` JSON-блоб (совместимость с современным WB)

Помимо отдельных retained-субтопиков `/meta/<key>` (`/meta/units`, `/meta/type`,
`/meta/precision`, …) мост **дополнительно** публикует сводный retained JSON-блоб
`/meta`, который читают современные инструменты WB (`wb-mqtt-serial` 2.x,
HA-автодискавери через WB). Это **аддитивно**: все прежние субтопики остаются
байт-в-байт, ничего не удалено и не переименовано. Блоб собирается из тех же
накопленных значений, что и субтопики (один источник — не могут разойтись), и
несёт тот же retain-флаг.

- **Контрол** — `/devices/{id}/controls/{name}/meta` (топик, отдельный от
  `…/meta/<key>` — в MQTT это разные топики, блоб не затирает субтопики):

  ```
  /devices/mr02m-COM1-5/controls/ao_1/meta
      {"type":"range","min":0,"max":1000,"order":1,"units":"V","precision":3,"title":{"ru":"AO1","en":"AO1"}}
  ```

  Типизация под JSON-схему WB: `readonly` → boolean, `order`/`min`/`max`/`precision`
  → число, `title`/`enum` → объект; `type`/`units` — строки как в субтопиках.

- **Устройство** — `/devices/{id}/meta` (WB device-meta shape):

  ```
  /devices/mr02m-COM1-5/meta         {"driver":"modbus-rtu","title":{"ru":"MR-02м 6DO8DI (COM1 addr=5)","en":"MR-02м 6DO8DI (COM1 addr=5)"}}
  ```

  Субтопики `/meta/name` и `/meta/driver` сохранены (аддитивно).

---

## Доступность (как в wb-mqtt-serial)

Мост повторяет ключевые надёжностные практики `wb-mqtt-serial`:

| Механизм | Поведение |
|----------|-----------|
| **Last Will (LWT)** | MQTT допускает один will на соединение. Будучи единым каналом ошибок, will публикует `/devices/sa02m-bridge/meta/error="r"` при падении/обрыве моста — монитор `/devices/+/meta/error` ловит крах моста так же, как offline отдельного устройства. `controls/connection` публикуется активно (`1` в работе, `0` при штатной остановке). |
| **Device-level error** | Когда устройство перестаёт отвечать (≥ `offline_after_fails` подряд), публикуется `/devices/{id}/meta/error="r"`; при восстановлении — `""`. Значения `controls/*` сохраняют последнее good-значение. |
| **Сброс retained при старте** | При старте мост чистит `/devices/{id}/meta/error=""` для каждого устройства из конфига, снимая «залипший» retained `"r"` от прошлого процесса (shutdown/LWT). Устройство несёт `"r"` только если отказывает **сейчас** — back-off снова выставит его после `offline_after_fails`. Иначе здоровое устройство, не менявшее состояние на перезапуске, никогда бы не сбросило старый `"r"`, и облачный watchdog флудил бы ложными инцидентами. |
| **Error back-off** | «Мёртвое» устройство не опрашивается на полной скорости: пауза растёт экспоненциально (`backoff_base_s` … `backoff_max_s`), чтобы не блокировать half-duplex RS-485 для живых устройств. |
| **Graceful offline** | При штатной остановке (`systemctl stop`) мост помечает все устройства и себя offline до отключения. |

### Устройство-статус моста — `sa02m-bridge`

```
/devices/sa02m-bridge/meta/name              "SA-02m Modbus→MQTT bridge"
/devices/sa02m-bridge/meta/driver            "sa02m-modbus-mqtt"
/devices/sa02m-bridge/meta/error             ""        (LWT → "r" при падении моста)
/devices/sa02m-bridge/controls/connection    type=switch  "1" online / "0" штатная остановка
/devices/sa02m-bridge/controls/devices_total  type=value  всего устройств в конфиге
/devices/sa02m-bridge/controls/devices_online type=value  отвечающих сейчас
/devices/sa02m-bridge/controls/poll_errors    type=value  накопленный счётчик отказов опроса
```

Настройки (в `mqtt:` и/или на устройство) — см. `sa02m-modbus-mqtt.yaml`:
`availability`, `bridge_device_id`, `offline_after_fails`, `backoff_base_s`, `backoff_max_s`.

> **`bridge_device_id` должен оставаться стабильным.** Каноническое значение —
> `sa02m-bridge`. Смена id **осиротит** весь старый retained-поддерево статуса
> моста (`devices_total`, `devices_online`, `poll_errors`, `connection`,
> `meta/error`) — эти топики останутся висеть под старым id и потребуют ручной
> очистки на брокере. И **никогда** не задавайте его равным телеметрийному id —
> имени платы (например `SA-02m`): это id живого устройства телеметрии
> (`sa02m_telemetry.py`, см. ниже), и совпадение смешает bridge-контролы с
> телеметрией в одном дереве.

---

## Схема Device ID

| Тип | Device ID | Пример |
|-----|-----------|--------|
| СА-02м контроллер | имя платы (`{hostname}`) | `SA-02m` |
| MR-02м модуль | `mr02m-{port}-{addr}` | `mr02m-COM1-5` |
| cyntron-dtv | `dtv-{port}-{addr}` | `dtv-COM3-1` |
| CE-02m-3 счётчик | `ce02m3-{port}-{addr}` | `ce02m3-COM2-14` |

---

## СА-02м — системная телеметрия

Device ID: **имя платы** (`hostname`, пример: `SA-02m`) — без префикса.
Порядок разрешения id, побеждает первое **корректное** значение:
`$SA02M_TELEMETRY_DEVICE_ID` → `/etc/sa02m_telemetry.conf` → `hostname` →
`SA-02m`. Допустимый набор символов `^[A-Za-z0-9_.:-]{1,64}$` — значение
становится сегментом топика, поэтому `/`, `+`, `#` отклоняются с WARN в журнале
и разрешение идёт дальше. Живой id всегда печатается в журнал при старте
службы: `journalctl -u sa02m-telemetry | grep 'telemetry device id'`.

```
/devices/SA-02m/meta/name          "СА-02м (SA-02m)"
/devices/SA-02m/meta/driver        "sa02m-telemetry"
/devices/SA-02m/meta/error         ""    (LWT → "r" при падении телеметрии)

# Доступность контроллера (connection — активная публикация; offline через LWT meta/error)
/devices/SA-02m/controls/connection  type=switch  "1" online / "0" штатная остановка

# Системные показатели (интервал 30 с)
/devices/SA-02m/controls/cpu_pct           type=value      %
/devices/SA-02m/controls/temp_c            type=temperature °C
/devices/SA-02m/controls/ram_pct           type=value      %
/devices/SA-02m/controls/uptime_s          type=value      (д/ч/м, без ед.)

# Управляемые выходы PCA9536 (R/W)
/devices/SA-02m/controls/do                type=switch
/devices/SA-02m/controls/do/on             ← write 0/1
/devices/SA-02m/controls/beeper            type=switch
/devices/SA-02m/controls/beeper/on         ← write 0/1
/devices/SA-02m/controls/alarm_led         type=switch
/devices/SA-02m/controls/alarm_led/on      ← write 0/1

# RS-485 статистика (5 портов: com1..com5)
/devices/SA-02m/controls/rs485_com1_tx     type=value
/devices/SA-02m/controls/rs485_com1_rx     type=value
/devices/SA-02m/controls/rs485_com1_errors type=value
# ... rs485_com2_* ... rs485_com5_*
```

### Закрепление id (`/etc/sa02m_telemetry.conf`)

Файл **никем не создаётся** — он существует, только если интегратор сам его
написал. Смысл: после закрепления последующая смена `hostname` уже не осиротит
ни одну привязку.

```
SA02M_TELEMETRY_DEVICE_ID=boiler-room-1
```

Второе применение — **общий внешний брокер**: с 1.0.5.69 все платы носят имя
`SA-02m`, поэтому на одном внешнем брокере они сталкиваются на одном id (так
было и раньше, с прежней схемой). Закрепление разных id — штатный выход.

### Смена id телеметрии (1.0.6.21)

| Было | Стало |
|---|---|
| `/devices/sa02m-{hostname}/…` (`sa02m-SA-02`, `sa02m-SA-02m`) | `/devices/{hostname}/…` (`SA-02m`) |

Затронуты только топики самого контроллера. Модули (`mr02m-*`, `dtv-*`,
`ce02m3-*`) и статус моста (`sa02m-bridge`) — **без изменений**.

- **Внешние клиенты** (SCADA, скрипты, интеграции), подписанные на старое
  поддерево контроллера, должны переподписаться на новое имя.
- **`sa02m-mqtt-opcua` / `sa02m-mqtt-snmp`** подписаны по wildcard, поэтому узел
  появится под новым именем сам, но **поканальное включение** в
  `/etc/sa02m-mqtt-opcua.conf` задано ключом `<device_id>/<control>` — каналы,
  включённые под старым id, перестанут экспортироваться, пока ключи не
  переписаны. Путь узла у OPC UA-клиентов тоже меняется.
- **Старое поддерево** (retained, без публикатора) плата очищает сама один раз
  при первом старте новой версии — но **только** если брокер локальный
  (`127.0.0.1`/`localhost`/`::1`) **и** в поддереве лежит retained
  `meta/driver` = `sa02m-telemetry`. Оба условия — доказательство «это моё»:
  все платы носят одно имя, и на общем брокере очистка стёрла бы живое
  поддерево соседа. Если доказательства нет, плата **ничего не удаляет** и
  пишет WARN с причиной.

Ручной эквивалент (единственный путь для внешнего брокера или платы с
переименованным вручную `hostname` — подставьте свой старый id):

```sh
# Посмотреть, что осталось под старым id
mosquitto_sub -h 127.0.0.1 -v -t '/devices/sa02m-SA-02m/#' --retained-only -W 3
# Очистить (пустой retained-payload на каждый топик)
mosquitto_sub -h 127.0.0.1 -t '/devices/sa02m-SA-02m/#' --retained-only -W 3 -F '%t' \
  | while read -r t; do mosquitto_pub -h 127.0.0.1 -t "$t" -r -n; done
```

**Привязки Алисы**, указывающие на старое имя, надо выбрать заново: сравните
сегмент `/devices/<id>/…` каждой привязки в `/etc/sa02m-alice-devices.conf` с
живым id из журнала телеметрии и с id устройств моста — привязка, не совпавшая
ни с одним, мертва.

---

## MR-02м / MP-02м — модули расширения

Device ID: `mr02m-{port}-{addr}` (пример: `mr02m-COM1-5`)

Тип модуля определяется из Input reg 0. Каждый тип имеет фиксированное количество каналов:

| Код | Имя | DO | DI | AO | AI |
|-----|-----|----|----|----|----|
| 1 | 6DO8DI | 6 | 8 | 0 | 0 |
| 2 | 16DO | 16 | 0 | 0 | 0 |
| 3 | 14DI | 0 | 14 | 0 | 0 |
| 4 | 6DO | 6 | 0 | 0 | 0 |
| 5 | 4DO6DI | 4 | 6 | 0 | 0 |
| 6 | 12AO | 0 | 0 | 12 | 0 |
| 7 | 12AI | 0 | 0 | 0 | 12 |
| 8 | 6AI6AO | 0 | 0 | 6 | 6 |
| 9 | 6AI2AO | 0 | 0 | 2 | 6 |
| 11 | DO4DI4AO2AI4 | 4 | 4 | 2 | 4 |
| 12 | DI8AI4 | 0 | 8 | 0 | 4 |
| 13 | DO2DI4AI2 | 2 | 4 | 0 | 2 |
| 14 | DO6DI4AI4 | 6 | 4 | 0 | 4 |
| 15 | DO4DI4AO4AI4 | 4 | 4 | 4 | 4 |

### Мета-информация
```
/devices/mr02m-COM1-5/meta/name         "MR-02м 6DO8DI (COM1 addr=5)"
/devices/mr02m-COM1-5/meta/driver       "modbus-rtu"
/devices/mr02m-COM1-5/controls/module_type   type=text
/devices/mr02m-COM1-5/controls/uptime_s     type=value  (д/ч/м, без ед.)
/devices/mr02m-COM1-5/controls/serial        type=text
/devices/mr02m-COM1-5/controls/mcu_temp      type=temperature  °C  (Holding 124, ×0.1)
/devices/mr02m-COM1-5/controls/mcu_vdd       type=voltage  V  (Holding 123, ×0.01)
/devices/mr02m-COM1-5/controls/op_days       type=value  дн  (Holding 114)
/devices/mr02m-COM1-5/controls/mcu_ram_free  type=value  B  (Input 65505)
/devices/mr02m-COM1-5/controls/mcu_ram_used  type=value  B  (Input 65506)
/devices/mr02m-COM1-5/controls/reset_reason  type=text  (Input 65508)
/devices/mr02m-COM1-5/controls/fw_updates    type=value  (Input 65509–65510, uint32)
```

### DO (дискретные выходы — coils 1..N, R/W)
```
/devices/mr02m-COM1-5/controls/do_1     type=switch  "0"|"1"
/devices/mr02m-COM1-5/controls/do_1/on ← write "0"|"1"
# ... до do_16
```

### DI (дискретные входы — input registers 18..17+N, FC04, R)
```
/devices/mr02m-COM1-5/controls/di_1    type=switch  readonly
# ... до di_14
/devices/mr02m-COM1-5/controls/di_1_count  type=value  (uint32, счётчик)
/devices/mr02m-COM1-5/controls/di_1_freq   type=value  Hz
```

### AO (аналоговые выходы — holding regs 33..32+N, сырое ×0,01 В, R/W)
```
/devices/mr02m-COM1-5/controls/ao_1    type=range  min=0 max=1000  meta/units=V
/devices/mr02m-COM1-5/controls/ao_1/on ← write 0–1000
# ... до ao_12
```

### AI (аналоговые входы — holding regs 400+7*n, R)
Значение регистра +3 (signed int16) масштабируется по типу датчика.
Полный список кодов — `opt/sa02m-flasher/sa02m_flasher/module_profiles.py` `AI_SENSOR_CHOICES`.
Коды **0–42** — Modbus selection codes (регистр «тип датчика», MR-02m ≥1.0.9.1), см. `MR-02m/README.md`:

| Код | Тип | Единицы | Масштаб reg+3 |
|-----|-----|---------|---------------|
| 0 | Выключен | — | — |
| 3 | NTC 10k (B3950) | °C | ×0.1 |
| 9 | Pt100 (α385), 2-пров. | °C | ×0.1 |
| 11 | Pt1000 (α385), 2-пров. | °C | ×0.1 |
| 12 | Pt50 (α391), 50П | °C | ×0.1 |
| 15 | Pt50 (α428), 50М | °C | ×0.1 |
| 22 | Pt100 (α385), 3-пров. | °C | ×0.1 |
| 25 | Pt50 (α391), 50П, 3-пров. | °C | ×0.1 |
| 28 | Pt50 (α428), 50М, 3-пров. | °C | ×0.1 |
| 34 | Напряжение 0–10 В | V | ×0.001 |
| 35 | Напряжение 0–30 В | V | ×0.01 |
| 38–40 | Ток 0–5 / 0–20 / 4–20 мА | mA | ×0.01 |
| 41 | Термопара K (ТХА) | °C | ×0.1 |
| 42 | Сухой контакт | — | ×1 (0/1) |

Коды 1–2, 4–8, 10, 13–14, 16–21, 23–24, 26–27, 29–33, 36–37 — другие NTC/RTD/Ni, шкала °C ×0.1 (если температурный тип).

```
/devices/mr02m-COM1-5/controls/ai_1               "23.5"
/devices/mr02m-COM1-5/controls/ai_1/meta/type     "temperature"
/devices/mr02m-COM1-5/controls/ai_1/meta/units    "°C"
/devices/mr02m-COM1-5/controls/ai_1/meta/error    ""
# ... до ai_12
```

---

## cyntron-dtv (RTU-Sensor)

Device ID: `dtv-{port}-{addr}` (пример: `dtv-COM3-1`)
Baudrate: 19200 (по умолчанию). Адрес: 1 (по умолчанию).
Все аналоговые значения — ×0.1 (кроме указанных).

```
/devices/dtv-COM3-1/meta/name           "DTV-RS-485 (COM3 addr=1)"
/devices/dtv-COM3-1/meta/driver         "modbus-rtu"
/devices/dtv-COM3-1/controls/uptime_s   type=value  (д/ч/м, без ед.)
/devices/dtv-COM3-1/controls/fw_version type=text

# Температуры (reg→channel)
# reg=1  temp_ds18b20    DS18B20         °C  ×0.1
# reg=2  temp_mcp9808    MCP9808         °C  ×0.1
# reg=3  temp_hdc1080    HDC1080         °C  ×0.1
# reg=4  temp_bme280     BME280          °C  ×0.1
# reg=5  temp_bme680     BME680          °C  ×0.1
# reg=6  temp_ext        NTC/Pt внешний  °C  ×0.1
/devices/dtv-COM3-1/controls/temp_bme680    type=temperature  units=°C

# Влажность
# reg=7  humidity_hdc1080  %RH  ×0.1
# reg=8  humidity_bme280   %RH  ×0.1
# reg=9  humidity_bme680   %RH  ×0.1
/devices/dtv-COM3-1/controls/humidity_bme680 type=rel_humidity units=%

# Давление
# reg=10 pressure_bme280_mmhg  mmHg  ×1.0
# reg=11 pressure_bme680_mmhg  mmHg  ×1.0
# reg=12 pressure_bme280_kpa   kPa   ×0.01
# reg=13 pressure_bme680_kpa   kPa   ×0.01
/devices/dtv-COM3-1/controls/pressure_bme680_kpa  type=pressure units=kPa

# Качество воздуха
# reg=16 gas_resist_bme680  kΩ     ×1.0
# reg=17 iaq_bme680         IAQ    ×1.0  (0-500)
# reg=18 eco2_bme680        ppm    ×1.0
# reg=19 tvoc_zmod          mg/m³  ×0.01
# reg=20 iaq_zmod           IAQ    ×1.0
# reg=21 eco2_zmod          ppm    ×1.0
/devices/dtv-COM3-1/controls/iaq_bme680    type=value units=IAQ
/devices/dtv-COM3-1/controls/tvoc_zmod     type=value units=mg/m³

# LD2412 присутствие и освещённость
# reg=25 light_pct        %    ×1.0
# reg=27 presence         0/1  (input register)
# reg=28 moving_distance  cm   ×1.0
# reg=29 still_distance   cm   ×1.0
# reg=30 detect_distance  cm   ×1.0
/devices/dtv-COM3-1/controls/presence      type=switch  readonly
/devices/dtv-COM3-1/controls/moving_distance type=value units=cm
/devices/dtv-COM3-1/controls/still_distance  type=value units=cm
/devices/dtv-COM3-1/controls/detect_distance type=value units=cm

# Управляемые выходы
/devices/dtv-COM3-1/controls/buzzer        type=switch
/devices/dtv-COM3-1/controls/buzzer/on    ← write "0"|"1"
/devices/dtv-COM3-1/controls/leds          type=switch
/devices/dtv-COM3-1/controls/leds/on      ← write "0"|"1"

# MCU диагностика
# reg=123 mcu_vdd   V  ×0.01
# reg=124 mcu_temp  °C ×0.1
/devices/dtv-COM3-1/controls/mcu_vdd      type=voltage     units=V
/devices/dtv-COM3-1/controls/mcu_temp     type=temperature units=°C
```

> **Примечание:** При 0x8000 в регистре → датчик не установлен или ошибка. Мост публикует `/meta/error = "r"`.

---

## CE-02m-3 — трёхфазный анализатор электроэнергии

Device ID: `ce02m3-{port}-{addr}` (пример: `ce02m3-COM2-14`)
Baudrate: 115200. Адрес: 14 (по умолчанию).

### Адресная карта

| Регистры | Данные | Формат | Масштаб |
|----------|--------|--------|---------|
| 500–502 | Напряжения фазные A/B/C | uint16 | ×0.1 В |
| 506–508 | Напряжения линейные AB/BC/CA | uint16 | ×0.1 В |
| 510–513 | Токи A/B/C/N | uint16 | ×0.001 А |
| 518–525 | Мощность активная A/B/C/Total | int32×4 | ×0.1 Вт |
| 526–533 | Мощность реактивная A/B/C/Total | int32×4 | ×0.1 вар |
| 534–541 | Мощность полная A/B/C/Total | int32×4 | ×0.1 ВА |
| 542 | Частота | uint16 | ×0.01 Гц |
| 543–546 | cos φ A/B/C/Total | int16×4 | ×0.001 |
| 547 | Температура ASIC | int16 | ×1 °C |
| 580–599 | Счётчики энергии (5×uint64) | uint64×5 | Вт·ч / вар·ч / ВА·ч |
| 600–611 | Счётчики по фазам (А×uint64) | uint64×3 | Вт·ч |

### Топики

```
/devices/ce02m3-COM2-14/meta/name       "CE-02m-3 (COM2 addr=14)"
/devices/ce02m3-COM2-14/meta/driver     "modbus-rtu"
/devices/ce02m3-COM2-14/controls/uptime_s     type=value  (д/ч/м, без ед.)

# Напряжения (В)
/devices/ce02m3-COM2-14/controls/voltage_a    type=voltage  units=V
/devices/ce02m3-COM2-14/controls/voltage_b    type=voltage  units=V
/devices/ce02m3-COM2-14/controls/voltage_c    type=voltage  units=V
/devices/ce02m3-COM2-14/controls/voltage_ab   type=voltage  units=V
/devices/ce02m3-COM2-14/controls/voltage_bc   type=voltage  units=V
/devices/ce02m3-COM2-14/controls/voltage_ca   type=voltage  units=V

# Токи (А)
/devices/ce02m3-COM2-14/controls/current_a    type=current  units=A
/devices/ce02m3-COM2-14/controls/current_b    type=current  units=A
/devices/ce02m3-COM2-14/controls/current_c    type=current  units=A
/devices/ce02m3-COM2-14/controls/current_n    type=current  units=A

# Активная мощность (Вт)
/devices/ce02m3-COM2-14/controls/power_a      type=power  units=W
/devices/ce02m3-COM2-14/controls/power_b      type=power  units=W
/devices/ce02m3-COM2-14/controls/power_c      type=power  units=W
/devices/ce02m3-COM2-14/controls/power_total  type=power  units=W

# Реактивная мощность (вар)
/devices/ce02m3-COM2-14/controls/reactive_a      type=value  units=var
/devices/ce02m3-COM2-14/controls/reactive_b      type=value  units=var
/devices/ce02m3-COM2-14/controls/reactive_c      type=value  units=var
/devices/ce02m3-COM2-14/controls/reactive_total  type=value  units=var

# Полная мощность (ВА) — опционально
/devices/ce02m3-COM2-14/controls/apparent_a      type=value  units=VA
/devices/ce02m3-COM2-14/controls/apparent_total  type=value  units=VA

# cos φ
/devices/ce02m3-COM2-14/controls/pf_a            type=value  units=cosφ
/devices/ce02m3-COM2-14/controls/pf_b            type=value  units=cosφ
/devices/ce02m3-COM2-14/controls/pf_c            type=value  units=cosφ
/devices/ce02m3-COM2-14/controls/pf_total        type=value  units=cosφ

# Частота, температура
/devices/ce02m3-COM2-14/controls/frequency       type=value  units=Hz
/devices/ce02m3-COM2-14/controls/asic_temp       type=temperature units=°C
# Диагностика МК (Input 123–124, как у MR/ДТВ): VDD ×0.01 В, temp ×0.1 °C
/devices/ce02m3-COM2-14/controls/mcu_vdd         type=voltage      units=V
/devices/ce02m3-COM2-14/controls/mcu_temp        type=temperature  units=°C

# Счётчики энергии (Вт·ч)
/devices/ce02m3-COM2-14/controls/energy_active_import    type=value  units=Wh
/devices/ce02m3-COM2-14/controls/energy_active_export    type=value  units=Wh
/devices/ce02m3-COM2-14/controls/energy_reactive_import  type=value  units=varh
/devices/ce02m3-COM2-14/controls/energy_reactive_export  type=value  units=varh
/devices/ce02m3-COM2-14/controls/energy_apparent         type=value  units=VAh

# По фазам (опционально, publish_per_phase_energy: true)
/devices/ce02m3-COM2-14/controls/energy_active_import_a  type=value  units=Wh
/devices/ce02m3-COM2-14/controls/energy_active_import_b  type=value  units=Wh
/devices/ce02m3-COM2-14/controls/energy_active_import_c  type=value  units=Wh
```

---

## Fast Modbus события

`fast_modbus` у устройства в `/etc/sa02m-modbus-mqtt.yaml` включает событийную
доставку быстрых каналов (Wiren Board Fast Modbus, FC 0x46). По умолчанию
**включено** для `mr02m` и `dtv`; для `ce02m3` — только явный
`fast_modbus: true` (после стабильного classic; ранний 0x18 на COM2 клинил СЭ).
Configure_events шлётся после classic warmup (`classic_ready_for_fmb`).
Поддерживаемые типы: `mr02m` (DO/DI/AO/AI), `dtv` (coils 1–2; Input 25–30),
`ce02m3` (Input 500–502 Uph, 510–513 Iph+N — как в прошивке CE EN_METER).
Медленные датчики ДТВ (рег. 1–24), энергия/мощность/диагностика СЭ — classic
insurance-опрос.

- **Топики и форматы не меняются** — событийная публикация байт-в-байт
  совпадает с полловой. Один поток на порт — как
  [wb-mqtt-serial](https://github.com/wirenboard/wb-mqtt-serial)
  `TSerialClientRegisterAndEventsReader` / TimeBalancer:
  - **EVENTS** (High): burst `poll_events` до 0x12 или 100 мс; период 50 мс
    @115200 (100 мс @≥38400, 200 мс ниже);
  - **POLLING** (Low): classic `poll_io` / diag в срезе ≤100 мс;
  - insurance ≥500 мс для каналов под событиями; при накоплении времени
    событий ≥500 мс — Force classic poll (`BALANCING_THRESHOLD`).
- **ДТВ, предусловие:** режим FMB в прошивке — Holding 122 = 1. При 0
  configure_events не подтверждается — мост логирует отказ и остаётся на
  классическом опросе.
- **Деградация по диапазонам:** каждый диапазон configure_events
  настраивается независимо; отклонённый диапазон покрывается классическим
  опросом (журнал: `configure_events … rejected — classic polling covers
  this range`), остальные получают события.
- **Два поколения формата.** Прошивки MR-02m новой ветки принимают только
  WB-форму кадра `configure_events` (0x18) и шлют события в WB-грамматике;
  прошивки ≤ 1.0.10.4x, ДТВ и СЭ-02м-3 — legacy-форму. Мост поддерживает обе:
  для `mr02m` сначала пробуется WB-кадр, при отсутствии ответа — legacy;
  подтверждённое поколение кешируется на адрес и сбрасывается при
  перезагрузке модуля. Цена зонда (в том числе для устройства, которое не
  подтверждает ни одну форму) — в контракте, здесь не пересказывается.
  Грамматики, порядок попыток и инварианты — **одно место:**
  [`docs/contracts/fmb-event-wire.md`](contracts/fmb-event-wire.md).
  Топики и форматы значений от поколения не зависят.
- **Ключ `fmb_event_wire: auto | wb | legacy`** (на устройство): `auto`
  (умолчание) — WB пробуется только для `mr02m` (неожиданная форма 0x18
  однажды заклинила СЭ-02м-3, § 1.0.5.46 CHANGELOG); `wb` — пробовать и на
  других типах; `legacy` — WB не отправлять.
- **Ключ `ai_read_chunk_regs`** (на устройство, `mr02m`): размер чанка FC03
  при чтении AI в регистрах, кратен 7 (1 канал = 7 рег.), умолчание 42.
  Меньшее значение лечит обрыв длинного ответа на загруженной линии ценой
  лишних транзакций; глобально — переменная окружения
  `SA02M_MR02M_AI_CHUNK_REGS`.

---

## Интервалы опроса (рекомендуемые)

Мост опрашивает **всю линию RS-485** (`/dev/COMn` + baud) **одним потоком**: после обхода всех адресов сразу следующий круг, **без паузы** (скорость = Modbus/RS-485). Отдельный поток на каждое устройство не используется.

| Параметр | Значение |
|----------|----------|
| Цикл порта | непрерывный round-robin |
| MR-02m DO/DI/AO/AI | каждый проход устройства в цикле |
| MR-02m диагностика | 60 с |
| DTV | Датчики + присутствие | каждый проход устройства |
| DTV | Диагностика | 60 с |
| CE-02m-3 | Мощность/Напряжения/Токи | каждый проход устройства |
| CE-02m-3 | Счётчики энергии | 60 с |
| CE-02m-3 | Диагностика | 120 с |
| СА-02м | Системная телеметрия | 30 с |

---

## Верификация

```bash
# Все устройства
mosquitto_sub -h 127.0.0.1 -v -t '/devices/#'

# SA-02m телеметрия (id = имя платы; проверить живой id — в журнале службы)
mosquitto_sub -h 127.0.0.1 -v -t '/devices/SA-02m/#' -C 20
mosquitto_pub -h 127.0.0.1 -t '/devices/SA-02m/controls/beeper/on' -m '1'

# MR-02m
mosquitto_sub -h 127.0.0.1 -v -t '/devices/mr02m-COM1-5/#' -C 30
mosquitto_pub -h 127.0.0.1 -t '/devices/mr02m-COM1-5/controls/do_1/on' -m '1'

# DTV
mosquitto_sub -h 127.0.0.1 -v -t '/devices/dtv-COM3-1/controls/+' -C 20
mosquitto_pub -h 127.0.0.1 -t '/devices/dtv-COM3-1/controls/buzzer/on' -m '1'

# CE-02m-3
mosquitto_sub -h 127.0.0.1 -v -t '/devices/ce02m3-COM2-14/controls/+' -C 30

# Ошибки опроса (по каналам)
mosquitto_sub -h 127.0.0.1 -v -t '/devices/+/controls/+/meta/error'

# Доступность устройств (device-level) и статус моста
mosquitto_sub -h 127.0.0.1 -v -t '/devices/+/meta/error'
mosquitto_sub -h 127.0.0.1 -v -t '/devices/sa02m-bridge/#'

# Внешний доступ (порт 1884)
mosquitto_sub -h 192.168.1.136 -p 1884 -u mqttuser -P <pass> -v -t '/devices/#'
```
