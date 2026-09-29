# Pult.online server — аудит платформы (runtime, надёжность, хранение, безопасность, эксплуатация)

Дата: 2026-09-28. Режим: только чтение. Ни один бинарник Pult не запускался; строки извлечены
python-скриптом (`scratchpad/strx.py`) в `pult_study/_strings_platform/pult_<svc>.txt`
(исходные `pult_study/_strings/*.txt` оказались ПУСТЫМИ — 0 байт). `db.sq3` открыт только
копией (`scratchpad/pult_db_copy/`) через `file:…?mode=ro`. Секреты, хэши паролей, ключи
лицензий и туннельные учётки в этот отчёт не переносятся.

Сокращения путей: `PS` = `C:/Users/admin/YandexDisk/ЦИНТРОН/Сборка линукс/Пульт Сервер/pult_server`,
`R` = наш репозиторий (worktree pulpit-server-audit-407296).

---

## 1. Как устроена платформа

### 1.1 Процессная модель
- 31 статически слинкованный ARM-бинарник на C (`/pult3/src/backend/<svc>/<svc>.c`, общие
  библиотеки `_libs/ns.c` (сеть), `ns_rpc.h`, `ns_http.h`, `kvdb.h`, `drv.h`, `http_parser.c`,
  OpenSSL **1.1.1l-dev** статически — EOL с 09.2023). Символы НЕ вырезаны → API читается по
  именам `api_*`.
- Запуск: LSB init `/etc/init.d/pult_server` (генерируется `PS/register`, который ещё и
  отключает SELinux) → `./start` → фоном `pult_supervisor` + `pult_rpcconsole start [list|-list]`.
- Supervisor (`PS/services/supervisor/config.cfg`) порождает каждый сервис как
  `sh -c <bin> >/dev/null 2>supervisor/errors/<svc>.tmp` — ещё 30 процессов-обёрток `sh`
  (~700 KB RSS каждая, `pult/08-proc-detail.txt`), по потоку на приложение (30 threads).
  fd `supervisor/lock` наследуется во все дочерние процессы (нет O_CLOEXEC).
- **Heartbeat**: каждый сервис пишет в `services/<svc>/running.tmp` 8 байт BE — epoch в мс
  (все 27 файлов в пределах ~2 с друг от друга, 2025-08-04 18:17). Supervisor считает сервис
  зависшим, если heartbeat старше `interval_for_response=9000` мс (`SUPERVISOR_ERROR_APP_HUNG
  3102`), рестарт через `interval_for_restart=1000` мс, `kill_app`/`proc_kill_children`.
- **Детект «прошлый запуск упал»**: при старте сервис видит несвежий `running.tmp` → строка
  `previous instance was crashed`, `service_run_after_crash`, в журнал уходит
  `1001 / 5028 SERVER_ERROR_RESTART_AFTER_CRASH` (в db.sq3 таких записей **37**), в ядре —
  `CORE_ERROR_JUST_CRASHED 4110`; stderr упавшего запуска сохраняется в
  `logs/crash_log_<svc>_<ts>.txt` / `last_crash_log.txt` (`crash_log_maxfiles=3`,
  `crash_log_maxsize=10000`).
- Прочие ручки supervisor: `nonstop_apps=tunnelclient,watchdog` (не останавливаются по
  `./stop`, только `./stop all`), `excluded_apps`, `exit_on_stop`, `restart_app_on_config_change=1`
  (рестарт сервиса при изменении его `config.cfg`), `check_overload` (CPU > 95 % 60 с →
  `CPU_OVERLOAD 3111`, выкл.), `check_connection` (нет ping до 77.88.8.8 180 с → скрипт
  `../../reboot`, выкл.), `cpu_usage_avg_count`. У каждого сервиса `cpu_core` (affinity) и
  `high_priority` (RT-приоритет).
- **Реальный провал на плате**: `PS/services/server/logs/crash_log_server_2025_07_29_*.txt` —
  `bind port 80: Address already in use` каждые ~1,1 с, бесконечно (наш nginx занимает :80).
  Нет backoff и нет лимита рестартов.
- Unix-сокеты `<svc>.us`, протокол JSON-RPC 2.0 + бинарный «BNRY» внутри, WS/HTTP API на TCP
  (`listen_tcp_port=0` по умолчанию, `allowed_ip=127.0.0.1`). `services.json` каждого сервиса —
  список пиров (имя, сокет, timeout, enabled).

### 1.2 RPC-методы (по символам `api_*`)
- supervisor: `start stop restart status list info reboot exit` + подписки.
- core и все `drv_*`: `exist info list status varinfo subscribe_upload`; `drv_virt` + `vars change data timeout`;
  `drv_gateway` + `gateway_api gateway_exec gateway_reboot gateway_reset_secret driver_stat`.
