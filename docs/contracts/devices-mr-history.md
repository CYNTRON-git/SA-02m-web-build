# Контракт: история AI-каналов MR-02m (devices-API + архив)

Домашний адрес контракта на **историю (графики + экспорт) аналоговых каналов
модулей MR-02m** (`12AI`, `6AI6AO`, `6AI2AO`) во вкладке «Устройства». Введён в
1.0.5.85: клик по карточке AI-модуля открывает то же окно графиков, что у ДТВ.
Машинная грамматика (маршруты, поля JSON, DDL) — на английском
(`PROTOCOL.md` invariant 5); пояснения — на русском (`docLanguage: ru`).

Реализация: `opt/sa02m-devices/sa02m_devices/device_history_db.py`
(`mr_samples`, `insert_mr_sample`, `history_mr`, `history_mr_batch`,
`collect_export_table_mr`), `sa02m_devices_logger.py` (запись каждые
`STAND_DEVICES_MR_INTERVAL_S`, по умолчанию 10 с), `api.py` (`handle_history`
ветка `kind=mr`, экспорт `kind=mr`). UI — `www/network_config/static/js/devices.js`
(та же модалка/движок Canvas 2D, что у ДТВ; точность оси —
`aiUnitPrecision` из `ai-sensors.js`).

Валидирующие тесты (проверяют форму ниже): `opt/sa02m-devices/tests/test_api_mr.py`
(маршрутизация канал/обзор, регресс ДТВ, экспорт) и
`tests/test_device_history_db.py` (вставка, отключённый канал, единица,
последняя-в-окне единица, purge, изоляция от dtv/ce).

## Архив (SQLite, «длинная» таблица — решение Оператора 2026-08-18)

```sql
CREATE TABLE IF NOT EXISTS mr_samples (
    ts        REAL    NOT NULL,
    device_id TEXT    NOT NULL DEFAULT '',
    ch        INTEGER NOT NULL,
    value     REAL,
    unit      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (ts, device_id, ch)
);
CREATE INDEX IF NOT EXISTS idx_mr_device_ch_ts ON mr_samples(device_id, ch, ts);
```

- Создаётся идемпотентно в `_connect()`; таблицы/запросы ДТВ/СЭ **не меняются**
  (развёртывание поверх живой базы неразрушающее).
- Строка пишется только для **включённого** канала (`sensor_code != 0`) с
  конечным значением; отключённый канал строк **не пишет** и на графике скрыт.
- `unit` хранится **на каждый отсчёт**: при смене типа датчика в середине
  истории читатель берёт последнюю единицу в окне, точки не «сшиваются»
  (честный разрыв).
- Ротация: тот же `STAND_DEVICES_RETENTION_S` (30 дней), purge удаляет
  `mr_samples` не расширяя purge ДТВ/СЭ.
- Каденс записи независим от тика ДТВ/СЭ (1 с USB/SD, 5 с eMMC): MR — 10 с
  (`STAND_DEVICES_MR_INTERVAL_S`), ≤12 строк/10 с на 12AI, без нового I/O к
  устройству (читается уже собранный `live_snapshot()`).

## HTTP (через nginx `/api/devices*` → `sa02m-devices-api` :8765)

`GET /api/devices/history?kind=mr&device_id=<id>&range=<key>[&channel=<N>]`

- `range` — как у ДТВ (`1h` по умолчанию, ключи `RANGES`).
- С `channel=N` → **один канал**: `history_mr()`.
- Без `channel` → **обзор** всех включённых каналов: `history_mr_batch()`
  (форма как у `history_batch()` ДТВ, `group:"all"`).
- Без `kind=mr` маршруты ДТВ/СЭ (`metric=`/`group=`) **не изменены**.

Один канал (`ok:true`):

| Поле | Тип | Смысл |
|---|---|---|
| `metric` | `"ai_<N>"` | ключ канала |
| `label` | `"AI <N>"` | подпись чипа (языково-нейтральна) |
| `unit` | string | единица последнего отсчёта в окне (`""` если нет точек) |
| `decimals` | `null` | точность задаёт UI по единице (`aiUnitPrecision`) |
| `device` | `"mr"` | вид устройства |
| `device_id` | string | `mr02m-COM<p>-<a>` |
| `range` | string | ключ окна |
| `series` | array | `[{field,label,unit,points:[[ts,value],…]}]` |

Обзор (`ok:true`): `range`, `group:"all"`, `device:"mr"`, `device_id`,
`metrics:[…]` — каждый элемент как «один канал» выше со своей `series`.

Экспорт: `GET /api/devices/history/export?kind=mr&device_id=…&range=…&fmt=…` —
тот же общий экспортёр (`export_text` / `export_xlsx`), колонки = включённые
каналы (`AI <N>, <unit>`), новых форматов нет.

## Совместимость / гарантии

- Аддитивно: у старого фронтенда (кэшированный бандл) без `kind=mr` ничего не
  меняется; MR-карточка до 1.0.5.85 клик не обрабатывала.
- Изменение формы ответа `kind=mr`, DDL `mr_samples` или каденса — правка этого
  файла + тестов в одном PR.
