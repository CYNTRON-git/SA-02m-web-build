# Контракт: API для внешних ИИ-агентов (SA-02m Agent API + MCP)

Дом этого контракта — этот файл. Модель угроз — актор A9 и две строки §4 в
`docs/threat-model.md`. Руководство оператора — `docs/AI_AGENT_INTEGRATION.md`.
Короткая карточка для агента, которого привели на плату — `www/network_config/llms.txt`
и MCP-ресурс `sa02m://guide`.

Служба ставится установщиком и **выключена**, пока оператор не включит её на
карточке «API для ИИ-агентов». Пока юнит выключен, `/api/v1` и `/mcp` не
отвечают: сокета нет.

## Кто слушает

Демон `sa02m-agent-api` (`opt/sa02m-agent-api`, Python 3.12, только stdlib) слушает
`unix:/run/sa02m-agent-api/api.sock` от пользователя `www-data`. nginx на `:9999`
проксирует без `auth_request` (аутентификацию делает демон):

| Путь | Что |
|---|---|
| `GET /health` | без токена: `{"ok":true}` |
| `GET /api/v1/openapi.json` | OpenAPI 3.1, нужен токен |
| `/api/v1/<домен>/<глагол>` | одна операция реестра, POST JSON (чтение ещё и GET; аргументы — query string, `?id=…`). GET на мутацию — 405 |
| `GET /api/v1/events` | SSE: задания, прогоны сценариев, изменения кэша MQTT |
| `GET /api/v1/jobs/<id>` | состояние и результат задания: `{"ok":true,"job":{"id","status":"running|done|error","result"}}` |
| `GET /api/v1/jobs/<id>/events` | SSE одного задания; первое событие — текущее состояние |
| `POST /mcp` | MCP Streamable HTTP, тело JSON-RPC |
| `GET /llms.txt` | статический файл панели |

`location /api/v1/` и `location = /mcp` живут в site-файле nginx. GitHub-OTA
рендерит шаблон и кладёт его в `sites-available` (`docs/deployment.md`): порт и
корень берутся из уже стоящего файла. Раннер на плате собирает манифест сам,
поэтому site-файл приезжает следующим обновлением после раннера, который это
умеет. Пока маршрута нет, `/api/v1` и `/mcp` отдают `index.html`, и карточка
пишет `nginx_routed: false`.

`ProtectSystem=strict` у этого юнита **нет**. Дочерний `sudo` наследует пространство
имён службы; со `strict` запись хелперов в `/etc` не проходит. `NoNewPrivileges=no`
— иначе нет sudo. Это названо в модели угроз, а не спрятано.

## Аутентификация

Заголовок `X-SA02M-Token: sa02m_<id>.<secret>`.

- `<id>` — 8 шестнадцатеричных знаков, `<secret>` — 32..64.
- Облачный прокси пропускает семейство `X-SA02M-*` (`docs/contracts/cloud-panel-proxy.md`).
  `Authorization` он может срезать, поэтому свой заголовок — основной.
- `Authorization: Bearer sa02m_...` принимается тем же разбором, если заголовка
  `X-SA02M-Token` нет.
- На диске (`/etc/sa02m-agent-api/tokens.json`) только sha256 всей строки токена,
  имя, scopes, срок, `created`, `last_used`, флаг `root_capable`. Секрета нет.
  Каталог `root:root` 0750, файл 0640 `root:www-data`: демон читает, не пишет.
  Пишет только root-хелпер.
- Срок: `days = 0` — без срока; иначе 1..3650 суток. Просроченный токен — 401.
- Частота: 60 запросов в минуту на токен (admin-операции — 10). Дальше 429.
- Журнал `/var/log/sa02m-agent-api/audit.jsonl`: id токена, операция, sha256
  аргументов (поля `password` / `secret` / `token` / `root_password` в дайджест
  не попадают как значения), `ok`. Сам секрет не пишется.

Выпуск и отзыв — **только живая сессия панели** (cookie + `X-SA02M-CSRF`).
Токен на `POST /api/v1/admin/tokens` получает 403 `token_cannot_mint` и демон
хелпер не вызывает. Карточка ходит в `cgi-bin/sa02m_agent_api.cgi`, а не в
демон: юнит по умолчанию выключен и сам себя не включит. Это отступление от
черновика «карточка без CGI»; контракт фиксирует CGI как плоскость управления.

Пользователь `www-data` не может подложить ссылку в каталог токенов. Отзыв
удаляет запись и файл `/etc/sa02m-agent-api/root-cap/<id>`.

## Scopes

Хранятся явным набором. Более высокий включает нижние: `admin` ⊃ `config` ⊃
`control` ⊃ `read`. Сервер дописывает нижние при выпуске, чтобы файл читался
однозначно.