- trends: `read slice write log`; db: `journal_read journal_write notification_read/write/count/watch`;
  builder: `build_projects build_schedules cancel`; exec: `async`; script: `debug update`;
  chnl_telegram: `send`; log_viewer: `hello`; map_mbtcp: `map`.
- server (≈150 методов): `user_* session* var_get/var_set/var_list alarm_ack/unack/shelve/unshelve
  backup_create/list/restore/delete fdb_* (файловая БД проекта) license_add/delete/keys
  ddos_block/unblock/forget system_set_config/system_reset_config/system_check_config
  system_check_cron system_set_time system_reboot system_start/stop/restart_service trends_read/slice
  journal editor_hold_object editor_start_build …`.
- `./cli unix://core.us @set var=P1_N1_var1 value=12345` — CLI с префиксами типов параметров
  (`b_ i_ u_ j_ d_ s_ o_`).

### 1.3 Конфиг-паттерн
- `config.cfg` (рабочий) + `config.default` (заводской, байт-в-байт тот же набор ключей) —
  сброс к умолчаниям = копия; `allow_reset_to_defaults=1` в схеме.
- `config.shema` — INI-схема, которая РИСУЕТ UI настроек: секция на ключ, атрибуты `help`
  (JSON ru/en), `group`, `control=checkbox|number|input|select`, `list=a|b`, `validation=
  numeric,notEmpty,minVal:N,maxVal:N,mac`, `advanced=1`, `hidden=1`, `visibleif=<ключ>`,
  `sort`, `width`, `type=serverAlarmSoundFiles` (динамический список). В `server/config.shema`
  108 help, 103 group, 62 advanced, 48 visibleif. RPC `system_config_schema`,
  `system_config_data`, `system_check_config`, `system_reset_config`.
- Журнал фиксирует изменение конфига **диффом**: событие `1/1006`, data =
  `{"hostclient":{"ENABLED":1,"target_port":80,"target_ssl":0}}`.
- `errors.json` на сервис: `[{name, code}]`, общие диапазоны: FDB 2070–2078, NS (сеть/SSL)
  2300–2327, NS_HTTP 2400–2402, NS_MBRTU 2420, NS_MBROT 2430, NS_RPC 2450–2457, NS_MQTT 2460,
  TREND 3020–3022, DB 3030, SCRIPT 3090, SUPERVISOR 3100–3111, CHNL_TELEGRAM 3510–3515,
  KVDB 3600–3616, DRV 4000–4006, CORE 4100–4110, DRV_* 4111–4387 (по диапазону на драйвер),
  BUILDER 5201, SERVER 5000–5058. В коде — assert `errcodes[N-ERR_OFFSET]=="NAME"` (каталог
  сверяется с бинарником при старте). Тексты — во фронтенде/`lang.json`.
- Логирование: `logger_*` (maxsize 100 KB × maxfiles 5, maxline 256) + категорийные флаги
  `log_app/log_mb/log_rpc/log_ws/log_http/…` на сервис; в default часть включена, в рабочем
  выключено. Пример `drv_mbrtu` лога: построчный трейс Modbus-кадров по портам
  (`/dev/COM1 0115200 N 1 <- 01 01 00 01 00 0A ED CD`, `REQUEST_TIMEOUT`, `restart port`).

### 1.4 Хранение
- **KVDB** (`values.kvdb` + `values.wal`): снапшот из записей «u32 длина | служебные слова |
  u64 время мс | 8 байт значения (IEEE754 BE double) | слово статуса | ключ с NUL». Коды ошибок
  выдают дизайн: WAL-запись с CRC (`WAL_RECORD_CRC 3611`, `WAL_RECORD_SIZE 3610`),
  `FSYNC_WAL_FILE 3607`, компактация через temp-файл + rename (`WRITE/RENAME/OPEN_TEMP_FILE
  3614–3616`), `FSYNC_DB_FILE 3613`. Ядро: `values_save_interval=10000`, **`values_enable_wal=0`**
  (до 10 с потерь при обесточивании); `drv_alarm`: WAL включён. `server/ddos.kvdb` хранит
  блокировки IP (переживают рестарт).
- **Кольцевой буфер ядра** `buffer_size=100000` изменений, `chunk_size=10000`, `buffer.json`
  (`signature, buffer_id, time, changes[]`) — сервер дочитывает пропущенные изменения после
  разрыва связи со шлюзом (store-and-forward, per-var флаг `buffer`).
