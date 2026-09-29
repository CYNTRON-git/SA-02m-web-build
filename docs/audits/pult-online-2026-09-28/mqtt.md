# Pult.online server — аудит MQTT / Modbus-карта / удалённый доступ (для SA-02m)

Дата: 2026-09-28. Режим: только чтение. Бинарники Pult НЕ запускались; строки извлечены
python-скриптом (исходный `_strings/pult_*.txt` в study kit оказался пустым — 0 байт,
пересобран в `scratchpad/mystrings/<svc>.txt`). Секреты (ключи, пароли, токены) в этом
файле не приводятся — только факт их наличия.

Обозначения источников: `M:` = зеркало `…/Пульт Сервер/pult_server/`,
`SCH:` = `M:services/server/schema.json` (`types.prototype_drv_*`), `STR:<svc>` =
строки бинарника, `LIVE` = живая проверка на 192.168.1.136.

---

## 0. Живые проверки (192.168.1.136)

### MQTT :1883 (map_mqtt, встроенный брокер)

Клиент paho 2.1.0, clean session, без will, без публикаций. Скрипты:
`scratchpad/mqtt_sub*.py`, логи — в выводе запусков, захваты `mqtt_capture*.json`.

| Проверка | Результат |
|---|---|
| CONNECT без логина/пароля, client_id пустой (с ПК 192.168.1.x) | **CONNACK 0 (принято)** |
| CONNECT без логина, client_id `sa02m-audit-ro` | CONNACK 0 |
| SUBSCRIBE `#`, `$SYS/#`, `+`, `P1_N1/var_hr_1` (отдельными пакетами и одним) | **SUBACK не пришёл ни разу** (paho log: SUBSCRIBE отправлен, ответа нет) |
| PINGREQ (keepalive 5 с) | PINGRESP приходит — брокер жив |
| Сообщения за 20 с + 12 с + 20 с | **0** (ни обычных, ни `$SYS`) |

Вывод: брокер принимает анонимные соединения из LAN (как и обещает
`mqtt_allowed_ip=127.0.0.1,192.168.*.*,100.64.*.*,172.16.*.*,10.*.*.*`, `mqtt_readonly=0`,
логин/пароль закомментированы — `M:services/map_mqtt/config.cfg`), но на этой плате
не опубликовано ни одной переменной: в зеркале нет `data/vars/map_mqtt.json` (есть только
`core/drv_gateway/drv_mbrtu/drv_virt/server.json`), т.е. ни у одной переменной не включён
`extra_service_local_map_mqtt__enabled`. Отсутствие SUBACK — нарушение MQTT 3.1.1
(§3.8.4: сервер ОБЯЗАН ответить SUBACK); строгие клиенты (ждущие mid) повиснут.
Неизвестно, отвечает ли он SUBACK, когда топик существует — проверить нечем, не
публикуя, а публиковать запрещено.

### Modbus TCP :5002 (map_mbtcp)

Сырой сокет, только FC01–04 (`scratchpad/mb_read.py`). Соединение с ПК **принято**
(в `modbus_allowed_ip=127.0.0.1,192.168` — строковый префикс, не CIDR).
Все запросы (unit 0/1/2/255; FC03 адр. 0/1/100/1000; FC04/FC01/FC02 адр. 0) →
**исключение 06 «Slave Device Busy»**. Карта пуста (нет `data/vars/map_mbtcp.json`), а на
незамапленный адрес Pult отвечает 06 вместо канонического 02 «Illegal Data Address» —
SCADA будет бесконечно ретраить вместо «нет такого регистра».

---

## 1. Как это устроено у Pult

### 1.1 Общая архитектура

Микросервисы (статически слинкованные ARM-бинарники, OpenSSL внутри), каждый — свой
unix-сокет + JSON-RPC 2.0 (`{"jsonrpc":"2.0",…}`). `core` держит значения всех переменных,
`drv_*` (южные драйверы) их производят, `map_*` (северные «карты») их отдают наружу,
`server` — веб-UI/API. Проект «собирается» (`builder`) в `data/vars/<svc>.json` на каждый
сервис; каждый файл несёт `signature` (sha256 сборки), таблицу `keys/rows` (колонночный
формат), и на обмене сервис сверяет signature — «update, signature mismatch» → отказ
(`STR:map_mqtt`, `STR:drv_gateway`). Переменная живёт в core с кортежем
`name / time(ms) / value(double) / error(code) / payload(opaque)`.