| Scope | Смысл |
|---|---|
| `read` | чтение статуса, конфигов, журналов, сценариев, файлов в `/opt/sa02m-user` |
| `control` | запись выходов (`mqtt.publish`, `hw.set`), пуск и гашение сценария |
| `config` | сеть, MQTT, шлюз, службы, сценарии, Node-RED, MPLC, пользовательский код |
| `admin` | оболочка, файлы вне песочницы, обновление, ядро, перезагрузка, учётка |

Проверка scope — **до** побочного эффекта. Не хватает — 403 `forbidden` и поле
`need`. Гейт `agent-api-scope-matrix`.

`admin` по умолчанию при создании выключен. Он даёт оболочку от `www-data`
(`mode=web`), не root.

## Root

`root_capable` — отдельный флаг, не scope. Карточка просит пароль root; хелпер
`sa02m-agent-root-cap.sh` читает хеш root из `/etc/shadow` и сверяет пароль
через `crypt(3)` в `libcrypt` (`hmac.compare_digest`; yescrypt `$y$` принимается).
Не сошлось или пароль пуст — выход 77, cap-файл не создаётся, токен отзывается.
Нет `libcrypt` или shadow не читается — выход 69. Сошлось — файл
`/etc/sa02m-agent-api/root-cap/<id>` 0600 root.

`shell.exec` с `mode=root` и `files.read` / `files.write` вне `/opt/sa02m-user`
зовут `sa02m-agent-root-exec.sh`. Хелпер проверяет id и cap-файл и только потом
запускает скрипт. Путь скрипта — только `/tmp/sa02m-agent-cmd.XXXXXX`, не ссылка.

Остаток, нарочно: пока cap-файл есть, **любой** процесс `www-data` (в том числе
`cmd_exec.cgi`) может вызвать тот же хелпер. Демон не вынесен в другого
пользователя: иначе пришлось бы копировать все sudo-гранты панели. Не ставьте
флаг без нужды. Отзыв токена файл удаляет.

## Ошибки

Демон отвечает настоящими кодами (кроме CSRF, см. ниже).

| Код | `error` | Когда |
|---|---|---|
| 200 | — | `{"ok":true,...}` |
| 400 | `bad_request` | тело, тип, длина, забор пути |
| 401 | `unauthorized` | нет токена, битый, просрочен |
| 403 | `forbidden` | нет scope (`need`) или нет `root_capable` |
| 403 | `token_cannot_mint` | токен полез в выпуск токенов |
| 404 | `not_found` | нет такой операции |
| 409 | `confirm_required` | необратимое без `confirm` |
| 429 | `rate_limited` | лимит частоты |
| 503 | `busy` | заняты все слоты запросов |

CSRF. POST с **cookie-сессией** на `/api/v1/admin/*` без верного `X-SA02M-CSRF`
— HTTP 200 и тело `csrf_error_body` (как у flasher и devices-api: одна реакция
панели). Запрос с токеном cookie не несёт, CSRF не спрашивается.

Необратимые операции требуют в JSON `"confirm": "<имя операции>"` (ровно
`reboot`, `kernel.set`, `kernel.refresh`, `web_update.apply`, `web_update.upload`,
`factory_reset`, `flasher.flash`, `web_creds.set`). Иначе 409 и хелпер не
вызывается. `factory_reset` после этого сам подставляет фразу CGI
`SA02M-RESET`.

`"dry_run": true` на мутации возвращает план и не вызывает хелпер.

## Задания и SSE

Долгое (`shell.exec`, прошивка, обновление, ядро, перезагрузка, factory reset,
деплой MPLC, `user.pip`, установка узла Node-RED, `backup.download`, `mqtt.scan`)
отвечает сразу `{"ok":true,"job_id":"..."}`. Неверные аргументы (нет `port` у
`mqtt.scan`, пустая команда у `shell.exec`) отвечают сразу кодом ошибки, без
задания. Результат — `GET /api/v1/jobs/<id>`
(`job.result` — тот же объект, что вернул бы прямой вызов; при ошибке в нём есть
`status`), события — `GET /api/v1/jobs/<id>/events` (`text/event-stream`). Чужой
`job_id` — 404. `"wait": true` в теле — ответ синхронно, без задания (nginx держит
соединение до 3600 с). Через MCP долгий инструмент по умолчанию отвечает
синхронно: у MCP-клиента нет опросчика заданий. В памяти держится не больше 200
завершённых заданий.

`GET /api/v1/events` — общая лента: те же задания, смена `runs.json` сценариев,
смена кэша `/run/sa02m-modbus-mqtt/*.json` (файл старше 10 с не считается живым).
Нужен scope `read`. nginx: `proxy_buffering off`, `proxy_read_timeout 3600s`.

