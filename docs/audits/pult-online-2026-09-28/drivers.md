# Pult.online server — аудит слоя драйверов и шаблонов устройств

Дата: 2026-09-28. Режим: только чтение. Бинарники не запускались; строки извлечены
python-скриптом из байтов (`scratchpad/drv_work/str/*.txt`) — **набор `pult_study/_strings/*.txt`
оказался пустым (0 байт у всех 31 файла)**, его генерация не удалась.

Обозначения путей:
- `PS/` = `C:/Users/admin/YandexDisk/ЦИНТРОН/Сборка линукс/Пульт Сервер/pult_server/`
- `SCH` = `PS/services/server/schema.json` (разбор: `scratchpad/drv_work/mbrtu.txt`,
  `drivers_specific.txt`, `node_proto.txt`)
- `STR/x` = `scratchpad/drv_work/str/pult_drv_x.txt`
- `SA/` = корень нашего репо (worktree `pulpit-server-audit-407296`)

---

## 1. Как устроены драйверы Pult

### 1.1 Процесс и среда
- Каждый драйвер — отдельный статический ARM-бинарник (`PS/services/drv_*/pult_drv_*`,
  1,3–4,2 МБ), конфиг — INI `config.cfg` (= `config.default`), общий каркас: `poll_interval=100`,
  unix-сокет `drv_x.us`, опционально TCP-порт 112xx, `allowed_ip`, логгер с ротацией
  (`logger_maxsize=100000`, `logger_maxfiles=5`), флаги `log_drv/log_var/log_mb/...`
  (`PS/services/drv_mbrtu/config.cfg`).
- Связь с ядром — JSON-RPC по unix-сокету (`services.json`: `core.us`, timeout 10000;
  у `drv_virt` ещё `server.us`). Подписка с буфером-повтором: «subscribed for %u vars from
  buffer_id %ju of signature %s», параметры `only_last`, `no_initial` (`STR/virt`, `STR/alarm`).
- Персистентность значений — собственный KV с WAL: `values.kvdb`/`value_index.kvdb`,
  `save_values`, `values_save_interval=10000`, `values_enable_wal=1`, коды KVDB_ERROR_* 3600–3616
  (`PS/services/drv_virt/config.cfg`, `drv_virt/errors.json`).

### 1.2 Скомпилированные переменные `data/vars/<drv>.json`
- Колоночный формат: `{"signature":…, "keys":{"nodes":[…],"vars":[…]}, "rows":{…}}`;
  `{}` = «поле не задано» (`PS/data/vars/drv_mbrtu.json`).
- **Одна подпись на сборку** (`09E59A16…` во всех пяти файлах `PS/data/vars/*.json`). Драйвер
  отказывает при расхождении: `DRV_ERROR_SIGNATURE` (4003), «update not sent, signature mismatch
  (drv | %s != %s | clnt)» (`STR/virt`) — ядро и драйверы гарантированно работают на одной сборке
  проекта.
- **Дедупликация физических узлов:** строки `nodes` в `drv_mbrtu.json` названы 8-hex-хэшем
  параметров связи (`AA844C06` = COM1, `00224EF2` = COM2); переменные ссылаются по индексу
  `nodeid`. `core.json` отображает глобальное имя → (драйвер, `varid`); `server.json` несёт
  путь узла, параметры, теги, `status_var`, а у переменных — полный дескриптор драйвера.
- Выражения компилируются в индексы: `"status($2,0,0,0,0)"` (`PS/data/vars/drv_virt.json`)
  вместо `$metric_up` — `$2` = varid в ядре.

### 1.3 Модель прототип → узел → переменная
- Прототип `@content.json` = `{params, drivers, reports, scripts, _drivers}`
  (`PS/data/projects/Проект1.project/prototypes/proto_mbrtu1.prototype/@content.json`):
  - `params[]`: `name/type(string|number|checkbox|list|dictionary)/default/list/help/group/
    caption/validation(maxVal,minVal,maxLen,minLen,len,port,host,ip,hex,mac,path)` и
    **`application: "drv_mbrtu.drv_node_port"`** — при создании переменной в это поле
    автоматически подставляется `%port%` (SCH `prototype_param`).
  - `drivers{drv:[vars]}` — разреженно, только отличия от умолчаний; `_drivers` — развёрнутая
    копия, где у каждой переменной проставлены все `%param%`.