### 1.2 map_mqtt — встроенный брокер, публикующий переменные

Конфиг: `M:services/map_mqtt/config.cfg`, форма UI `config.shema`.

- Порт 1883, опц. TLS (`mqtt_ssl_enabled`, `ssl_protocols=TLSv1.2,TLSv1.3`, только
  ECDHE/DHE-AEAD шифры — это у них сделано хорошо), `mqtt_allowed_ip` (glob-маски),
  опц. единый `mqtt_username/mqtt_password` (по умолчанию выключены), глобальный флаг
  `mqtt_readonly`.
- Публикация — **opt-in на переменную** (свойства в каждом `prototype_drv_*`, `SCH:`):
  - `extra_service_local_map_mqtt__enabled` (0/1);
  - `service_local_map_mqtt__topic`, default `%node_prefix%/%var_local_name%`,
    подстановки `%node_gateway%` (MAC шлюза), `%node_prefix%`, `%var_global_name%`,
    `%var_local_name%` + любые параметры узла (`%floor%/%room%/…`);
  - `service_local_map_mqtt__fields`, default `name,time,value,error,payload,gateway,path,params`
    — payload = **JSON-объект**: `name` (глоб. имя), `time` (ms epoch), `value` (number),
    `error` (код, если есть), `payload` (объект), `gateway` (MAC hex), `path` (путь узла),
    `params` (параметры узла). Поля можно урезать.
  - `service_local_map_mqtt__readonly` — запрет записи из MQTT на переменную.
- Запись: клиент публикует **в тот же топик**; брокер разбирает число и вызывает
  `set var: #%u = %f, payload: %uB` (`STR:map_mqtt`) → core → драйвер. Отдельного
  command-топика (`/on`, `/set`) нет.
- Retain: настройки retain у карты нет; в подписке к core есть флаги `no_initial`,
  `as_index` — *выведено*: новый подписчик получает текущее значение от брокера по
  внутреннему кэшу, а не через retained-флаг. На живой плате не подтверждено
  (переменных нет).
- Логи брокера: «auth failed, code %d», «%s <- %s | QoS=%u, Retain=%u» — QoS/retain
  входящих принимаются и логируются.

### 1.3 drv_mqtt — MQTT-клиент (южный драйвер)

`SCH:prototype_drv_mqtt`, `M:services/drv_mqtt/config.cfg`, `STR:drv_mqtt`.

- Параметры узла (= одно соединение с брокером): `drv_node_host` (def localhost),
  `drv_node_port` (1883/8883), `drv_node_ssl`, `drv_node_checkcert` (**def 0**),
  `drv_node_timeout` (10000 мс), `drv_node_username/password`, `drv_node_qos` (def 1),
  `drv_node_retain` (def 0); CA — `ssl_root_file=../../ssl/root.pem`. Client-id — `%s-%s`.
- Параметры переменной: `drv_topic`; `extra_format` ∈ `num | json | text | regx`;
  - `num` + `drv_num` — `dec` или HEX_{BE,LE}_{INT,UINT}{8,16,32,64}/FLOAT/DOUBLE (сырой
    hex-payload);
  - `json` + `drv_json` — путь `a/b/102/c`, с `*` для неизвестного индекса
    (`sensors/*/params/temperature`) в паре с условием `drv_match`
    (`sensors/*/params/id:102`, несколько условий построчно);
  - `text` — шаблон `param=pressure;value=%value%`; `regx` — regex с группой;
  - `drv_match` — фильтр сообщений в общем топике (wildcard для text, regex, json-условия);
  - `drv_list` — таблица подстановки `stop:0 / start:1 / auto:2` (обратимая на запись);
  - `drv_write` — шаблон тела записи (`{"sensor":123,"setpoint":%value%}`), пишется в
    **тот же** `drv_topic`; без шаблона — число с `drv_dcmsep`;
  - `drv_wrrnd` — округление при записи; `drv_timeout` — если сообщений нет N мс,
    переменная получает ошибку **4122 DRV_MQTT_ERROR_PUBLISHER_OFFLINE**.
