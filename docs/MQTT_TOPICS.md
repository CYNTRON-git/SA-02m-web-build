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
| Запись значения | `/controls/{name}/on` ← write `0` / `1` / float |
| Публикация meta | Один раз при старте (retained) |

---

## Схема Device ID

| Тип | Device ID | Пример |
|-----|-----------|--------|
| СА-02м контроллер | `sa02m-{hostname}` | `sa02m-SA-02` |
| MR-02м модуль | `mr02m-{port}-{addr}` | `mr02m-COM1-5` |
| cyntron-dtv | `dtv-{port}-{addr}` | `dtv-COM3-1` |
| CE-02m-3 счётчик | `ce02m3-{port}-{addr}` | `ce02m3-COM2-14` |

---

## СА-02м — системная телеметрия

Device ID: `sa02m-{hostname}` (пример: `sa02m-SA-02`)

```
/devices/sa02m-SA-02/meta/name          "СА-02м (192.168.1.136)"
/devices/sa02m-SA-02/meta/driver        "sa02m-telemetry"

# Системные показатели (интервал 30 с)
/devices/sa02m-SA-02/controls/cpu_pct           type=value      %
/devices/sa02m-SA-02/controls/temp_c            type=temperature °C
/devices/sa02m-SA-02/controls/ram_pct           type=value      %
/devices/sa02m-SA-02/controls/uptime_s          type=value      с

# Управляемые выходы PCA9536 (R/W)
/devices/sa02m-SA-02/controls/do                type=switch
/devices/sa02m-SA-02/controls/do/on             ← write 0/1
/devices/sa02m-SA-02/controls/beeper            type=switch
/devices/sa02m-SA-02/controls/beeper/on         ← write 0/1
/devices/sa02m-SA-02/controls/alarm_led         type=switch
/devices/sa02m-SA-02/controls/alarm_led/on      ← write 0/1

# RS-485 статистика (5 портов: com1..com5)
/devices/sa02m-SA-02/controls/rs485_com1_tx     type=value
/devices/sa02m-SA-02/controls/rs485_com1_rx     type=value
/devices/sa02m-SA-02/controls/rs485_com1_errors type=value
# ... rs485_com2_* ... rs485_com5_*
```

---

## MR-02м / MP-02м — модули расширения

Device ID: `mr02m-{port}-{addr}` (пример: `mr02m-COM1-5`)

Тип модуля определяется из Input reg 0. Каждый тип имеет фиксированное количество каналов:

| Код | Имя | DO | DI | AO | AI |
|-----|-----|----|----|----|----|
| 1 | DO6DI8 | 6 | 8 | 0 | 0 |
| 2 | DO16 | 16 | 0 | 0 | 0 |
| 3 | DI14 | 0 | 14 | 0 | 0 |
| 4 | DO6 | 6 | 0 | 0 | 0 |
| 5 | DO4DI6 | 4 | 6 | 0 | 0 |
| 6 | AO12 | 0 | 0 | 12 | 0 |
| 7 | AI12 | 0 | 0 | 0 | 12 |
| 8 | AO6AI6 | 0 | 0 | 6 | 6 |
| 9 | AI6AO2 | 0 | 0 | 2 | 6 |
| 11 | DO4DI4AO2AI4 | 4 | 4 | 2 | 4 |
| 12 | DI8AI4 | 0 | 8 | 0 | 4 |
| 13 | DO2DI4AI2 | 2 | 4 | 0 | 2 |
| 14 | DO6DI4AI4 | 6 | 4 | 0 | 4 |
| 15 | DO4DI4AO4AI4 | 4 | 4 | 4 | 4 |

### Мета-информация
```
/devices/mr02m-COM1-5/meta/name         "MR-02м DO6DI8 (COM1 addr=5)"
/devices/mr02m-COM1-5/meta/driver       "modbus-rtu"
/devices/mr02m-COM1-5/controls/module_type   type=text
/devices/mr02m-COM1-5/controls/uptime_s     type=value  с
/devices/mr02m-COM1-5/controls/serial        type=text
```

### DO (дискретные выходы — coils 1..N, R/W)
```
/devices/mr02m-COM1-5/controls/do_1     type=switch  "0"|"1"
/devices/mr02m-COM1-5/controls/do_1/on ← write "0"|"1"
# ... до do_16
```