- Узел `@prop.json` = `{"prototype":"proto_mbrtu1.prototype","param_port":"/dev/COM1",…}`
  (`…/nodes/Узел1.node/@prop.json`). Глобальное имя: `P<проект>_N<узел>_<var>`
  (`P1_N1_var_hr_1`). В выражениях — `$var` (узел) или `$P1_N2_var1` (чужой узел).
- Статус узла: `status_var` прототипа + функция `status($online,$work,$warning,$alarm,$service)`
  → 0 офлайн/1 онлайн/2 работа/3 предупр./4 авария/5 сервис; «Недостоверно=Ноль» обязательно
  (SCH `prototype.status_var`).
- Теги: наследуются прототип→узел→папки→шлюз, ограничивают видимость переменных группам
  пользователей и служат фильтром агрегаций (SCH `node.tags`).
- `extra_server_active=0` — переменная выкидывается из сборки И из счёта лицензии.
- `gateway: Авто|Локально` — при узле за удалённым шлюзом сборщик сам создаёт пару
  «удалённая переменная на шлюзе + локальная ссылка drv_gateway»; `buffer=1` — кольцевой
  буфер на шлюзе с досылкой после обрыва связи.

### 1.4 Общий слой обработки значения (у всех драйверов с данными)
SCH `prototype_drv_mbrtu` строки 67–158 (`drv_work/mbrtu.txt`):
- `round`; `threshold` (мёртвая зона по значению); `threstime` (не чаще N мс);
  `min/max` — **только для записи пользователем**; `coef/offset` (VarValue = Signal·k + b);
  нормализация `scl_hi/scl_lo/raw_hi/raw_lo`; `reset_interval/reset_value` (автосброс, для
  импульсных команд).
- Групповые операции: `group_vars` (прямые), `group_inv` (инверсные), `group_ignore`,
  `group_block` (ждать подтверждения — защита от петель), `group_sync` + `group_timeout` +
  `group_tolerance` (ядро само досинхронизирует ведомые), `group_trigger` (разрешающая переменная).
- Запись: `retry_count` (1), `retry_interval` (1000 мс); `readonly`.
- Опрос: `extra_polling=period|cron` (cron 5 или 6 полей), `drv_period`.
- Симуляция: `sim_random` + `period/delta/init/min/max` — значения драйвера игнорируются,
  для отладки мнемосхем без железа.
- Экспорт той же переменной: в Modbus-карту сервера/шлюза (`service_*_map_mbtcp__*`),
  в MQTT-карту (`service_local_map_mqtt__topic` = `%node_prefix%/%var_local_name%`, поля JSON
  `name,time,value,error,payload,gateway,path,params`), в командную строку
  (`service_local_exec__command` с `%var_value%`).
- `trend` (16 байт на запись), `extra_server_watch` (принудительная подписка сервера).

### 1.5 Каталог ошибок
Числовые коды, у каждого драйвера свой диапазон; значение переменной несёт `value+error+time+payload`
(`PS/services/drv_*/errors.json`):
- общее: NS_* 2300–2457 (сеть/SSL/HTTP/RPC), DRV_* 4000–4006: `WAIT_DATA`, `VAR_NOT_FOUND`,
  `READONLY`, `SIGNATURE`, `EXPRESSION`, **`UNRELIABLE_VAR` (4005)**, `UPDATE_SUBSCRIPTION`;