- Общие для всех драйверов свойства переменной: `coef/offset`, `scl_hi/lo`, `raw_hi/lo`,
  `round`, `threshold` (зона нечувствительности), `threstime` (минимальный интервал
  изменения), `min/max` (только для записи оператором), `reset_interval/value`,
  групповые связи `group_vars/inv/ignore/block/sync/timeout/tolerance/trigger`,
  `trend`, `buffer`, `readonly`, `tags` (ACL по тегам), `sim_random*` (симуляция).
- Коды ошибок: 4120 BROKER_DISCONNECTED, 4121 PUB, 4122 PUBLISHER_OFFLINE
  (`M:services/drv_mqtt/errors.json`) — числовой каталог + двуязычные описания.

### 1.4 map_mbtcp — Modbus TCP slave поверх переменных

`M:services/map_mbtcp/config.*`, `SCH:` (`service_local_map_mbtcp__*`,
`service_remote_map_mbtcp__*`), `STR:map_mbtcp`.

- Порт(ы) списком (`modbus_port=502,503,504` — разные порты = разные карты),
  `modbus_allowed_ip` (строковые префиксы; `*` = все).
- На переменную: `enabled`, `port` (def 5002), `slave` (def 1), `reg_type` ∈ CL/DI/HR/IR,
  `reg_addr` + `reg_off` (физ. адрес = addr + off), `format` ∈ INT16, UINT16,
  FLOAT_{ABCD,CDAB,BADC,DCBA}, (U)INT32_{ABCD,CDAB,BADC,DCBA}, BIT_0…BIT_15; в бинарнике
  ещё `reg_mlt` (множитель).
- Две карты: **local** (на сервере) и **remote** (на шлюзе — `service_remote_*`);
  одна переменная может быть выставлена в обе.
- Запись в HR/CL → `set var` (как у MQTT). Аутентификации нет (протокол).
- LIVE: незамапленный адрес → исключение 06.

### 1.5 Удалённый доступ и облако

| Сервис | Что делает | Факты |
|---|---|---|
| `hostclient` | Обратный **HTTP-прокси**: плата держит исходящее TLS-соединение к `pult.host:443`, пользователь открывает поддомен `<client_domain>.pult.host`, трафик идёт на `target_host=localhost:80` | per-device `client_domain` + `client_key` + `client_secret` (заполнены в зеркале), `server_checkcert=1`, `ssl_root_file`; логи «USER %u --> TARGET» (`M:services/hostclient/config.cfg`, `STR:hostclient`) |
| `tunnelclient` | Универсальный TCP-туннель к `tunnel.pult.online:443` для «консоли» (десктоп-клиент/конфигуратор): `register_client` → `client_registered`, `client_console_online/offline`, ping/pong | идентификатор = **MAC** (`validation=mac`) + пароль; `ssl_protocols=SSLv2,SSLv3,TLSv1.2,TLSv1.3`, шифры с `RSA+3DES`; при отсутствии сертификата делает self-signed (`STR:tunnelclient`) |
| `drv_gateway` | **Агрегация шлюзов**: Pult-сервер принимает входящие соединения своих полевых Pult-шлюзов на `:11211` (`gateway_tcp_port`), сервер же ходит к ним через `gateway_tunnel=ws://localhost:11211` (`server/config.cfg`) | шлюз = узел типа `gateway` с полем `mac`; рукопожатие `hello{mac,version_number}` + секрет (ошибки 4243 NOT_FOUND, 4244 WRONG_SECRET, 4245 INVALID_MAC, 4246 DATA_VERSION); сервер пушит собранный проект `data_<sig>.tar.gz` и расписания `schd_*` (`gateways_sync_interval=60000`, `gateway_check_schedules_interval=5000`); удалённые команды `gateway_exec`, `gateway_reboot`, `gateway_api`, `gateway_log`, `gateway_reset_secret`; метрики шлюза как переменные (`online/offline/ping`, `extra_data=metric`) |
| core buffer | Кольцевой буфер изменений на **шлюзе** (`buffer_size=100000` записей, `buffer_all=0`, флаг `buffer` на переменной); сервер после восстановления связи переподписывается «from buffer_id %ju of signature %s» и дочитывает пропущенное (`chunk_size=10000`) | `M:services/core/config.shema` (help `buffer_size`), `core/buffer.json` (`{"signature","buffer_id","time","changes":[]}`) |
| core values | Последние значения сохраняются на диск (`save_values=1`, `values.kvdb`, раз в 10 с, WAL) → переживают рестарт | `M:services/core/config.cfg` |
| `letsssl` | Обёртка над certbot: раз в 12 ч копирует `/etc/letsencrypt/live/<domain>` в `ssl/` | `enabled=0` в зеркале; нужен публичный домен и входящий :80 |