- **Тренды**: `data/trends/<var>` — один файл на переменную, 10 000 файлов, 45,7 MB, без
  заголовка, запись 16 байт: `u32 BE unix-сек | u16 BE мс | u16 флаги (всегда 0) | f64 BE значение`.
  Запись по изменению (медиана шага 1,06 с), все файлы монотонны по времени (защита
  `TREND_ERROR_ROLLBACK 3022`). `max_rows=3000` (строк на ответ/срез), `min_free_space=1000` MB,
  `max_trends_size=0`, `max_store_days=0`, чистка cron `0 4 * * *`, `cleanup_buffer_size=1 MB`.
- **SQLite** `data/db/db.sq3` (WAL, page 4096, auto_vacuum=0): `journal(id, regtime, evttime,
  executor, ip, gateway, project, object, value, data, context, event, error, dt, var, gvar, tags,
  description)` — **индекс на каждом из 17 столбцов**; `notifications(type, created, watched, user,
  notification, tok)`. 632 записи: context 1 = система (1001 старт, 1002 старт сервиса, 1006
  изменение конфига), 2 = пользователь (2001 вход, 2003/2004 сессия, токен в журнале усечён),
  3 = объекты/переменные. `notification_storage_days=30`, `max_db_size=1000` MB,
  `cleanup_period=0 3 * * *`.
- **Сборка проекта** (builder): дерево `data/projects/*.project/{nodes,prototypes,mimics,alarms,…}`
  с `@prop.json`/`@content.json`/`@access.json` компилируется в колоночный JSON на сервис
  `data/vars/<svc>.json = {signature, keys{nodes[],vars[]}, rows{…}}`; ВСЕ пять файлов и
  `buffer.json` несут ОДНУ подпись сборки; `DRV_ERROR_SIGNATURE 4003` — сервис отвергает
  данные чужого поколения. Правка проекта не влияет на рантайм до «сборки».

### 1.5 Безопасность
- Пользователи — каталоги `data/users/<login>.user/@prop.json` (salted hash 64 hex + соль 64 hex,
  алгоритм по строкам не установлен — вероятно однократный SHA-256; флаги `admin`,
  `editor_access`, подписки на классы уведомлений), сессии — файлы
  `sessions/<64hex>.session`. Права на объект — `@access.json` (`{"view":{"mode":"deny"}}`),
  проверки `is_write_access`, `is_editor_access`, `EXEC_ACCESS_DENIED`, `READONLY_ACCESS`,
  блокировка объекта редактором `editor_hold_time`.
- `login_backoff_interval=3`, `logout_interval=600` (автовыход по бездействию, предупреждение
  `logout_fadetime`), `TOO_MANY_SESSIONS`, `password_min_size`, API-ключи.
- Anti-DDoS в самом сервере: `ddos_period=10` с, лимиты соединений/новых соединений/байт на IP,
  `ddos_block_time=3600`, ручные `ddos_block/unblock/forget`, персистентно в `ddos.kvdb`.
- `allowed_ip` на каждый сервис (маски `192.168.*.*`); у веб-сервера `*`.
- TLS: по умолчанию `port_https=0`; `ssl_protocols=TSSLv2,SSLv3,TLSv1,TLSv1.1,TLSv1.2,TLSv1.3`
  (опечатка «TSSLv2» + SSLv3/TLS1.0/1.1), шифры `ALL:!ADH:…:+MEDIUM`; у tunnelclient
  `SSLv2,SSLv3,…` и `RSA+3DES`. `letsssl` (ACME-перекладчик из `/etc/letsencrypt/live`) выключен.
- Лицензии — файлы в `data/license/` с тиром в имени (`FREE0010`, `X0000100`, `X0001000` —
  число переменных) + контрольная сумма; ограничения `LICENSE_EXCEEDED/DUPLICATED/INVALID`.
- `PS/backup`: `rsync --rsh="sshpass -p $4 ssh -o StrictHostKeyChecking=no"` — пароль в argv
  (виден в `ps`), MITM не проверяется, `--delete` на удалённой стороне.
- Всё под root; скриптовый язык имеет `func_exec` (shell) и `func_reboot`; `drv_cmd` исполняет команды.
- В зеркале открытым текстом лежат учётки туннеля/хост-клиента и дефолтные пароли
  (`services/tunnelclient/config.cfg`, `hostclient/config.cfg`, `server/config.cfg`) — зеркало
  на Яндекс.Диске = утечка (значения не переносятся сюда).