- mbrtu 4280–4298: `RESPONSE`, **`MB01…MB11` — каждое Modbus-исключение отдельным кодом**,
  `REQUEST_TIMEOUT`, `PORT_ERROR`, `PORT_CLOSED`, `QUEUE_OVERFLOW`, `DUPL_REQUEST_REJECTED`,
  **`DIFF_PORT_PARAMS`**, `REQUEST_CANCELED`; + `NS_MBRTU_ERROR_BAD_CRC/UNKNOWN_FUNCTION` 2420–2421;
- mbtcp 4170–4188 (+ `CONNECT_TIMEOUT`, `DISCONNECTED`, `NON_STANDARD_CODE`); mbrot 4370–4387;
- opcua 4270–4279, snmp 4300–4304, bacnet 4310–4321, http 4190–4193, mqtt 4120–4122,
  virt 4111–4117, alarm 4140–4149, gateway 4240–4246, gpsd 4230–4232, cam 4250, cmd 4210.

---

## 2. По драйверам

### drv_mbrtu (Modbus RTU)
Параметры узла (SCH): `drv_node_slave` (1–247), `port` (/dev/ttyUSB0), `speed` (110…4000000),
**`parity` 0/1/2, `stopbits` 1/2, `flow_control` 0/RTS/XON_XOFF** — у каждого узла;
`offset` (физ.адрес = адрес + смещение), `timeout` (100 мс по умолчанию!), `delay` (пауза между
запросами), `group` 0–4, `ignore_errors` 0–15, `writecmd` 16|6, `skip_if_timeout`.
Параметры переменной: `extra_data=register|metric`; `drv_reg_type` CL/DI/HR/IR; `drv_reg_addr`;
`drv_format` INT16, UINT16, FLOAT/INT32/UINT32 × ABCD/CDAB/BADC/DCBA, **BIT_0…BIT_15**;
`drv_hipri`; `drv_group_id`. Скрытые ключи в бинаре: `serial_mode`, `reg_off`, `reg_mlt`
(`STR/mbrtu`).
- **Группировка** (SCH `drv_node_group`): 0 — запрос на регистр; 1 — «без пробелов» (только
  подряд идущие адреса); 2 — «с пробелами» (максимальный блок, дыры читаются впустую);
  3 — «по переменным» (запрос на переменную); 4 — по `GroupId` (ручные блоки).
- **HI-PRI**: запрос в голову очереди (`insert_request_after_last_hipri_request` в `STR/mbrtu`),
  с честным предупреждением о голодании обычных переменных.
- **ignore_errors N**: N подряд ошибок чтения не переводят переменную в ошибку.
- **skip_if_timeout**: первый таймаут узла отменяет остальные его запросы в этом цикле и
  пропускает один цикл (`skip_node_requests`).
- **Рестарт порта на таймауте** (`restart_port`, `ns_serial_restart`), `clear_queue`,
  `check_write_queue`, `check_mbrtu_diff_con` → `DIFF_PORT_PARAMS` (разные скорость/чётность
  у узлов одного порта — ошибка на переменную, не тихий конфликт).
- Функции записи в бинаре: `WR05 CL`, `WR06 HR`, `WR15 CL`, `WR16 HR`.
- Метрики (переменная `extra_data=metric`): `requests, errors, vars, registers, quality, art,
  rps, eps, varps, regps, up, down` + `total_*` и **`total_queue`** по линии.

**Поведение по логам** (`PS/services/drv_mbrtu/logs/*.txt`, 10 файлов, 04.08.2025 22:18–22:30,
ведомых нет): узлы P1_N1 на /dev/COM1 и P1_N2 на /dev/COM2, 115200 8N1, slave 1, по 10
переменных CL/DI/HR/IR, группировка 1, skip_if_timeout=1, период 1000 мс.
- Каждый цикл: 4 запроса на порт (`RD01 CL 1-10`, `RD02 DI`, `RD03 HR`, `RD04 IR`) — 40
  переменных упакованы в 4 кадра.
- Отправляется только первый (`01 01 00 01 00 0A ED CD`), через 100 мс — `REQUEST_TIMEOUT`,
  затем «restart port», три остальных — `REQUEST_CANCELED`, через ~100 мс порт закрыт/открыт.