**Топология.** Один и тот же дистрибутив работает и как сервер, и как шлюз. Полевые
шлюзы (Pult на контроллере у объекта) опрашивают железо своими `drv_*`, буферизуют
изменения, и сами дозваниваются до центрального Pult-сервера (drv_gateway :11211);
переменная прототипа с `gateway=%gateway%` («Авто») разворачивается сборщиком в пару:
драйверная переменная **на шлюзе** + зеркальная переменная `drv_gateway` **на сервере**
(`drv_varid` = индекс в пространстве шлюза). Сервер — единая точка UI/трендов/тревог/
уведомлений для многих шлюзов; конфигурация стекает сервер → шлюз архивом с подписью.
Доступ человека снаружи — отдельно: `hostclient` (веб по поддомену) или `tunnelclient`.

### 1.6 chnl_telegram (только как канал доставки)

`M:services/chnl_telegram/config.cfg`, `STR:chnl_telegram`,
`M:data/projects/Проект1.project/notifications/*.ntf/@content.txt`.

- До 5 ботов, long-poll `getUpdates` раз в 5 с, отправка раз в 1 с,
  `telegram_queue_limit=1000`, `telegram_attempts=3`, **`telegram_actuality=3600`** (старше
  часа — не слать).
- Привязка получателя: бот просит «поделиться номером» (`request_contact` клавиатура),
  хранит `phone ↔ chat_id` в `db.kvdb`; адресация уведомлений — по телефону пользователя
  Pult. (Проверяет ли он, что присланный контакт — свой, `contact.user_id == from.id`, —
  из строк не видно.)
- Шаблоны уведомлений: один файл на тип, блоки `#if <выражение>` по каналу/событию
  (`%channel_telegram%`, `%channel_interface%`, `!%var__alarm_repeated%`,
  `%var__alarm_action_act%`), `#telegram_parse_mode MarkdownV2`, подстановки
  `%var__alarm_md_link%`, `%var__node_md_link%`, `%var__time_str%`, `%debug%` (дамп всех
  подстановок), `#comment`, `#if 0` для отключённых блоков.

---

## 2. Сравнение с SA-02m

