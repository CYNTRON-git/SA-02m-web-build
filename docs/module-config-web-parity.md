# MR/MP-02m web config window — parity contract

Эталон: desktop `MR-02m-flasher` (`module_config_window.py`, `module_profiles.py`, `docs/module-config-poll-write-tz.md`). Рабочая реализация: `www/network_config/static/js/flasher.js`, `device_config.py`, `module_profiles.py`, `service.py`.

## Sidebar order (MR)

| # | Tab id   | Условие показа                         | Live-подпись (web)        |
|---|----------|----------------------------------------|---------------------------|
| 1 | `info`   | всегда                                 | —                         |
| 2 | `network`| всегда                                 | —                         |
| 3 | `relay`  | только `DO6DI8`, `DO4DI6`              | —                         |
| 4 | `di_*`   | `max_di > 0`; **до** DO                | активен / не активен      |
| 5 | `do_*`   | `max_do > 0`                           | ВКЛ / ВЫКЛ                |
| 6 | `ao_*`   | `max_ao > 0`                           | текущее напряжение        |
| 7 | `ai_*`   | `max_ai > 0`                           | короткий тег режима (sidebar_tag) |

## Polling

| Режим              | Когда                         | Содержимое `mr`                                      |
|--------------------|-------------------------------|------------------------------------------------------|
| `full`             | открытие окна, смена вкладки, после записи сети/holding/coil | полный снимок (все блоки DO/DI/AO/AI) |
| `minimal`          | совместимость / редкий режим | только live сайдбара (без полных DI/AO/AI настроек) |
| `panel`            | фоновый опрос таймера (~4 с) на открытой вкладке IO | minimal + полный Modbus-блок, соответствующий `active_tab` (`do_*`, `di_*`, `ao_*`, `ai_*`, `relay`) |

Поле ответа API: `snapshot_detail`: `"full"` \| `"minimal"` \| `"panel"`. Запрос: `snapshot_detail`, `active_tab` в теле `POST /device_config/snapshot`.

При `panel` бэкенд читает облегчённый кадр сайдбара и дополняет его полным блоком DO/DI/AO/AI/реле для текущей вкладки (см. `device_config._read_mr_snapshot_panel`).

На вкладке «Сеть» при **локально изменённых** полях фоновые снимки `minimal`/`panel` после merge не перезаписывают `network` (аналог desktop: не затирать ввод).

## Modbus: ключевые регистры (MR)

| Диапазон / регистр | Назначение |
|--------------------|------------|
| 110–112, 122, 128  | линия RS-485, Fast Modbus, адрес |
| 130, 131, 622      | релейный режим, опции, stagger |
| **134**            | **общий** Modbus inactivity (сек), один на модуль |
| 135                | сброс счётчиков DO |
| 600..599+N         | безопасное состояние DO |
| 616..621           | таймерные слова DO1..DO6 только |
| 623..628           | redelay DO1..DO6 только |
| 630..              | режим DI по каналам |
| 646..              | debounce DI |
| 662..              | long press |
| 678..              | double-click window |
| 694                | сброс счётчиков DI |
| 750..757           | режим частоты DI (до 8 каналов) |
| 33..               | уставка AO (holding) |
| 503..              | безопасное AO (`ao_safe_holding_register`; для 6AO6AI — тот же базовый блок) |
| 400 + stride×(ch−1)| AI: тип в word 0; калибровка +4 |
| 491 + stor, 533+3×stor | Kalman, фильтр WB (6AO6AI / 12AI) |

Катушки DO: с `1` по `max_do`. Для релейных модулей перед FC05 допускается попытка записи **130=0** (ручной режим); таймаут не блокирует DO.

## Семейства (охват)

| Код / тип      | relay | DI перед DO | Таймер DO | freq DI ≤8 | AO safe | AI stride / фильтры |
|----------------|-------|-------------|-----------|------------|---------|---------------------|
| DO6DI8         | да    | да          | DO1–6     | да         | 503+ch−1| —                   |
| DO4DI6         | да    | да          | DO1–4     | да         | —       | —                   |
| DO16           | нет   | —           | DO1–6     | —          | —       | —                   |
| DO6            | нет   | —           | DO1–6     | —          | —       | —                   |
| DI14           | нет   | да          | —         | да         | —       | —                   |
| DI10CON        | нет   | да          | —         | да (≤8)    | —       | —                   |
| DO5DI2AO       | нет   | да          | DO1–6*    | да         | 503+ch−1| —                   |
| TO4DI6         | нет   | да          | DO1–4     | да         | см. AO  | см. AO/AI          |
| AO12           | нет   | —           | —         | —          | 503+ch−1| stride 14           |
| AO6AI6         | нет   | да**        | —         | —          | 503+ch−1| stride 7, фильтры stor 6–11 |
| AI12           | нет   | —           | —         | —          | —       | stride 7, фильтры stor 0–11 |

\*Таймеры только где есть соответствующий канал DO и `≤6`.  
**Порядок: при наличии DI/DO — сначала DI, затем DO.

## AI UI (desktop parity)

- Группировка: «режим» (bucket) + динамический список подтипов из `AI_SENSOR_CHOICES`.
- **Порядок кнопок режима** (как в desktop `ai_ui_mode_radio_labels()`): Выключен → NTC → RTD → 0–10 В → 4–20 мА → **ТХА → DIN** (ТХА до DIN).
- **Коды датчиков**: полный диапазон 0x0000–0x0026, включая `0x0026` (Напряжение 0–30 В) в группе «0–10 В» (bucket `volt`). Источник истины — `MR-02m-flasher/flasher_windows/module_profiles.py` `AI_SENSOR_CODES`.
- **RTD 2/3-провод**: коды 0x001B–0x0025 — трёхпроводные; 0x0002–0x0014 (кроме не-RTD) — двухпроводные. Переключатель 2/3-провод показывается только для режима RTD.
- Калибровка (int16, base+4): только для температурных датчиков (NTC, Pt/Ni, ТХА и т.д.), не для «Выключен», напряжения/тока — см. `ai_ui_temperature_calibration_applicable()` в `module_profiles.py`.
- Фильтры Kalman / SPS / avg / tau: только `6AO6AI` и `12AI`, слоты как в прошивке (`ai_stor_for_6ao6ai_p` / `ai_stor_for_12ai_channel`).

## Приёмка

См. `docs/module-config-hardware-parity-checklist.md`. Автотесты: `opt/sa02m-flasher/tests/test_device_config.py`, `test_module_profiles_policy.py`.
