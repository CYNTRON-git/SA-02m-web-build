# Контракт: конфигурация AI-канала модуля (web-сторона флешер-демона)

Домашний адрес веб-контракта на **чтение и запись параметров аналогового входа
(AI)** модулей расширения MR-02м с AI (`12AI`, `6AI6AO`) через флешер-демон
(`opt/sa02m-flasher`). Машинная грамматика (номера регистров, JSON-поля,
имена полей) — на английском (`PROTOCOL.md` invariant 5); пояснения — на
русском (`docLanguage: ru`).

Реестр он-wire регистров принадлежит прошивке (`MR-02m/MODBUS_VARIABLES.txt`) и
здесь только **читается**, не дублируется (см. «Источник истины»). Веб пишет и
декодирует **строго** по нему; расхождение — находка ревью.

Реализация: `sa02m_flasher/device_config.py` (снимок `_read_ai_channels`,
allow-list `_allowed_mr_holding_registers`), `module_profiles.py` (карта
регистров канала). UI — `www/network_config/static/js/flasher.js`
(`renderModuleAiTab`, `applyAiCalibration`, `applyAiLimit`), строки — `i18n.js`.

## Раскладка регистров канала (эталон `MODBUS_VARIABLES.txt:315,400–406,261–262`)

Логический канал AI: `base = 400 + (ch−1)×7`, шаг **7** (`ai_channel_base_register`,
`ai_channel_stride`). Блок из 7 слов на канал:

| Смещение | Регистр (AI1) | Тип | Смысл | Доступ |
|---|---|---|---|---|
| `base+0` | 400 | Holding | тип датчика (код 0..42) | R/W |
| `base+1/+2` | 401/402 | Input | измеренное (raw s32) | **только чтение** |
| `base+3` | 403 | Input | пересчитанное (scaled) | **только чтение** |
| `base+4` | 404 | Holding | калибровка (смещение int16) | R/W |
| `base+5` | 405 | Holding | нижний предел измерения int16 | R/W |
| `base+6` | 406 | Holding | верхний предел измерения int16 | R/W |

Флаги аварии — **Input 107** (ниже предела) / **Input 108** (выше), по одному
биту на логический канал (бит `ch−1`, до 16 каналов), одно парное чтение на
снимок (`ai_input_limit_flags_for_channel`).

## Снимок AI-канала (`_read_ai_channels`, поля JSON — стабильный контракт)

Каждый элемент `mr.ai.channels[]` несёт (потребляется задеплоенными, в т.ч.
кешированными, бандлами — поля **аддитивны**, старый бандл игнорирует новые):

| Поле | Тип | Источник | Смысл |
|---|---|---|---|
| `channel` | int | — | номер канала (1-based) |
| `register_base` | int | `ai_channel_base_register` | `base` канала (**никогда не пересчитывать из индекса на клиенте** — писать по нему) |
| `sensor_code` | int | Holding `base+0` | код типа датчика 0..42 |
| `sensor_label`, `sidebar_tag`, `ui_bucket` | str | производные от `sensor_code` | подписи/группа |
| `measured_raw`, `scaled_raw` | int/null | Input `base+1..3` | текущие значения |
| `calibration` | int (s16) | Holding `base+4` | смещение калибровки |
| `limit_low` | int (s16) | Holding `base+5` | нижний предел измерения |
| `limit_high` | int (s16) | Holding `base+6` | верхний предел измерения |
| `fault_low` | bool | Input 107, бит `ch−1` | ниже предела / обрыв |
| `fault_high` | bool | Input 108, бит `ch−1` | выше предела / КЗ |
| `filters` | obj/absent | Holding фильтров | только `12AI`/`6AI6AO` |

**Применимость калибровки — не поле снимка.** Выводится на клиенте из
`sensor_code` (`ai_ui_uses_value_calibration`: температура ∪ напряжение ∪ ток;
скрыта для «Выключен» и «сухого контакта»). Единственный источник — `sensor_code`;
прежнее поле `calibration_applicable` (только-температура) **удалено** из снимка,
чтобы не иметь двух расходящихся источников. Старый кешированный бандл при
отсутствии поля падает на свой клиентский вывод (null-safe) — регрессии нет.

**Декод аварии (клиент, эталон `ai_input_limit_range_message`):** только для
активных/температурных режимов (`volt/curr/ntc/rtd/tc_k`). Температурные:
`fault_low`→«Обрыв датчика», `fault_high`(NTC/RTD)→«Короткое замыкание на линии».
Активные: «Ниже/Выше диапазона измерения». «Выключен»/«сухой контакт» — без аварии.

## Граница записи (allow-list, `_allowed_mr_holding_registers`)

Запись Holding-регистров разрешена **только** по закрытому списку. На каждый
AI-канал гранты — **ровно**:

- `base+0` (тип датчика),
- `base+4` (калибровка, `ai_calibration_holding_register`),
- `base+5` (нижний предел), `base+6` (верхний предел),
- регистры фильтров (Kalman/SPS/avg/tau) — только `12AI`/`6AI6AO`.

`base+1/+2/+3` (измеренное/пересчитанное, Input) — **никогда не записываемы**.
Запись любого регистра вне списка → `ValueError` в `write_allowed_holding`
(fail-closed, отказ, не ложный `ok`). Это граница безопасности: неверный/чужой
регистр из запроса отклоняется до какой-либо записи (`web-code-rigor.md`).

**Клампы записи (клиент):** предел и калибровка активных режимов — весь int16
`[−32768, 32767]`; калибровка температуры — ±100. Порядок `lo < hi` не
навязывается (как в эталоне). Запись — по `register_base` из снимка через
`signedToUint16`, под edit-guard (опрос 1 с не затирает набираемое значение);
при отказе поле откатывается к снимку.

## Идиома ошибок

Как в `mqtt-set-endpoint.md`: транспорт остаётся управляемым, ошибка — в теле
ответа демона; тишина/сбой чтения не выдаётся за успех. Опрос конфигурации —
модаль-скоуп (1 с, только при открытом окне), не на дашборд-хот-пути; авария
читает Input 107/108 одним парным чтением на снимок (O(1), не на канал).

## Валидирующая проверка

Автоматическая, `py-unit-flasher` (`opt/sa02m-flasher/tests/test_device_config.py`):

1. `test_allowed_holding_regs_grant_ai_channel_offsets` — для `6AI6AO` на каждый
   AI-канал `_allowed_mr_holding_registers` содержит `base`, `base+4`, `base+5`,
   `base+6` (и `base+4 == ai_calibration_holding_register`) и **не** содержит
   `base+1/+2/+3` (Input-регистры не записываемы).
2. `test_ai_snapshot_exposes_limit_and_fault_fields` — `_read_ai_channels` (с
   мокнутыми чтениями) отдаёт поля `limit_low/limit_high/calibration/fault_low/
   fault_high`, корректно декодит бит `ch−1` из слов Input 107/108 и **не** несёт
   поля `calibration_applicable`.

## Источник истины (он-wire — НЕ дублировать здесь)

- `MR-02m/MODBUS_VARIABLES.txt` — раскладка регистров канала AI (`:315,400–406`),
  флаги 107/108 (`:261–262`), кодировка пределов (`:405–412`).
- `MR-02m-flasher/flasher_windows/module_profiles.py` — эталонные предикаты
  (`ai_ui_uses_value_calibration`, `ai_calibration_clamp`,
  `ai_sensor_uses_input_range_limit_registers`, `ai_input_limit_flags_for_channel`,
  `ai_input_limit_range_message`), которым следует веб.