## Операции

Имя в таблице — имя в реестре. REST: `POST /api/v1/` + имя с точками, заменёнными
на `/`. Инструмент MCP: `sa02m_` + имя с точками, заменёнными на `_`.

Где есть CGI, демон вызывает его со служебной сессией (`web_session_create` в
`lib_web_auth.sh`, cookie и `X-SA02M-CSRF`). Контракт CGI не меняется.
`mqtt.publish` — та же грамматика, что `docs/contracts/mqtt-set-endpoint.md`.

| Операция | Scope | Куда |
|---|---|---|
| `system.status` | read | `status.cgi` (`part` — `[A-Za-z0-9_-]{1,32}`) |
| `system.info` | read | версия, вариант, адреса, юнит API |
| `services.list` | read | `services_ctrl.cgi` |
| `network.get` | read | `config.cgi` |
| `mqtt.config.get` | read | `mqtt_config.cgi` |
| `mqtt.devices.live` | read | кэш `/run/sa02m-modbus-mqtt` |
| `mqtt.topics.snapshot` | read | ключи того же кэша |
| `gateway.config.get` | read | `gateway_config.cgi` |
| `gateway.status` | read | `gateway_status.cgi` |
| `rules.list` `rules.get` `rules.runs` | read | `/etc/sa02m-rules/scenarios.json`, `runs.json` |
| `devices.history` | read | сокет `sa02m-devices-api`, `GET /api/devices/history` |
| `logs.journal` | read | `sa02m-agent-journal.sh` (allow-list юнитов) |
| `logs.install` | read | `log.cgi` |
| `user.files.list` `user.files.read` | read | только под `/opt/sa02m-user` |
| `user.units.status` `user.units.logs` | read | `sa02m-user-unit.sh`, журнал |
| `nodered.flows.get` | read | `127.0.0.1:1880` |
| `mplc.project.info` | read | есть ли каталог проекта |
| `flasher.ports` `flasher.firmware` `flasher.jobs` | read | сокет flasher (`/ports`, `/firmware`, `/jobs`) |
| `mqtt.publish` | control | `mqtt_set.cgi` |
| `hw.set` | control | `hw_set.cgi` |
| `rules.run` `rules.off` | control | store `{"run_now":true,"id"}`; гашение — `mosquitto_pub` на локальный 1883 |
| `network.apply` | config | `apply.cgi` (те же поля формы) |
| `mqtt.config.set` `mqtt.scan` `mqtt.tcp_probe` | config | соответствующие CGI; `mqtt.scan` требует `port` (`/dev/…`), `baudrate`/`max_addr` по желанию |
| `gateway.config.set` `gateway.ctrl` | config | CGI шлюза; `gateway.ctrl` — `action` ∈ start/stop/restart/reload |
| `services.start` `stop` `install` `uninstall` | config | `services_ctrl.cgi` (id из его allow-list, не этот юнит) |
| `rules.upsert` `replace` `delete` `library` | config | `sa02m-rules-store-apply.sh` → `sa02m_rules.store.apply_command`. Тело store: `upsert` — `{"upsert":[rows]}` (вход: `scenarios[]` или одна строка), `replace` — `{"replace":true,"scenarios":[…]}`, `delete` — `{"delete":true,"id"}`, `library` — `{"library":"<js>"}`. Ответ store возвращается как есть |
| `nodered.flows.deploy` `nodered.nodes.install` | config | admin API `:1880` |
| `mplc.project.deploy` | config | `mplc_project_deploy.cgi` |
| `user.files.write` `delete` `mkdir` | config | забор realpath, без симлинков, файл ≤ 256 КиБ |
| `user.units.install` `start` `stop` `remove` | config | шаблон `sa02m-user@.service` |
| `user.pip` | config | только venv `/opt/sa02m-user/.venv`; имя пакета `[A-Za-z0-9_.-]{1,64}`; нет сети — ошибка, не зависание |
| `shell.exec` | admin | `mode=web` — bash от `www-data`, cwd `/opt/sa02m-user`, timeout ≤ 120 с, без sudo. `mode=root` — только `root_capable` |
| `files.read` `files.write` | admin | вне песочницы, через root-exec, обычный файл, не ссылка, ≤ 256 КиБ |
| `web_update.check` | admin | `web_update_check.cgi` |
| `web_update.apply` `web_update.upload` | admin | CGI, нужен `confirm`; `apply` с `confirm_version` — файловый пакет, без него — GitHub-OTA |
| `kernel.set` `kernel.refresh` | admin | `kernel_ctrl.cgi`, нужен `confirm` |
| `cpu.profile` | admin | `cpu_profile.cgi` |
| `system.reboot` `system.restart_services` | admin | CGI, `reboot` с `confirm` |
| `web_creds.set` | admin | `web_creds.cgi`, нужен `confirm` |
| `backup.download` | admin | `web_backup.cgi` (поток gzip): задание; результат `{"ok","content_type","filename","size","body_b64"}` |
| `factory_reset` | admin | `web_factory_reset.cgi`, нужен `confirm` |
| `flasher.flash` `flasher.scan` | admin | сокет flasher; `flash` с `confirm` |

