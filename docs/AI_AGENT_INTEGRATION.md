# Руководство: API для ИИ-агентов

Служба `sa02m-agent-api` по умолчанию выключена. Карточка «API для ИИ-агентов»
на вкладке «Управление» её включает и выпускает токен. Секрет показывается
один раз. Этот файл — как подключиться и как вызывать операции. Полный перечень
операций, коды и границы — `docs/contracts/agent-api.md`. Машиночитаемая схема —
`GET /api/v1/openapi.json` (нужен токен). Текст, который плата отдаёт агенту
при MCP `initialize`, — ресурс `sa02m://guide`.

## Пять минут

1. Включите службу на карточке. Создайте токен со scope «Чтение».
2. Нажмите «Копировать конфиг MCP». В нём адрес `http://<ip-платы>:9999/mcp`
   и заголовок `X-SA02M-Token`.
3. Cursor: Settings → MCP → вставьте этот JSON. Claude Desktop: тот же блок в
   конфиг MCP-серверов.
4. Проверка без MCP:

```bash
curl -sS -H "X-SA02M-Token: sa02m_<id>.<secret>" \
  http://192.168.1.136:9999/api/v1/system/info
```

Ответ: `{"ok":true,"result":{"version":"…","api":"sa02m-agent-api",…}}`.

Тот же заголовок проходит через облачный туннель `cloud.cyntron.ru` (порт 9999
уже в списке туннеля). `Authorization: Bearer sa02m_<id>.<secret>` демон тоже
принимает, но облако может его срезать — для туннеля оставляйте `X-SA02M-Token`.

Если карточка пишет, что маршрут `/api/v1` в nginx не настроен, GitHub-OTA
файл сайта не привёз. Обновите плату офлайн-пакетом или `install.sh --refresh`.
Пока маршрута нет, `/api/v1` и `/mcp` отдают `index.html` панели.

Клиент без Streamable HTTP: на ПК `tools/mcp/sa02m-mcp-stdio.py`.

```bash
export SA02M_API_URL=http://192.168.1.136:9999
export SA02M_API_TOKEN='sa02m_<id>.<secret>'
python3 tools/mcp/sa02m-mcp-stdio.py
```

Один объект JSON-RPC на строку stdin.

## Аутентификация

Заголовок на каждый запрос:

```
X-SA02M-Token: sa02m_<id>.<secret>
```

`<id>` — 8 шестнадцатеричных знаков. Секрет в панели больше не показывается:
потеряли — отзовите и выпустите новый. Токен не выпускает другие токены.
Выпуск и отзыв — только карточка (cookie-сессия оператора и CSRF).

Срок и scope задаются при выпуске. Просроченный и отозванный токен — `401`.

Лимит: 60 запросов в минуту на токен. Операции scope `admin` — ещё 10 в минуту.
Сверх лимита — `429`, тело `{"ok":false,"error":"rate_limited"}`.

## Два входа

Имя операции в реестре — `домен.глагол` (`system.info`, `rules.run`).

| | REST | MCP |
|---|---|---|
| Куда | `POST /api/v1/домен/глагол` | `POST /mcp`, метод `tools/call` |
| Имя | слэш вместо точки | `sa02m_` + подчёркивания вместо точек (`sa02m_system_info`) |
| Аргументы | JSON-тело | `params.arguments` |
| Чтение | ещё и `GET` с query string | тот же `tools/call` |

`GET` на мутацию — `405`. Служебные поля тела (`confirm`, `dry_run`, `wait`)
в CGI и в store не уходят.

Пример чтения аргументом:

```bash
curl -sS -H "X-SA02M-Token: $TOK" \
  'http://192.168.1.136:9999/api/v1/rules/get?id=night'
```

Тот же вызов POST:

```bash
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"id":"night"}' \
  http://192.168.1.136:9999/api/v1/rules/get
```

MCP:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"sa02m_rules_get","arguments":{"id":"night"}}}
```

Инструмент вне scope токена в `tools/list` отсутствует. `tools/call` по нему —
ошибка JSON-RPC, побочный эффект не выполняется.

Ресурсы MCP: `sa02m://guide`, `sa02m://openapi`, `sa02m://mqtt/topics`,
`sa02m://rules/scenarios`, `sa02m://user/tree`.

Промпты: `onboarding` (прочитать плату и предложить план, ничего не меняя),
`diagnose` (почему устройство не отвечает — по живому кэшу MQTT и журналу моста).

