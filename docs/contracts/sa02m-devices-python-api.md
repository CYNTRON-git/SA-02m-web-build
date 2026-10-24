# Контракт: публичный Python-API пакета `sa02m_devices` (межпроектный)

Домашний адрес контракта на **имена и формы вызова**, которые сторонний проект
импортирует из пакета `sa02m_devices` (`opt/sa02m-devices/`, на плате —
`/opt/sa02m-devices`). Введён 2026-09-30 по решению Оператора («сделай как
правильно»): переименование, удаление или смена keyword-параметра здесь ломало
сервис в другом репозитории, а падающего теста с этой стороны не было. Машинная
грамматика (имена модулей, функций, параметров, таблиц) — на английском
(`PROTOCOL.md` invariant 5); пояснения — на русском (`docLanguage: ru`).

## 1. Потребитель

**Стенд HardPy** — соседний репозиторий `hardpy_tests` (объявлен в
`.ai-dev/components.json`), развёрнут на стенде 1.135 в `/opt/hardpy_tests`.
Его сервис `services/stand_web_api.py` и логгер `scripts/sa02m_devices_logger.py`
импортируют пакет по пути из `SA02M_DEVICES_ROOT` (`/opt/sa02m-devices`); когда
пакета нет — используют свою вендорную копию `lib/`. Потребитель **не выполняет
DDL**: схему архива создаёт и мигрирует только этот пакет (§4).

## 2. Поверхность контракта

Ниже — какие имена и где они живут. **Грамматика вызова** (какие параметры
передаются по ключу, сколько ведущих позиционных) **здесь не повторяется**: её
единственный дом — таблица `CONTRACT` в валидирующем тесте
`opt/sa02m-devices/tests/test_python_api_contract.py`. Тест сверяет набор имён
этой таблицы с таблицей ниже (маркеры `names:begin`/`names:end`), так что
расхождение документа и теста — красный прогон, а не тихий дрейф.

Правило формы (одно для всех имён): параметр, который потребитель передаёт **по
ключу**, сохраняет имя; позиционные параметры могут быть переименованы;
`**kwargs` допустим как приёмник ключей. Значения по умолчанию, типы и формы
возврата контракт не фиксирует, кроме двух названных в §3.

<!-- names:begin -->
| Модуль | Имя | Где определено | Назначение |
|---|---|---|---|
| `sa02m_devices.device_history_db` | `history` | `history_query.py` | серия одной метрики за окно |
| `sa02m_devices.device_history_db` | `history_batch` | `history_query.py` | серии группы метрик (`climate`/`energy`) |
| `sa02m_devices.device_history_db` | `history_mr` | `history_mr.py` | AI-канал MR-02m (`docs/contracts/devices-mr-history.md`) |
| `sa02m_devices.device_history_db` | `history_mr_batch` | `history_mr.py` | обзор всех AI-каналов модуля |
| `sa02m_devices.device_history_db` | `history_carel` | `history_query.py` | метрика Carel (`docs/contracts/carel-ahu.md` §7) |
| `sa02m_devices.device_history_db` | `history_carel_batch` | `history_query.py` | обзор метрик Carel |
| `sa02m_devices.device_history_db` | `export_text` | `history_export.py` | выгрузка TSV → `(body, filename)` |
| `sa02m_devices.device_history_db` | `export_xlsx` | `history_export.py` | выгрузка xlsx → `(bytes, filename)` |
| `sa02m_devices.device_history_db` | `period_summary_ce` | `history_query.py` | сводка СЭ за период (мощность, ΔE, стоимость) |
| `sa02m_devices.device_history_db` | `storage_status` | `history_store.py` | активный носитель/путь архива |
| `sa02m_devices.device_history_db` | `custom_range_key` | `history_ranges.py` | ключ произвольного окна |
| `sa02m_devices.device_history_db` | `db_path` | `history_store.py` | путь к SQLite (USB → SD → eMMC) |
| `sa02m_devices.device_history_db` | `ensure_schema` | `history_store.py` | единственный вход DDL/миграций (§4) |
| `sa02m_devices.device_history_db` | `insert_sample` | `history_write.py` | запись снимка ДТВ/СЭ |
| `sa02m_devices.device_history_db` | `purge_old` | `history_write.py` | ретенция 30 д |
| `sa02m_devices.device_history_db` | `rotate_if_needed` | `history_store.py` | ротация по размеру |
| `sa02m_devices.stand_devices` | `live_snapshot` | `stand_devices.py` | живой снимок устройств из кеша MQTT |
| `sa02m_devices.stand_devices` | `parse_device_id` | `stand_devices.py` | разбор `device_id` → kind/port/addr |
| `sa02m_devices.devices_widgets` | `load` | `devices_widgets.py` | конфиг карточек |
| `sa02m_devices.devices_widgets` | `config_path` | `devices_widgets.py` | путь к конфигу карточек |
| `sa02m_devices.devices_widgets` | `apply_widgets_view` | `devices_widgets.py` | фильтр снимка для UI |
| `sa02m_devices.devices_widgets` | `add_widget` | `devices_widgets.py` | вернуть карточку |
| `sa02m_devices.devices_widgets` | `remove_widget` | `devices_widgets.py` | снять карточку |
| `sa02m_devices.devices_widgets` | `filter_for_archive` | `devices_widgets.py` | фильтр снимка перед записью |
| `sa02m_devices.device_events` | `list_events` | `device_events.py` | события, новые первыми |
| `sa02m_devices.device_events` | `detect_ce_events` | `device_events.py` | детектор событий СЭ по снимку |
| `sa02m_devices.device_events` | `purge_events` | `device_events.py` | ретенция событий |
| `sa02m_devices.websession` | `check_session_store` | `websession.py` | живая ли сессия по cookie |
| `sa02m_devices.websession` | `check_csrf` | `websession.py` | проверка CSRF → `(ok, reason)` |
| `sa02m_devices.websession` | `csrf_error_body` | `websession.py` | тело ошибки `E_CSRF` |
| `sa02m_devices.websession` | `CSRF_HEADER` | `websession.py` | константа: имя заголовка CSRF |
| `sa02m_devices.websession` | `DEFAULT_SESSION_DIR` | `websession.py` | константа: каталог сессий |
<!-- names:end -->