- Интервал между циклами 2,0 с (210 из 235 интервалов; 24 по 1,9 с) — период 1 с + пропуск
  одного цикла. На ~12 минут лога: **473 рестарта порта**, 236 отправленных кадров на порт,
  HR/IR/DI **ни разу** не запрашивались. Порты работают параллельно (независимые очереди).
- Пробел в логе 22:22:28→22:27:00 без рестарта драйвера (счётчик запросов 1E9F→20BC
  продолжился) — потеря журнала ротацией, а не остановка опроса. Один файл 100 КБ ≈ 52 с
  истории при `log_mb=1`.

### drv_mbtcp / drv_mbrot
Как RTU, плюс `drv_node_host`, `port` (502), `keepalive`, **`sharecon`** (одно TCP-соединение на
несколько узлов), у mbtcp ещё **`async`** (конвейер запросов без ожидания ответа; при нём
`delay` не работает). Ошибки: `CONNECT_TIMEOUT`, `DISCONNECTED`, `NON_STANDARD_CODE`.
mbrot = RTU-кадры (с CRC) поверх TCP, без async (SCH, `errors.json`).

### drv_mqtt (только модель переменной)
Узел: host/port/ssl/checkcert/timeout/username/password/qos/retain. Переменная: `drv_topic`,
`extra_format=num|json|text|regx`; `drv_num` — `dec` или HEX_BE/LE × INT8…UINT64/FLOAT/DOUBLE;
`drv_json` путь с `*` + `drv_match` условие (`sensors/*/params/id:102`); `drv_text`
(`%value%`), `drv_regx`; `drv_list` таблица подстановки строка↔число с `*`/`?`; `drv_write`
шаблон публикации с `%value%`; `drv_dcmsep`, `drv_wrrnd`; `drv_timeout` → `PUBLISHER_OFFLINE`.

### drv_opcua
Узел: `drv_node_url`, timeout, `subscription_attempts` (10), `subscription_interval`,
**`subscription_per_request` (1000)** — разбиение большой подписки на пачки, т.к. серверы
не успевают. Переменная: `drv_ua_nodeid` (i/s/g/b), метрика `online`. Внутри open62541,
политика `SecurityPolicy#None` (`STR/opcua`).

### drv_snmp
`drv_oid` (числовой), `community`, host/port 161/timeout 100 мс, period|cron, те же форматы
num/json/text/regx + HEX-кодировки и таблица подстановки. Ошибки TIMEOUT/RESPONSE/NO_DATA/OID/FORMAT.

### drv_bacnet
Узел: `device_id`, `udp_port` 47808, timeout 100, `broadcast_ip`; драйвер: `whois_period=10000`.
Переменная: `object_type` (полный список BACnet), `object_instance`, `object_property`
(по умолчанию present-value), метрики online/offline. ReadProperty/WriteProperty/Who-Is
(`STR/bacnet`). Нет COV (не найдено в строках).

### drv_http
`drv_incoming` 0 исходящий/1 входящий (приём push-запросов на свои порты); метод/URL/заголовки/
тело отдельно для чтения и записи; `drv_source` body/header/get_param/post_param/http_code;
форматы как у MQTT; **`drv_cache` 50 мс** — один запрос на несколько переменных с одним URL;
`checkcert`. Входящий режим с `timeout` → `OFFLINE`.

### drv_cmd
Команда чтения (`cat /dev/gpio1`), команда записи с `%value%`, форматы, `drv_cache`
(`rcache.c` в `STR/cmd`). Это исполнение произвольной shell-строки на каждую переменную.

### drv_gpsd / drv_cam
gpsd: параметры TPV (`lat, lon, alt, speed, …`), `?WATCH` запрос. cam: MJPEG/JPEG URL, кадр
в `payload.image` (Data URL), `drv_interval` для ограничения трафика.