## Scope

Каждый следующий включает предыдущий: `read` ⊂ `control` ⊂ `config` ⊂ `admin`.

| Scope | Что можно | Чего нельзя |
|---|---|---|
| `read` | статус, сеть, MQTT-кэш, сценарии, журналы, файлы в `/opt/sa02m-user`, порты прошивальщика | писать что-либо |
| `control` | `mqtt.publish`, `hw.set`, `rules.run`, `rules.off` | менять конфиг и файлы |
| `config` | сеть, MQTT, шлюз, службы, сценарии, Node-RED, MPLC, код пользователя, `user.pip` | shell, обновление, reboot |
| `admin` | `shell.exec` от `www-data`, файлы вне песочницы, обновление, ядро, reboot, бэкап, сброс, прошивка | root, пока нет флажка |

`admin` при выпуске выключен. Он не даёт root.

Root — отдельный флажок `root_capable`. Карточка спрашивает пароль root.
Неверный пароль — токен не создаётся (`root_auth`). С флажком доступны
`shell.exec` с `"mode":"root"` и `files.read` / `files.write` вне
`/opt/sa02m-user`. Не ставьте флажок, если агенту хватает `www-data`.

## Мутации

На любой мутации сначала `"dry_run": true`: демон вернёт план и хелпер не
вызовет.

Необратимое требует `"confirm"`, равный **имени операции**:

`reboot`, `kernel.set`, `kernel.refresh`, `web_update.apply`,
`web_update.upload`, `factory_reset`, `flasher.flash`, `web_creds.set`.

Без него — `409` `confirm_required`, команда не уходит. `factory_reset` после
`confirm` сам подставляет фразу CGI.

```bash
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"confirm":"reboot"}' \
  http://192.168.1.136:9999/api/v1/system/reboot
```

## Долгие операции

`shell.exec`, прошивка, обновление, ядро, перезагрузка, factory reset,
деплой MPLC, `user.pip`, установка узла Node-RED, `backup.download`,
`mqtt.scan` по REST сразу отвечают:

```json
{"ok": true, "job_id": "0123456789abcdef"}
```

Результат:

```bash
curl -sS -H "X-SA02M-Token: $TOK" \
  http://192.168.1.136:9999/api/v1/jobs/0123456789abcdef
```

`job.status` — `running`, `done` или `error`. `job.result` — тот же объект,
что вернул бы прямой вызов. Чужой `job_id` — `404`.

`"wait": true` в теле — ответ синхронно, без задания. nginx держит соединение
до 3600 с. Через MCP долгий инструмент по умолчанию отвечает синхронно:
опросчика заданий у MCP-клиента нет.

События одного задания: `GET /api/v1/jobs/<id>/events` (`text/event-stream`).
Первое событие — текущее состояние. Общая лента: `GET /api/v1/events`
(задания, прогоны сценариев, смена кэша MQTT). Нужен scope `read`.

В памяти не больше 200 завершённых заданий.

## Типовые вызовы

Переменная `$TOK` — секрет целиком, `sa02m_<id>.<secret>`. Хост замените на IP
платы. Порт панели — `9999`.

Состояние платы (scope `read`):

```bash
curl -sS -H "X-SA02M-Token: $TOK" \
  'http://192.168.1.136:9999/api/v1/system/status?part=sys'
```

`part` — `[A-Za-z0-9_-]{1,32}` (`sys`, `net`, … — те же части, что у `status.cgi`).

Живые устройства MQTT и журнал моста:

```bash
curl -sS -H "X-SA02M-Token: $TOK" \
  http://192.168.1.136:9999/api/v1/mqtt/devices/live
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"unit":"sa02m-modbus-mqtt","lines":40}' \
  http://192.168.1.136:9999/api/v1/logs/journal
```

`unit` — из allow-list демона (`sa02m-modbus-mqtt`, `sa02m-rules`, `nginx`,
`mosquitto`, `sa02m-flasher`, `mplc4`, …) или `sa02m-user@<имя>`. `lines` — 1…200.

Запись выхода MQTT (scope `control`). Топики — Wiren Board,
`/devices/<id>/controls/<name>`; публикация идёт в `…/on` без retain:

```bash
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"device":"SA-02m","control":"beeper","value":"1"}' \
  http://192.168.1.136:9999/api/v1/mqtt/publish
```