| Тема | Pult | SA-02m (файл) |
|---|---|---|
| Северный MQTT | встроенный брокер, opt-in на переменную, JSON {name,time,value,error,…}, запись в тот же топик | mosquitto: 1883 loopback-anon, 1884 наружу с паролем+ACL (`etc/mosquitto/10listeners.conf`, `acl_default.conf`); конвенция WB `/devices/<id>/controls/<n>` + `/on` для записи, retained, `meta/error`, LWT (`docs/MQTT_TOPICS.md`) — **у нас лучше** (отдельный command-топик, retained, LWT, auth) |
| Время/ошибка значения | в каждом сообщении (`time`, `error`) | значение — голое число; ошибка — отдельный `meta/error` (`r`/`w`), времени нет |
| TLS на MQTT | опция, TLS1.2/1.3 AEAD | на 1884 TLS нет |
| Роль «только чтение» | глобальный `mqtt_readonly` + на переменную | ACL: единственный `mqttuser` с `readwrite /devices/#` |
| MQTT-клиент к чужим брокерам | drv_mqtt (JSON-path, regex, hex, match, подстановки, publisher-offline) | нет: мост только Modbus→MQTT (`opt/sa02m-modbus-mqtt/bridge_mqtt.py`) |
| Modbus TCP slave-карта | map_mbtcp (переменная → регистр, форматы, порты) | нет; есть только прозрачный шлюз к RS-485 (`docs/contracts/gateway-network-access.md`) и Modbus TCP как клиент (`docs/contracts/bridge-modbus-tcp.md`), OPC UA northbound |
| Удалённый веб | hostclient per-device key/secret | frpc к `cloud.cyntron.ru`, общий по флоту `FRP_TOKEN` (O2), device-side allow-list портов {80,9999} (`docs/contracts/cloud-enrollment.md`) |
| Буфер при обрыве связи | кольцевой буфер 100k изменений на шлюзе, досылка по buffer_id | heartbeat send-only, без буфера; архив только локальный (SQLite `sa02m-devices-logger`) |
| Метрики связи как переменные | online/offline/ping шлюза | статус агента в файле `/run/sa02m-cloud-status.json` (`docs/contracts/cloud-agent-status.md`), в MQTT не публикуется |
| Уведомления | Telegram-канал на устройстве, шаблоны `#if` | уведомления сценариев уходят в облако (`sa02m-rules` Notify); своего Telegram на плате нет |

---

## 3. Идеи для SA-02m (по убыванию ценности)

1. **Буфер «сохранить и дослать» для облачного канала.** Pult: кольцевой буфер изменений
   на шлюзе + досылка с `buffer_id` и проверкой signature (`core/config.shema`,
   `STR:drv_gateway`). У нас: heartbeat только send-only
   (`docs/contracts/cloud-enrollment.md` §1), при обрыве frpc/облака история для облака
   теряется. Зачем: тренды/тревоги в облаке без дыр при коротких обрывах 4G/Ethernet.
   Реализация: ограниченная очередь в SQLite (уже есть logger) + монотонный seq +
   `ack_seq` от облака; помечать буферизуемые контролы. Требует облачной стороны.
   **Усилие: L.**
2. **Modbus TCP slave-карта «MQTT → регистры» (read-only по умолчанию).** Pult: map_mbtcp
   (`SCH: service_local_map_mbtcp__*`). У нас: SCADA/ПЛК, умеющие только Modbus TCP,
   не видят МР-02м/ДТВ/СЭ через СА-02м иначе как прозрачным шлюзом, который отнимает
   линию. Зачем: частый запрос интеграторов, без конфликта за COM-порт (читаем из
   MQTT-кэша). Правила: allow-list `bind/allow_from` с тем же fail-closed-контрактом, что
   `gateway-network-access.md`; запись — отдельный opt-in на контрол, только в `/on`;
   незамапленный адрес → исключение 02, устаревшее/`meta/error=r` → 04 или 0B (не 06, как
   у Pult). **Усилие: M.**
3. **Роль «только чтение» и TLS на внешнем брокере.** Pult: `mqtt_readonly`, per-var
   readonly, TLS 1.2/1.3 AEAD (`map_mqtt/config.cfg`). У нас: один `mqttuser` с
   `readwrite /devices/#`, 1884 без TLS (`etc/mosquitto/*`). Зачем: мониторинговой SCADA
   или облаку не нужно право двигать реле; сейчас утечка одного пароля = запись в
   выходы. Реализация: второй пользователь `topic read /devices/#` (ACL), опциональный
   listener 8883 с self-signed per-device сертификатом. **Усилие: S (ACL) / M (TLS).**
4. **Метка времени и качество значения в JSON (аддитивно).** Pult: `fields=name,time,value,error,…`.
   У нас: голое значение + `meta/error`. Зачем: облако/SCADA отличают свежее значение от
   залипшего retained после ребута моста. Реализация: как `/meta`-блоб — отдельный
   retained топик `/devices/<id>/controls/<n>/state` `{"v":…, "ts":…, "err":""}`, WB-топики
   байт-в-байт. **Усилие: S–M.**