### drv_virt (виртуальные переменные)
`extra_data` (SCH): `calc` (хранимое, модификатор `incr/decr/swap/rand` + `delta/initval/
min/max`, `nosave`), `expr`, `ping` (ICMP RTT, `retries`, `ping_noerror`), `time`
(timestamp/mday/wday/…/zone), `split` (читать из `$ir`, писать в `$hr`, с `split_sync` +
`split_sync_delay` 10 с), `startstop` (одна 0/1-переменная → импульсы в `$start`/`$stop`),
`api` (меняется API-функцией `change`, `api_timeout` → `OFFLINE`), `aggr` (sum/avg/min/max/
errors/count по тег-фильтру `a & !b`, `val_filter` на `x`, `val_expr`, scope node/project/server).
- **Движок выражений**: функции abs…tanh, `bit(X,Y)`, `bool`, `iif`, `float(A,B,C,D)`,
  `double(8 байт)`, `hi16/lo16/hi32/lo32`, `round/round2`, `clz32`, `trunc`, `random`, время
  `timestamp()/hour()/…/uptime()`, `alarm_unack/alarm_active/…` (`STR/virt`), операторы C-стиля.
  Суффиксы `$v__error`, `$v__time`, `$v__value` (последнее достоверное). Пересчёт — по
  изменению входов или по period/cron; `drv_counter` — накопление (интеграл).
- **Недостоверность** (`drv_expr_unrel`, `split_unrel`, `aggr_unrel`): 0 → результат =
  `DRV_ERROR_UNRELIABLE_VAR`; 1 → вход = 0; 2 → последнее достоверное.

### drv_alarm
Обычная и сводная тревога. Сигнал = выражение; условие `!=0, ==0, ==, !=, >, <, >=, <=,
отклонение (dev), скорость изменения (roc, period, rise/fall/both)`; уставка, `delay` (сек,
антидребезг только на активацию), `deadband` (гистерезис нормализации) — **все как выражения**;
класс 3 Авария/2 Неисправность/1 Предупреждение/0 Оповещение; квитирование, приоритет,
шелвинг (`action_shelve … period`), уведомления по шаблону `#template default_alarm.ntf`.

### drv_gateway
Удалённые шлюзы: переменная = `drv_varid` в пространстве шлюза, метрики online/offline/ping;
синхронизация `gateways_sync_interval=60000`, ошибки `WRONG_SECRET`, `INVALID_MAC`,
`DATA_VERSION`.

### Поставляемая библиотека шаблонов
`PS/services/server/templates/prototypes/` — **только 8 обобщённых** «standart_*»: empty,
mbrtu, mbtcp, mbrot, mqtt, opcua, bacnet, gateway. Ни одного вендорского устройства. Каждый
несёт параметры связи с `application` и метрики + `status`-переменную
(`status($metric_up,0,0,0,0)`), шлюз — ещё тревогу `alarm_offline` с `drv_delay 30`.
Единственный вендорский — скрытый шаблон проекта `templates/projects/segnetics`
(`info.json` `hidden:true`): прототип `segnetics_ventilation` на drv_mbtcp, 187 переменных
(DI 14336…, CL 15360…, IR 40960… `INT32_CDAB`) + HTML-экран «Мастер».

---

## 3. Сравнение с SA-02m