Выход самой платы (`hw.set`): `channel` — `do`, `beeper`, `alarm_led` или
`usb_power`; `value` — целое.

Сценарий (сначала `read`, запись — `config`):

```bash
curl -sS -H "X-SA02M-Token: $TOK" \
  http://192.168.1.136:9999/api/v1/rules/list
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"id":"night"}' \
  http://192.168.1.136:9999/api/v1/rules/run
```

Новая строка — `rules.upsert`, тело `{"scenarios":[ …одна строка сценария… ]}`
или одна строка полями верхнего уровня. Замена всего файла — `rules.replace`
с `{"scenarios":[…]}`. Удаление — `rules.delete` с `{"id":"…"}`. Библиотека
JS — `rules.library` с `{"library":"…"}` (до 16 КиБ). Погасить выходы прогона —
`rules.off` с `{"id":"…"}`.

Скан шины (scope `config`, долгая операция). `port` обязателен:

```bash
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"port":"/dev/ttyS1","baudrate":9600,"wait":true}' \
  http://192.168.1.136:9999/api/v1/mqtt/scan
```

Свой код — только под `/opt/sa02m-user`. `..` и симлинк отклоняются до записи.
Файл не больше 256 КиБ.

Каталог создайте до файла: родитель `start.sh` уже должен существовать.

```bash
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"path":"demo"}' \
  http://192.168.1.136:9999/api/v1/user/files/mkdir
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"path":"demo/start.sh","text":"#!/bin/sh\nexec sleep infinity\n"}' \
  http://192.168.1.136:9999/api/v1/user/files/write
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"name":"demo"}' \
  http://192.168.1.136:9999/api/v1/user/units/install
```

`name` — `^[a-z0-9][a-z0-9-]{0,30}$`. Точка входа — `start.sh` в этом каталоге.
`install` отдаёт каталог группе `sa02m-user` и ставит `+x` на `start.sh`.
Дальше `user.units.start` / `stop` / `remove`. Пакет в venv
`/opt/sa02m-user/.venv` — `user.pip` с `{"package":"six"}` (имя
`[A-Za-z0-9_.-]{1,64}`); без сети это ошибка, не зависание. Юнит программы:
`User=sa02m-user`, `MemoryMax=64M`, `CPUQuota=30%`.

Бэкап (scope `admin`) — задание; в результате gzip в base64:

```bash
curl -sS -H "X-SA02M-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"wait":true}' \
  http://192.168.1.136:9999/api/v1/backup/download
```

Поля результата: `content_type`, `filename`, `size`, `body_b64`.

Обновление с файлового пакета — `web_update.apply` с `confirm` и
`confirm_version` вида `1.0.7.1`. Без `confirm_version` — GitHub-OTA.
Оба варианта требуют `"confirm":"web_update.apply"`.

## Ошибки

| HTTP | `error` | Что сделать |
|---|---|---|
| 400 | `bad_request` | проверить обязательные поля и формат (`port`, `id`, `channel`) |
| 400 | `io_error` | путь — каталог, нет прав или нет места; `reason` называет тип |
| 401 | `unauthorized` | нет заголовка, секрет битый или токен отозван |
| 403 | `forbidden` | поднять scope или не звать root без флажка (`need` в теле) |
| 404 | `not_found` | нет такой операции или чужой `job_id` |
| 405 | — | GET на мутацию; повторить POST |
| 409 | `confirm_required` | добавить `confirm` с именем операции |
| 429 | `rate_limited` | подождать минуту |
| 502 | — | служба выключена или сокет ещё не поднялся (после enable — несколько секунд) |

Тело ошибки: `{"ok":false,"error":"…"}`. Успех: `{"ok":true,…}`.

## Чего агент не делает

- Не выпускает токены и не читает чужие секреты.
- Не пишет вне `/opt/sa02m-user`, пока нет `admin` и (для root-путей) флажка.
- Не вызывает необратимое без явной просьбы оператора и без `confirm`.
- Не обходит отказ scope и не просит root «на всякий случай».
- Не считает `dry_run` выполненной командой.

Аудит каждого вызова — `/var/log/sa02m-agent-api/audit.jsonl` (хеш аргументов,
не тело). Секреты токенов на диске не хранятся, только хеши в
`/etc/sa02m-agent-api/tokens.json`.