Все имена `device_history_db` — реэкспорт фасада `device_history_db.py` из
модулей `history_*` (колонка «Где определено»); потребитель импортирует только
через фасад. `websession.py` — побайтовая копия флешерного (ряд
`websession-parity`).

## 3. Формы возврата, на которые опирается потребитель

- `check_csrf(...)` → кортеж `(ok: bool, reason: str)`; `reason` — из
  `CSRF_REASONS` либо `""` при успехе.
- `export_text` / `export_xlsx` → пара `(содержимое, имя_файла)`.

## 4. Владение схемой архива

- DDL и миграции таблиц `dtv_samples`, `ce_samples`, `mr_samples`,
  `carel_samples` выполняет **только** `ensure_schema()` этого пакета
  (`history_store.py`). HardPy не создаёт и не изменяет таблицы.
- `carel_samples` — **широкая** раскладка: колонка на метрику
  (`CAREL_COLUMNS`), PK `(ts, device_id)`; длинная форма 1.0.6.40
  (`metric`/`value`/`unit`) мигрируется при первом открытии и не возвращается
  (`docs/contracts/carel-ahu.md` §7).
- `mr_samples` остаётся длинной (`docs/contracts/devices-mr-history.md`).

## 5. Правило стабильности

- **Имя, keyword-параметр или таблица из §2/§4 меняется только через шаг
  совместимости**: старое имя/ключ живёт рядом с новым не меньше одного релиза
  (алиас, `**kwargs`-приёмник, `DeprecationWarning`), а запись об этом идёт в
  `CHANGELOG.md` в разделе релиза под фиксированным заголовком
  **«Контракт sa02m_devices»** — это то место, за которым следит HardPy.
  Удаление старого имени — отдельный релиз, тоже под этим заголовком.
- **Точность и округление значений могут меняться без смены сигнатур** (это
  изменение значения, не контракта): например, `energy_kwh_import` переходит с
  0,1 кВт·ч на 3 знака (= 1 Вт·ч) в ветке `ce-energy-wh`. Такое изменение
  тоже объявляется под заголовком «Контракт sa02m_devices», чтобы потребитель
  проверил свои пороги и форматы.
- Добавление нового имени или нового keyword-параметра с умолчанием —
  совместимо, объявляется в обычном разделе релиза; в таблицу §2 попадает,
  когда его начинает импортировать потребитель.

## 6. Валидирующие проверки — две половины

- **Здесь:** `opt/sa02m-devices/tests/test_python_api_contract.py`
  (ряд `devices-python-api-contract` реестра `.ai-dev/quality/tools.json`,
  build-beat; тот же файл собирает и `py-unit-devices`). Через
  `inspect.signature` проверяет каждое имя таблицы `CONTRACT`: существует,
  принимает контрактные ключи (или `**kwargs`) и ведущие позиционные;
  константы; кортеж `check_csrf`; что `ensure_schema()` сам создаёт четыре
  таблицы и `carel_samples` широкая; что таблица §2 и `CONTRACT` называют один
  и тот же набор имён. Ряд закреплён в `comment-mutation-proof`: закомментированный
  реэкспорт в фасаде делает его красным.
- **На стороне HardPy:** `scripts/check_devices_contract.py --expect upstream`
  на плате — зеркало той же таблицы против установленного `/opt/sa02m-devices`;
  падает по тем же причинам. Одно изменение здесь → красный там до шага
  совместимости из §5.