5. **Внешние MQTT-устройства (клиент чужих брокеров / чужих топиков).** Pult: drv_mqtt —
   JSON-path с `*`+условие, regex, text, HEX, таблица подстановки, шаблон записи,
   `drv_timeout` → ошибка «издатель офлайн». У нас: нет — Zigbee2MQTT/Tasmota/Shelly нельзя
   завести в «Устройства»/Алису/сценарии. Реализация: тип устройства `mqtt-ext` в мосте,
   переносящий чужой топик в `/devices/<id>/controls/*` + `meta/error=r` по таймауту
   тишины. **Усилие: M–L.** Даже без всего драйвера — взять «таймаут тишины →
   meta/error» для виртуальных устройств (сценарии, Carel по TCP). **S.**
6. **Метрики связи как MQTT-контролы.** Pult: переменные шлюза online/offline/ping.
   У нас: только `/run/sa02m-cloud-status.json`. Зачем: сценарий/SCADA может сработать на
   потерю облака. Реализация: `/devices/SA-02m/controls/cloud_online`, `cloud_last_hb_s`
   из агента. **Усилие: S.**
7. **Telegram как локальный резервный канал уведомлений.** Pult: очередь 1000, 3 попытки,
   **actuality 3600 с** (не слать протухшие), привязка по `request_contact`, шаблоны `#if`.
   У нас: уведомления только через облако. Зачем: тревога доходит, когда облака нет или
   плата не привязана. Взять семантику очереди/актуальности и `%debug%`-подсказку в
   шаблонах; привязку — по одноразовому коду из панели, а не по телефону. **Усилие: M.**
8. **Числовой каталог ошибок с двуязычными описаниями.** Pult: `errors.json` в каждом
   сервисе (коды 2300–4246, `desc{en,ru}`). У нас: строковые `error`/`reason` в каждом
   контракте свои. Зачем: единые коды для UI/i18n/облака. **Усилие: S** (начать с моста).
9. **Сигнатура конфигурации на стыке.** Pult: `signature` сборки в каждом vars-файле,
   отказ при расхождении (`DRV_ERROR_SIGNATURE`, «update, signature mismatch»). У нас:
   облачные сценарии/конфиг моста без явной версии данных. Зачем: не применить сценарий к
   устаревшему набору устройств. **Усилие: S–M.**
10. **Режим симуляции значений.** Pult: `sim_random*` на переменную, `sim_enabled` в core.
    У нас: только headless-стабы. Зачем: демо/отладка сценариев и Алисы без железа;
    опубликовать фейковые МР-02м в MQTT на стенде. **Усилие: S.**
11. **Зона нечувствительности и минимальный интервал публикации.** Pult: `threshold`,
    `threstime` на переменную. У нас: мост публикует по опросу (проверить, есть ли дедуп
    по изменению). Зачем: трафик 1884/облака. **Усилие: S.**
12. **Per-device учётка удалённого доступа.** Pult hostclient: у каждой платы свой
    key/secret. У нас: общий `FRP_TOKEN` (известный O2, план — mTLS Phase 4). Это
    подкрепляет приоритет Phase 4, а не новая работа. **Усилие: L (облако).**

## 4. Что Pult делает хуже — НЕ копировать

1. **Анонимный брокер на всю «частную» сеть с правом записи по умолчанию.**
   `mqtt_allowed_ip` включает 10/8, 192.168/16, 100.64 (CGNAT/Tailscale) и только
   `172.16.*.*` (не весь 172.16/12 — маска неверна); логин закомментирован,
   `mqtt_readonly=0`. LIVE: CONNACK 0 анонимному клиенту из LAN. У нас наружу — только
   1884 с паролем; сохранить так.
2. **Нарушение протокола: нет SUBACK** (LIVE) и незамапленный Modbus-регистр → 06 Busy
   вместо 02 (LIVE).