### DI (дискретные входы — discrete inputs 18..17+N, R)
```
/devices/mr02m-COM1-5/controls/di_1    type=switch  readonly
# ... до di_14
/devices/mr02m-COM1-5/controls/di_1_count  type=value  (uint32, счётчик)
/devices/mr02m-COM1-5/controls/di_1_freq   type=value  Hz
```

### AO (аналоговые выходы — holding regs 33..32+N, 0–1000 ‰, R/W)
```
/devices/mr02m-COM1-5/controls/ao_1    type=range  min=0 max=1000
/devices/mr02m-COM1-5/controls/ao_1/on ← write 0–1000
# ... до ao_12
```

### AI (аналоговые входы — holding regs 400+7*n, R)
Значение регистра +3 масштабируется по типу датчика.

| Код датчика | Тип | Единицы | Масштаб |
|-------------|-----|---------|---------|
| 1 | NTC 10kΩ | °C | ×0.1 |
| 2 | Pt100 2-wire | °C | ×0.1 |
| 3 | Pt1000 2-wire | °C | ×0.1 |
| 4 | Pt100 3-wire | °C | ×0.1 |
| 5 | 0-10V | V | ×0.001 |
| 6 | 4-20mA | mA | ×0.001 |
| 7 | Термопара K | °C | ×0.1 |
| 8 | Сухой контакт | — | — |
| 9 | Pt100 4-wire | °C | ×0.1 |
| 10 | Pt500 2-wire | °C | ×0.1 |
| 11 | Сопротивление 2-wire | Ω | ×0.01 |

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
/devices/dtv-COM3-1/controls/uptime_s   type=value  с
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

# Управляемые выходы
/devices/dtv-COM3-1/controls/buzzer        type=switch
/devices/dtv-COM3-1/controls/buzzer/on    ← write "0"|"1"
/devices/dtv-COM3-1/controls/leds          type=switch
/devices/dtv-COM3-1/controls/leds/on      ← write "0"|"1"

# MCU диагностика
# reg=123 mcu_vdd   V  ×0.01
# reg=124 mcu_temp  °C ×1.0
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
/devices/ce02m3-COM2-14/controls/uptime_s     type=value  с

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

## Интервалы опроса (рекомендуемые)

| Устройство | Данные | Интервал |
|------------|--------|----------|
| MR-02m | DO/DI | 1 с |
| MR-02m | AI/AO | 5 с |
| MR-02m | Диагностика | 60 с |
| DTV | Датчики | 10 с |
| DTV | Присутствие LD2412 | 2 с |
| DTV | Диагностика | 60 с |
| CE-02m-3 | Мощность/Напряжения/Токи | 5 с |
| CE-02m-3 | Счётчики энергии | 60 с |
| CE-02m-3 | Диагностика | 120 с |
| СА-02м | Системная телеметрия | 30 с |

---

## Верификация

```bash
# Все устройства
mosquitto_sub -h 127.0.0.1 -v -t '/devices/#'

# SA-02m телеметрия
mosquitto_sub -h 127.0.0.1 -v -t '/devices/sa02m-SA-02/#' -C 20
mosquitto_pub -h 127.0.0.1 -t '/devices/sa02m-SA-02/controls/beeper/on' -m '1'

# MR-02m
mosquitto_sub -h 127.0.0.1 -v -t '/devices/mr02m-COM1-5/#' -C 30
mosquitto_pub -h 127.0.0.1 -t '/devices/mr02m-COM1-5/controls/do_1/on' -m '1'

# DTV
mosquitto_sub -h 127.0.0.1 -v -t '/devices/dtv-COM3-1/controls/+' -C 20
mosquitto_pub -h 127.0.0.1 -t '/devices/dtv-COM3-1/controls/buzzer/on' -m '1'

# CE-02m-3
mosquitto_sub -h 127.0.0.1 -v -t '/devices/ce02m3-COM2-14/controls/+' -C 30

# Ошибки опроса
mosquitto_sub -h 127.0.0.1 -v -t '/devices/+/controls/+/meta/error'

# Внешний доступ (порт 1884)
mosquitto_sub -h 192.168.1.136 -p 1884 -u mqttuser -P <pass> -v -t '/devices/#'
```