| Тема | Pult | SA-02m сегодня |
|---|---|---|
| Группировка запросов | 5 режимов (SCH `drv_node_group`) | `TemplatePoller.poll_io` — **один запрос на канал** (`SA/opt/sa02m-modbus-mqtt/bridge_template.py:350-371`) |
| Ошибка чтения канала | код ошибки на каждую переменную (MB01..MB11, TIMEOUT…) | конвенция `/controls/x/meta/error="r"` (`SA/docs/MQTT_TOPICS.md:11`), но **TemplatePoller её не публикует** — `except: log.debug; continue` (`bridge_template.py:354-356`); другие семейства публикуют (`bridge_carel.py:177`, `bridge_dtv_ce.py:133`) |
| Метрики связи | quality/ART/RPS/up/down/total_queue как переменные | только уровень моста `devices_online/poll_errors` (`SA/opt/sa02m-modbus-mqtt/bridge_mqtt.py:408-421`) и строка журнала раз в 60 с (`bridge_device.py:472-479`) |
| Параметры линии | parity/stopbits/flow на узле | жёстко 8N1 (`SA/docs/contracts/template-device.md` §8) |
| Конфликт параметров порта | `DIFF_PORT_PARAMS` на переменную | только ERROR в журнале (`SA/opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py:178-312`) |
| Мёртвый ведомый | skip_if_timeout + рестарт порта | счётчик отказов + экспоненциальный back-off после 3 отказов (`bridge_device.py:192-205`); но внутри одного прохода template продолжает читать все каналы |
| Форматы | 4 порядка байт × 32 бит + BIT_0..15 | u16/s16/u32/s32/float + word_order; битовые поля и byte_order — «громкий пропуск» (`template-device.md` §4) |
| 32-бит запись | FC16/FC06 выбор | отказ записи (`bridge_template.py:390-395`), хотя FC16 уже есть (`bridge_serial.py:509`) |
| Мёртвая зона/частота публикаций | threshold/threstime/round | нет у template |
| Cron-опрос | да | только `poll_s` на устройство |
| RTU-over-TCP клиент | drv_mbrot | не поддерживается (`SA/docs/contracts/bridge-modbus-tcp.md` §7) |
| TCP-клиент | keepalive/sharecon/async | один сокет на host:port, окна переподключения, окно на молчащий unit (`bridge-modbus-tcp.md` §6) — **лучше Pult** |
| Очередь записи | DUPL_REQUEST_REJECTED, QUEUE_OVERFLOW | коалесценция по (device, control) — выполняется последнее значение (`bridge_serial.py:588-596`) — **лучше** |
| Вычисляемые переменные | drv_virt: expr/split/startstop/aggr/ping | нет «виртуального контрола»; сценарии `sa02m-rules` — действия, не производные значения (`SA/opt/sa02m-rules/sa02m_rules/engine.py:1-8`) |
| SNMP | num/json/text/regx + HEX + таблица, cron | только `scale/units`, GET на OID (`SA/opt/sa02m-mqtt-snmp/sa02m-mqtt-snmp.py:217-267`) |
| Симуляция | sim_random на переменной | нет |

---

## 4. Идеи для SA-02m (по ранжиру)

1. **Блочное чтение в TemplatePoller** (режим «без пробелов» + ограничение дыры N и 125 рег.).
   Сейчас MP-02 AHU (23 канала, float-входы 4000…4046 подряд — `templates/config-mp02-ahu.json`)
   = 23 транзакции за проход; блоками — 2–4. На 9600/19200 это разница в секундах цикла и
   в доле шины для соседей. Обязателен откат на поканальное чтение при исключении 02 (дыра
   с «несуществующим» регистром). Усилие **M**.