Служба `sa02m-agent-api` в allow-list `sa02m-web-service-ctl.sh` **не** входит.
Её включают карточка и `sa02m-agent-api-ctl.sh`.

## MCP

`POST /mcp`, `Content-Type: application/json`, один объект JSON-RPC 2.0.
Уведомление без `id` — HTTP 202 и пустое тело.

Методы: `initialize`, `notifications/initialized`, `ping`, `tools/list`,
`tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`.

`initialize` отвечает `protocolVersion` `2025-03-26`, `serverInfo.name`
`sa02m-agent-api`. Инструмент, которого scope токена не покрывает, в
`tools/list` отсутствует; `tools/call` по нему — ошибка JSON-RPC, хелпер не
вызывается.

Ресурсы: `sa02m://guide`, `sa02m://openapi`, `sa02m://mqtt/topics`,
`sa02m://rules/scenarios`, `sa02m://user/tree`.

Промпты: `onboarding` (изучи плату и предложи план), `diagnose` (почему
устройство не в сети).

Клиент без Streamable HTTP на ПК: `tools/mcp/sa02m-mcp-stdio.py` (stdio → HTTP,
переменные `SA02M_API_URL` и `SA02M_API_TOKEN`).

## Пользовательский код

Пользователь `sa02m-user`, каталог `/opt/sa02m-user` `2775 sa02m-user:www-data`.
`www-data` в группе `sa02m-user`, чтобы отдать группу новым файлам (`0660`).
Обратное (сажать `sa02m-user` в группу `www-data`) не делается: эта группа
читает каталог сессий.

Компонент пути `..`, абсолютный путь и любой симлинк отклоняются до записи.
Шаблон `sa02m-user@.service`: `User=sa02m-user`, `ProtectSystem=strict`,
`ReadWritePaths=/opt/sa02m-user/%i`, `MemoryMax=64M`, `CPUQuota=30%`,
`NoNewPrivileges=yes`, `ExecStart=/opt/sa02m-user/%i/start.sh`. Имя юнита —
`^[a-z0-9][a-z0-9-]{0,30}$`. `user.units.install` (root-хелпер) отдаёт каталог
программы группе `sa02m-user` и ставит `+x` на `start.sh` — демон пишет файлы
`0660` от `www-data`, сам он выполнить их не сделает.

Плата, обновлявшаяся только по сети, модуль `scripts/13-agent-api.sh` не
проходила: пользователя `sa02m-user`, каталоги и seed `tokens.json` создаёт
`sa02m-agent-api-ctl.sh enable` (идемпотентно, из того же
`etc/tmpfiles.d/sa02m-agent-api.conf`). Карточка сообщает, если в site-файле
nginx нет маршрута `/api/v1` (`nginx_routed:false` в ответе CGI).

Служебная сессия демона (`web_session_create agent-api`) живёт только в его
памяти: при каждом новом минте демон удаляет из `/run/sa02m-web-sessions`
файлы прежних сессий пользователя `agent-api` (и их `.csrf`); сессии панели не
трогаются.

## Node-RED

Прокси ходит на `127.0.0.1:1880`. Если лежит `/etc/sa02m-agent-api/nodered.env`
и это обычный файл не для всех на запись, демон берёт оттуда basic auth.
Сам порт 1880 в LAN по-прежнему без `adminAuth`, если оператор его не включил:
прокси это не закрывает и не открывает шире. Включать `adminAuth` — отдельное
решение, этот контракт его не меняет.

## Бэкап и клон

В веб-бэкап входит `/etc/sa02m-agent-api/tokens.json` (хеши, не секреты).
Cap-файлы не входят. Восстановление принимает тот же путь
(`etc/sa02m-restore-backup.sh`). Factory reset и `wipe_agent_api_identity()` на
площадках снятия образа стирают токены, cap-файлы и wants-ссылку юнита.
Ссылку на каталоге cap не разыменовывают.

## Облако

Туннель пускает порт 9999, относительные пути панели доходят. Токен в заголовке
`X-SA02M-*` тоже. TLS обрывается в облаке: облако видит токен, как сейчас видит
пароль. Срок и scope уменьшают ущерб, но не прячут токен от облака.