### 1.6 Планировщик, скрипты, прочее
- scheduler: собственный cron (`cron_parse_expr`, `cron_next/prev`), производственный календарь
  `data/calendar/calendar.txt` с типами дней (`get_calendar_weekdaytype`), синхронизация
  календаря с `https://webscada.ru` (`sync_calendar_enabled=1`), обработка скачков системного
  времени (`time_correct_system_changes`, `time_correction_*`).
- script: собственный интерпретатор (recursive descent: `parse_let/parse_rule/parse_block…`),
  ~70 встроенных функций (math, время, `iif`, `bit/hi16/lo16`, `alarm_*`, `telegram`, `exec`,
  `reboot`), `default_script_timeout=100` мс, `test_script_timeout=10000`, перечитка каталога
  `check_files_interval=10000`.
- exec: `/bin/sh` async. log_viewer: чтение логов сервисов с исключениями.
- server: бэкапы на устройстве (`backup_create/list/restore/delete`, tar.gz через
  `pack_backup.sh`/`unpack_backup.sh`), `vardump` (экспорт переменных/значений в JSON/bin),
  брендирование, демо-режим, WS-туннель к шлюзам.

---

## 2. Сравнение с SA-02m и идеи (ранжировано)

| # | Идея | Pult | У нас | Почему | Усилие |
|---|---|---|---|---|---|
| 1 | Журнал аудита действий (кто/откуда/что, дифф конфига) в SQLite | `journal` context 1/2/3, `1006` = дифф конфига, вход/выход/сессия, `RESTART_AFTER_CRASH` | нет журнала доступа — прямо записано как открытая угроза: `R/docs/threat-model.md §5` («нет журнала доступа», A3-атрибуция) | закрывает атрибуцию, помогает сервису; дешево — CGI уже знает пользователя/IP | M |
| 2 | Детект «прошлый запуск упал / было обесточивание» + показ в UI | stale `running.tmp` → 5028, `last_crash_log.txt` | systemd `Restart=` есть, но факт некорректного завершения/рестартов не виден Оператору | «почему ночью пропали данные» отвечается одним бейджем; источник — `journalctl --list-boots`/`NRestarts` | S |
| 3 | Схема настроек → UI (help ru/en, validation, advanced, visibleif, reset-to-default) | `config.shema` + `config.default` | формы и валидация вручную по CGI (`R/www/network_config/cgi-bin/config.cgi`, `mqtt_config.cgi`, `gateway_config.cgi`) | одна декларация даёт валидацию на CGI и JS, i18n, «вернуть по умолчанию»; снимает класс ошибок экранирования | L (начать с одного раздела) |
| 4 | Каталог кодов ошибок с диапазонами по подсистемам (+ сверка при старте) | `errors.json`, `errcodes[N]==NAME` assert | строковые коды разрозненно (`E_CSRF`, `E_CMD` в `lib_web_auth.sh`, `web_backup.cgi`) | единый дом кодов → переводимые тосты, grep по коду в поддержке, quality-гейт на уникальность | S–M |
| 5 | Backoff/лимит рестартов + наблюдаемость | нет (антипример — см. §3) | 18× `Restart=on-failure`, но `StartLimitBurst` только в 3 юнитах, `WatchdogSec` — в `sa02m-serial-gateway.service` и 2 мостах | довести до всех демонов: `StartLimitIntervalSec/Burst`, `WatchdogSec`+`sd_notify` в `opt/sa02m-rules`, `sa02m-flasher`, `sa02m-devices-*` | S |
| 6 | Производственный календарь (праздники РФ) для расписаний | `calendar.txt`, weekdaytype, sync | в движке сценариев только `workday/weekend` по дням недели (`R/docs/contracts/cloud-scenarios.md:294`) | ПВУ/освещение в праздники — частый запрос; календарь можно тянуть из нашего облака | M |
| 7 | Устойчивость расписаний к скачку часов | `time_correct_system_changes` | `sa02m-rules` использует monotonic для бюджета, но поведение `time`/`sun` триггеров при NTP-скачке (плата без RTC стартует с 1970/старого времени) не описано в контракте | избежать массового срабатывания/пропуска после синхронизации времени | S (проверка+тест) |
| 8 | Store-and-forward буфер при потере связи | кольцевой буфер ядра 100k изменений | в `opt/sa02m-cloud-agent` очереди/спула не найдено | история в облаке без дыр при обрывах LTE/VPN | M |
| 9 | Архив «по изменению» с deadband вместо фиксированного шага | тренды on-change, 16 B/точка | `sa02m_devices_logger.py`: фиксированно 1 с (5 с на eMMC), MR 10 с | в разы меньше записей на eMMC/SD при той же точности; ресурс флеша | M |
| 10 | Бэкап на устройстве + восстановление из UI | `backup_create/list/restore/delete` | только скачивание (`web_backup.cgi` → `sa02m-web-backup.sh`), restore-CGI нет | откат после неудачной наладки без ssh | M |
| 11 | Категорийные трассы в лог по флагам + ротация | `log_mb/log_app…`, 100 KB×5 | трассировки Modbus в мосте/прошивальщике включаются правкой кода/env | диагностика RS-485 на объекте без пересборки | S–M |
| 12 | Поколение конфигурации (одна подпись на все сервисы) | `signature` в `vars/*.json` и `buffer.json` | мост/логгер/rules читают свои файлы независимо | защищает от смешения «новый мост + старый маппинг» после частичного применения | M |
| 13 | Автовыход по бездействию для админки | `logout_interval=600` | скользящая сессия 10 суток (`SA02M_SESSION_TTL=864000`, `lib_web_auth.sh:12`) | оставленный открытым браузер у щита = открытый root-UI | S (решение Оператора) |
| 14 | Анти-флуд на уровне IP | `ddos_*`, бан персистентен | блокировка логина 10/300 с есть (`lib_web_auth.sh` `web_login_*`), общего rate-limit нет | nginx `limit_req/limit_conn` на `/cgi-bin/` — дешево и снимает DoS форками bash | S |
| 15 | per-user роли (просмотр/управление/админ) | users + `@access.json` | единый общий пароль (`threat-model.md §5`) | уже кандидат в threat model; Pult показывает минимальную модель: admin / editor / read-only | L |