2. **Per-control `meta/error="r"` в TemplatePoller** + различать код исключения
   (02 «нет регистра» ≠ таймаут). Приводит template к собственной конвенции
   `MQTT_TOPICS.md:11`; сейчас нечитаемый канал просто застывает на старом значении без флага.
   Усилие **S**. (Это расхождение с контрактом — по правилу «дефект назначается» стоит
   отдать Builder'у отдельной веткой.)
3. **Метрики связи устройства как контролы**: quality, ART (мс), errors/min, up на устройство
   и queue/cycle на порт — в `/devices/<id>/controls/link_*` или в `sa02m-bridge`. Данные уже
   считаются (`mark_ok/mark_fail`, цикл порта). Даёт виджет «качество линии» и тревогу
   без чтения журнала. Усилие **S–M**.
4. **Прерывание прохода на первом таймауте** (аналог skip_if_timeout, но без рестарта порта):
   после таймаута в `poll_io` остальные каналы устройства в этом проходе не читать; back-off
   дальше как есть. Сейчас до `offline_after_fails` мёртвое устройство платит N×timeout за проход.
   Усилие **S**.
5. **Parity/stopbits на порт** и превращение `mixed_*_conflicts` в видимую ошибку устройства
   (в MQTT/UI), а не только строку журнала. Закрывает 8N2-устройства WB. Усилие **M**
   (ключ пула `port:baud` → `port:baud:parity:stop`).
6. **Расширение формата шаблона**: BIT_n из регистра (адрес `reg:shift:1`), byte-order
   ABCD/CDAB/BADC/DCBA, 32-бит запись через уже существующий FC16, опция `writecmd 6|16`.
   Усилие **M**.
7. **Мёртвая зона и ограничение частоты публикаций** (threshold/threstime/round) на канал —
   меньше MQTT-трафика и записей в архив `sa02m-devices-logger`. Усилие **S**.
8. **Виртуальные устройства с выражениями**: expr с `iif/bit/hi16/float()`, режимы
   недостоверности (ошибка/ноль/последнее), `split` (читать IR — писать HR, прямо про уставки
   MP-02), `status()` 0–5 для цветного статуса устройства. Усилие **L**.
9. **Cron/медленный период на канал** (счётчики энергии раз в час). Усилие **S**.
10. **SNMP: форматы text/regex/json + HEX + таблица подстановки** (статусы ИБП строками).
    Усилие **S–M**.
11. **Режим симуляции канала** для демонстраций и headless-проверки UI. Усилие **S**.
12. **Декларируемые параметры шаблона** (`application`-привязка, валидация port/ip/hex)
    вместо свободного YAML — основа будущего мастера «добавить устройство». Усилие **M**.
13. **RTU-over-TCP клиент** (для шлюзов ЕКФ/Moxa в прозрачном режиме). Усилие **M**.

## 5. Что НЕ копировать
- **Рестарт порта на каждом таймауте** — по логу 473 закрытия/открытия COM за ~12 минут,
  и рестарт бьёт по всей линии, а не по мёртвому узлу. Наш back-off на устройство лучше.
- **skip_if_timeout в их виде**: первым всегда уходит один и тот же блок (CL), остальные
  блоки узла не запрашиваются никогда, пока он молчит — диагностически «слепо».
- **Таймаут 100 мс по умолчанию при 9600 по умолчанию** (`standart_mbrtu`): ответ на 125
  регистров на 9600 8N2 идёт ≈290 мс — большие блоки гарантированно «таймаутят». Наш
  таймаут от char-time (`bridge_serial.py:341-356`) правильнее.
- **Отклонение дублей записи** (`DUPL_REQUEST_REJECTED`) — у нас коалесценция к последнему
  значению, это правильнее для оператора.
- **drv_cmd и exec-хуки**: произвольная shell-строка с `%value%`/`%var_value%` на переменную —
  прямой путь к инъекции; противоречит `web-code-rigor.md` (allow-list до shell-слова).
- **Небезопасные умолчания**: `ssl_protocols=TSSLv2,SSLv3,TLSv1,…` и `allowed_ip=*` у
  drv_http/drv_gateway (`config.cfg`), OPC UA `SecurityPolicy#None`, `checkcert=0` у MQTT/HTTP,
  пароли брокера как обычные параметры прототипа.
- **min/max только для записи пользователем, а чужие значения без проверки** — у нас
  запись вне формата отвергается (`template-device.md` §3), не ослаблять.
- **Колоночный vars.json с `{}`-заглушками и позиционными индексами** — непрозрачен для
  диффа и ревью; наш YAML + контракт читаемее. Имена `P1_N1_x` зависят от внутренних id.
- **Журнал на 5×100 КБ** при детальном логе ≈ 4–5 минут истории — бесполезен для разбора
  инцидента.
- **Библиотека «шаблонов» без устройств** — 8 пустых каркасов; ценность у нас как раз в
  проверенных картах своих приборов (MR-02m, ДТВ, CE, Carel, MP-02).