3. **Запись в тот же топик, что и публикация** (map_mqtt и drv_mqtt: `drv_write` уходит в
   `drv_topic`). Эхо собственных команд, гонки, retained-команда переиграется. Наша
   отдельная `/on` без retain (`docs/contracts/mqtt-set-endpoint.md`) правильнее.
4. **Устаревшие TLS-протоколы/шифры**: `drv_gateway` и `server` —
   `ssl_protocols=TSSLv2,SSLv3,TLSv1,TLSv1.1,TLSv1.2,TLSv1.3` (опечатка + SSLv3/TLS1.0/1.1),
   шифры `…+MEDIUM`; `tunnelclient` — `SSLv2,SSLv3` + `RSA+3DES`. `drv_mqtt` —
   `drv_node_checkcert` по умолчанию 0.
5. **Канал шлюза без TLS и с удалённым исполнением**: `gateway_ssl_enabled=0`,
   `allowed_ip=*`, аутентификация MAC + секрет, при этом есть `gateway_exec` /
   `gateway_reboot` / `gateway_api`. Это RCE-канал по открытому тексту. У нас командного
   канала облака нет (F1 удалён) — не возвращать.
6. **Шаблон shell-команды с подстановкой значений** (`service_local_exec__command`,
   `%var_payload%`, пример с `mysql -e '… %var_name% …'`) — инъекция через payload.
7. **Идентичность туннеля = MAC + пароль** на публичном relay (`tunnelclient`,
   `validation=mac`): MAC угадывается/виден в LAN, стойкость = пароль.
8. **Веб на :80 без HTTPS, `allowed_ip=*`, дефолтные логин/пароль администратора и
   демо-учётка в `server/config.cfg`**; `letsssl` требует публичного домена — для LAN-плат
   не решение.
9. **Секреты открытым текстом в конфигах** (hostclient key/secret, tunnelclient
   password/passkey/secret, токены ботов). **Важно:** в зеркале на Яндекс.Диске
   (`…/Пульт Сервер/pult_server/services/{hostclient,tunnelclient}/config.cfg`) лежат
   заполненные значения — если они живые, их стоит считать раскрытыми и перевыпустить.
10. **Привязка Telegram по номеру телефона** — если бот не проверяет, что контакт свой,
    чужой номер = чужие тревоги. Для нас — одноразовый код из панели.

## 5. Ограничения аудита

- Строки бинарников — косвенное свидетельство; формат JSON-сообщений map_mqtt взят из
  справки схемы (`service_local_map_mqtt__fields`), не из живого трафика (на плате нет
  опубликованных переменных).
- `_strings/*.txt` в study kit пустые; использованы `scratchpad/mystrings/*.txt`.
- Проверка «отвечает ли брокер SUBACK на существующий топик» и формат ответа Modbus на
  замапленный регистр невозможны без изменения конфигурации платы.
- В threat-model (`docs/threat-model.md` строка A2/A3→S1 про «публикацию из LAN в 1883»)
  описан LAN-доступ к 1883, а в репо `listener 1883 127.0.0.1` — стоит сверить, какой из
  двух фактов верен на плате (не в рамках этого аудита).

## 6. Сверка с перегенерированным study kit (после сообщения координатора)

`pult_study/_strings/pult_*.txt` теперь непустые; сверены с `scratchpad/mystrings/`
(разница в числе строк — 1, то же извлечение). Все опорные строки подтверждены:
`set var: #%u` (map_mqtt, map_mbtcp, exec, trends), `auth failed` (map_mqtt, drv_mqtt),
`%node_prefix%` (map_mqtt), `gateway_exec` (drv_gateway, server), `buffer_id` (core, все
drv_*, server), `request_contact` (chnl_telegram), `reg_mlt` (map_mbtcp и Modbus-драйверы).
Уточнения: список `TSSLv2,SSLv3,TLSv1,…` есть ещё и в `drv_http` (не только drv_gateway и
server); `pult_exec.txt` содержит `/bin/sh` и `%var_payload%` — подтверждает, что
exec-сервис передаёт подстановку payload в shell (п. 4.6). Выводы не меняются.