Уже есть у нас и лучше/наравне (не копировать): атомарная установка `sa02m_atomic_install`
(`R/scripts/lib.sh:513`), never-widen refresh (`R/docs/contracts/installer-refresh-policy.md §1`),
sha512-crypt паролей + CSRF + хэш токена в имени файла сессии (`lib_web_auth.sh`), lockout
логина, SQLite-архив с retention/WAL/выбором носителя (`opt/sa02m-devices/…/history_store.py`,
`stand_storage_path.py` statvfs), жёсткий дедлайн скриптов (`code_runner._Deadline`),
`MemoryMax=32M` у rules.

---

## 3. Что Pult делает хуже — НЕ копировать

1. **Рестарт без backoff и лимита** — `bind port 80` раз в 1,1 с бесконечно
   (`services/server/logs/crash_log_server_*`); у нас systemd `StartLimit*` — держать.
2. **Свой supervisor вместо systemd** + `sh -c` на каждый сервис (+30 процессов), fd `lock`
   течёт в детей, init через LSB, нет cgroup-лимитов памяти/CPU. Не делать свой менеджер процессов.
3. **Всё под root**, скриптовый язык умеет `exec`/`reboot`, `drv_cmd` исполняет команды — ровно
   тот класс, который мы закрываем (`threat-model.md §5`).
4. **TLS**: HTTPS выключен по умолчанию, в списке SSLv3/TLS1.0/1.1 и мусорный «TSSLv2», MEDIUM-шифры;
   статический OpenSSL 1.1.1 (EOL) — не обновляется apt'ом. Если делать TLS — только TLS1.2+/1.3.
5. **Секреты открытым текстом в конфиге** (туннель, дефолтный админ, демо-пароль) и бэкап через
   `sshpass -p` в argv + `StrictHostKeyChecking=no`.
6. **Потеря до 10 с значений** при обесточивании в ядре (`values_enable_wal=0`,
   `values_save_interval=10000`) — WAL есть, но выключен по умолчанию.
7. **Индекс на каждом столбце журнала** (17 индексов) — усиление записи на флеше; у нас индексы
   только под реальные запросы.
8. **Файл на переменную** (10 000 файлов трендов, без заголовка/версии/CRC) — давление на inode и
   fsync; формат не самоописываемый.
9. **Одинаковый полный набор драйверов включён всегда** (bacnet/opcua/cam/snmp/gpsd `enabled=1`
   без проектов) — лишняя память; наш модульный `app on|off` лучше.
10. **`check_connection` → reboot при отсутствии интернета** — для автономного объекта опасно.
11. **Модуль Modbus RTU Pult открывает `/dev/COM1`/`COM2` напрямую** (`data/vars/drv_mbrtu.json`,
    лог `drv_mbrtu`) — вне нашего протокола аренды портов (`sa02m-domain.md ## Subsystems`);
    совместная установка Pult и наших MPLC/моста/прошивальщика на одну плату конфликтует по
    RS-485 и по :80.
12. `register` молча отключает SELinux; ядро на плате `6.1.0-rc6-rt4` (rc-ядро).
