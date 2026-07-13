# СА-02м Web Interface — Журнал изменений

**Версия 1.0.4** | Июль 2026  
Платформа: Armbian Linux (ARM) · nginx + fcgiwrap · Bash CGI + Python-демон `sa02m-flasher`

---

## 1.0.5.0 - Стабильность авторизации RS-485, ускорение HW-блока, portable-python gate (июл 2026)

### Безопасность и авторизация
- **Исправлена авторизация демона RS-485 (тост «Порты: HTTP 401»).** Успешный
  вход выдаёт случайный per-session токен; CGI-слой (`lib_web_auth.sh`) кладёт
  файл сессии с именем `sha256(token)`, содержимым `<expiry> <user>`, в
  setgid-каталоге `/run/sa02m-web-sessions` (файлы 640, группа www-data). Демон
  `sa02m-flasher` (`auth.py`) оставался на старой схеме — сравнивал cookie с
  константой `session_token=cyntron_session` (`check_session`) — и отклонял
  любую валидную веб-сессию → `/api/flasher/*` всегда 401. Теперь работают обе
  половины: демон читает то же серверное хранилище через
  `auth.py::check_session_store` (`sha256(token)` → файл → срок из содержимого,
  read-only), с новым ключом `SESSION_DIR` в `/etc/sa02m_flasher.conf` и
  tmpfiles-каталогом сессий. Токен не виден как имя файла (security).
- **Истёкшая сессия → страница входа.** Вместо тостов «HTTP 401: unauthorized»
  от отдельных виджетов (напр. «Порты») при 401 пользователя перекидывает на
  форму авторизации (централизованно, обёртка `fetch`). Одиночные транзиентные
  401 (краткая гонка в хранилище сессий) не разлогинивают: 401 перепроверяется
  лёгким запросом к `auth_check.cgi` (не к кэшированному `status.cgi`) — редирект
  только при подтверждённой пропаже сессии, иначе исходный GET тихо повторяется.
- **Обновление сессии — троттлинг и атомарность.** Продление срока веб-сессии
  выполняется атомарно и с троттлингом — устранены редкие ложные 401 от
  смешанного/гоночного доступа к хранилищу сессий при обновлении страницы.

### Производительность
- **HW-блок «Дискретный выход, USB-питание и индикация» грузится рано.**
  `status.cgi?part=hardware` перенесён из тяжёлой очереди опроса в лёгкую
  (ранняя фаза 500 мс): дешёвое чтение (~0.4 с, TTL-кэш, без живого I2C)
  больше не ждёт 2.2-с интервал тяжёлой очереди. Блок заполняется за ~3 с
  вместо ~18 с; период опроса 6 с (был 12 с) — нагрузка на I2C не растёт.
  Проверено на устройстве.

### Инструменты и качество (dev-only)
- **version-consistency gate выбирает рабочий python.** Строка реестра качества
  (`.ai-dev/quality/tools.json`) пробует `python3` → `python` → `py` и запускает
  первый рабочий интерпретатор. На Linux/CI поведение не меняется (`python3`);
  на Windows-машине разработчика, где `python3` — нерабочая заглушка Microsoft
  Store, гейт больше не падает ложно. Fail-closed: без рабочего интерпретатора
  гейт ПАДАЕТ. На веб-интерфейс и развёрнутые устройства не влияет.

### Установщик
- **Поддержка имён интерфейсов `end0`/`end1`.** `scripts/02-network.sh` больше
  не завязан жёстко на `eth0`/`eth1`: он определяет фактическое имя интерфейса
  (`first_existing_iface`, как в веб-UI), пишет конфиг под него
  (`interfaces.d/<iface>.conf`) и на плате с предсказуемыми именами не удаляет
  её `.link`-файлы. Повторный прогон установщика без `--ip` НЕ трогает живой
  интерфейс (не делает flush) — установка/обновление на плате с `end0` больше
  не рвёт связь. На платах с `eth0` поведение не изменилось.

## 1.0.4.1 - UI/UX: KPI-сводка, иконки карточек, контраст WCAG AA (июл 2026)

### Дашборд «Сведения»
- **KPI-ряд** над сеткой виджетов: 4 плитки-сводки — «Службы активны X/Y», «Ethernet-линки X/Y», «Линии RS-485 активны X/Y», «Предупреждения N» (CPU ≥80 %, t° ≥80 °C, ОЗУ ≥90 %, диск ≥90 %). Значения окрашиваются по состоянию (зелёный/жёлтый/красный), плитка предупреждений подсвечивается при N>0, tooltip перечисляет причины. **По требованию оператора ряд позже скрыт** (`display:none`, малоинформативен) — разметка и JS сохранены, включается одним атрибутом.
- **Иконки-чипы** в заголовках карточек (Система, CPU, Нагрузка, Температура, Uptime, ОЗУ, Диск, Службы, USB, microSD, RS-485) — цветные скруглённые контейнеры в стиле референса.
- **Точки-индикаторы** в pill «Линк / Нет линка» и бейджах служб («• Активен»).

### Читаемость и контраст (проверено по WCAG 2.1)
- Тёмная тема: вторичный текст `#8e8e93` → `#a5a5ac` — контраст на карточке 4.7:1 → 6.3:1 (AA).
- Все новые пары «иконка/фон чипа» и «значение/фон» ≥4.6:1 (AA), большинство ≥6.6:1 (AAA); проверены обе темы.

### Навигация
- Активный пункт меню — заливка акцентным градиентом с тёмным текстом (контраст 8.9:1/5.0:1 на краях градиента), как в референсном дашборде.

### Файлы
- **index.html:** KPI-ряд, иконки-чипы карточек.
- **main.css:** токены `--chip-*`, `--nav-active-*`, стили `.kpi-*`, `.w-ico*`, точки состояния.
- **app.js:** `kpiSet*()`, `kpiWarningsFromPriority()`; хуки в `renderServicesDynamic`, `applyNetworkStatus`, `renderRs485`, `applyPriorityStatus`.
- **i18n.js:** переводы новых подписей.

### Дашборд — исправления и HW-управление
- **Виджет «Ethernet № 1»** снова показывает актуальный IP. `status.cgi` жёстко
  читал `eth0`/`eth1`; на платах с предсказуемыми именами `end0`/`end1`
  (не перепрошитых на 1.0.4.x-образ) `eth0_ip` приходил пустым и в виджете был
  «—». Добавлен `first_existing_iface()` — статистика/IP/режим берутся с того
  из `eth0`/`end0` (и `eth1`/`end1`), что существует; ключи JSON не изменились.
- **Блок «Дискретный выход, USB-питание и индикация»:** пары кнопок Выкл/Вкл
  (Тихо/Звук) для DO, пищалки и аварийного LED заменены на **одиночные
  toggle-кнопки** — нажатие включает канал (кнопка заливается голубым, как
  активный пункт меню слева), повторное нажатие выключает. Кнопка «Сброс»
  питания USB на время сброса (10 с) горит тем же голубым.
- **Иконки виджетов** дашборда приведены к единой голубой (cyan) гамме, как у
  «Системы» (были оранжевые/зелёные у температуры, ОЗУ, диска, USB, microSD,
  RS-485).
- **Убраны точки-кругляшки перед статусами** (бейджи «Сервис активен/остановлен»,
  «Работает», «Остановлен», pill «Линк/Нет линка», бейджи служб) — статус читается
  без ведущей точки. Отдельные индикаторы (статусная колонка портов, точки COM в
  сайдбаре, точка активности RS-485) не тронуты.
- **Таблица «Устройства на шине» (MQTT):** заголовки были по центру, значения —
  слева; заголовки выровнены влево, колонки совпадают со значениями.

### Рефакторинг (F10 — декомпозиция god-файла `app.js`)
- **`app.js` (3326 стр.) разбит на ядро (389 стр.) + 7 модулей `static/js/app/*.js`**
  по ответственностям (`status`, `hw`, `rs485`, `forms`, `services`, `misc`,
  `init`). Обычные классические скрипты в общей глобальной области (ES-модули не
  вводятся), порядок загрузки в `index.html` = исходный порядок исполнения.
  Поведение-сохраняюще: подтверждено побайтовой полнотой (объединение файлов
  идентично исходному `app.js`) и характеризационным оракулом. `flasher.js`
  **не тронут** — единый связный IIFE вокруг общего `state`, безопасно не
  разбивается на глобальные скрипты (отложено, см. `.ai-dev/backlog.md`).
- **Характеризационный харнесс `scripts/dev/`** (dev-only, не деплоится):
  headless-обход всех вкладок×тем×вариантов с гейтом по инвентарю глобалей,
  ошибкам страницы и структуре DOM; режим `--target device` — проверка на
  реальной плате. Раскол проверен локально и на устройстве.
- **Quality:** `js-syntax` (`.ai-dev/quality/tools.json`) расширен на
  `static/js/app/*.js`.

## 1.0.4.0 - Debian 11 / WB-ядро, MQTT paho, виджеты и питание USB (июл 2026)

### Платформа
- **Debian 11 + ядро Wiren Board** `5.10.35-sa02m+`: миграция базового образа, обновление зависимостей.
- **paho-mqtt**: установка python-библиотеки в образ; guard'ы от «тихого» падения MQTT-мостов при отсутствии зависимости.
- Совместимость с **Python 3.9** (устранение PEP-604 `X | Y`-аннотаций в скриптах устройства).

### Исправления
- **codesyscontrol.service**: устранён uptime <1 мин (demo-mode + отсутствие отслеживания PID).
- **Веб-виджеты**: корректный вывод IP «Ethernet № 1», состояния USB-модема и формата виджета «Система».
- **Питание USB (VBUS)**: включение по умолчанию через выделенный `sa02m-usb-vbus.service`.
- Бэкенд: правки `status.cgi`, `apply.cgi`, `config.cgi`, `lib_rtc.sh`, `variant.cgi`, `ssh_debug.cgi`, `mqtt_status.cgi`.

## 1.0.3.35 - Kernel switch (RT/SMP), CPU profiles, flasher hardening (июн 2026)

### Прошивка MR-02m — защита от прерывания
- **sa02m-flasher:** `GET /status`, `any_active_job()`, `irreversible` на job; блок `/ports/release|restore` и `/cancel` после необратимой записи; callback `on_irreversible` в `FlashCancelGate`.
- **flasher.js:** reconnect к активной задаче после F5 (`sessionStorage` + `/status`); `beforeunload`; блок release/конфигурации при busy.
- **sa02m-web-service-ctl.sh:** `flasher_busy` через `GET /status` демона (не flock на lock-файлах) — блок Start MPLC4/MQTT мост только при реальной scan/flash.
- **app.js:** кнопка «Пуск» MPLC4/MQTT мост disabled при `flasher_busy`.

### Службы — единый статус и async Stop/Start
- **services_ctrl.cgi:** POST start/stop — немедленный `pending`, ctl в фоне; GET `?result=1`.
- **sa02m-web-service-ctl.sh:** `sc_run_slow` (45s) для SysV (mplc4, codesys); init.d/update-rc.d; CODESYS runtime в list; `flasher_busy` guard.
- **status.cgi:** статусы служб из `sa02m-web-service-ctl.sh list` (dashboard = Управление).
- **nginx:** `fastcgi_read_timeout 120s` для `services_ctrl.cgi`.
- **app.js:** poll до смены состояния; исправлен `!== wantStart`; подписи mqtt-bridge → «MQTT мост», mqtt-telemetry → «MQTT телеметрия».

### MQTT / обновления / UI
- **mqtt_status.cgi:** статус моста с учётом ctl `user_disabled` (как dashboard).
- **sa02m-web-update-check.sh + app.js:** semver — «Доступно обновление» только если remote > deployed.
- **index.html / main.css:** блок «Ядро и частота CPU»; тип устройства — select fit-content + Apply в одной строке; Ethernet Static/DHCP.

### Переключение ядра RT ↔ SMP (веб)
- **etc/sa02m-kernel-select.sh:** status/set/init; swap `zImage` на FAT `/dev/mmcblk2p1` из `/usr/local/share/sa02m/kernel/zImage.{smp,rt}`; `/etc/sa02m_kernel.conf`.
- **tools/buildroot/sa02m-kernel-deploy.sh:** деплой эталонных zImage и modules; `manifest.json`.
- **tools/buildroot/prepare-rt-docker-kernel.sh:** команда `build-kernel-smp` (SMP + Docker netfilter, без PREEMPT_RT) — сборка SMP-образа отложена на VM.
- **kernel_ctrl.cgi** + плитка «Ядро Linux» в Управление; reboot через `doReboot()`.
- **sa02m-pre-start.sh:** log mismatch desired vs running после reboot.

### Частота CPU (только SMP, не RT)
- **etc/sa02m-cpu-profile.sh:** профили performance/high/medium/low/adaptive; persist `/etc/sa02m_cpu.conf`; **sa02m-cpu-profile.service** на boot.
- **cpu_profile.cgi** + плитка «Частота CPU» (скрыта на RT-ядре).
- **status.cgi:** `kernel_is_rt`, `cpu_profile_ui_available`, `cpu_profile`, `cpu_governor`.

### Эталонные образы ядра (локально)
- **tools/buildroot/output/images/manifest.json:** MD5/size для `zImage.smp` и `zImage.rt` (6.1.0-rc6 SMP / 6.1.0-rc6-rt4), скопированы с устройства `/usr/local/share/sa02m/kernel/`.
- **prepare-rt-docker-kernel.sh:** fix `build-kernel-smp` (non-interactive Kconfig, без RT-патча, verify по `$LINUX_DIR/.config`).

### Настройка модулей MR-02m — AI sensor / port lease (июн 2026)
- **sa02m-flasher/service.py:** `device_config/*` через `port_lease` (освобождение COM от MQTT/MPLC на время Modbus-сессии, как scan/flash).
- **mplc_lease.py:** stop MQTT-моста — `systemctl stop` + `pkill modbus_mqtt_bridge`; детект процесса через `pgrep`.
- **flasher.js:** pending/inflight guard для типа AI-датчика (`_aiSensorPending`, `mergeAiChannelFromPoll`) — фоновый panel-опрос не откатывает выбор; compare-before-write для sensor/cal/filters; cache-bust `flasher.js?v=1.0.3.35.5`.
- **mqtt_config.cgi:** restart моста через `sa02m-web-service-ctl.sh start mqtt-bridge` (не только `systemctl restart` disabled unit).

### Окно настройки модуля — фоновый опрос 1 Гц (июн 2026)
- **flasher.js:** `setInterval` panel-опрос каждую 1 с (`startConfigPolling`/`configPollTick`); кнопка «Закрыть» всегда активна; `patchConfigLiveReadouts` при редактировании полей; исправлен deadlock (`_bgPollPromise`/`awaitConfigPortIdle` в `configApi`); освобождение MQTT до первого snapshot; cache-bust `flasher.js?v=1.0.3.35.9`.
- **mplc_lease.py:** `wait_port_poll_free()` после stop опросчиков; `port_lease(preserve_released=True)` — MQTT не перезапускается в сессии настройки.
- **service.py:** `ports/release` → `ok:true` при успешном release MQTT без busy PID; `device_config` с `preserve_released=True`.

> Подробности: docs/bugs/BUGLOG.md (записи 2026-06-25 09:42–10:30).

> Подробности AI sensor: docs/bugs/BUGLOG.md (записи 2026-06-24 14:36, 14:48).

## 1.0.3.34 - RT kernel, Docker, web services UI (июн 2026)

### RT-ядро и Docker на устройстве
- **tools/buildroot/prepare-rt-docker-kernel.sh:** patch-first RT (`patch-6.1-rc6-rt4`), `apply_sa02m_boot_kconfig` (sunxi/eMMC/eth), `apply_docker_netfilter_kconfig` (NAT/conntrack/BPF/raw), verify-хуки; fix `pipefail` на `olddefconfig`.
- **tools/buildroot/README.md:** сборка на VM, деплой zImage/modules, откат SMP.
- На SA-02m: ядро `6.1.0-rc6-rt4` PREEMPT_RT, `docker.io`, `hello-world` OK; `iptables-legacy`, `/etc/docker/daemon.json` (ipv6 off).

### Web UI — Docker в «Службы»
- **status.cgi:** Docker в `optional_services`; `fast_service_state` по `dockerd` (не `docker`).
- **sa02m-web-service-ctl.sh:** start/stop `docker.service` из «Управление → Службы» (как MPLC4).
- **app.js:** label Docker в управлении службами.

> Подробности инцидентов: docs/bugs/BUGLOG.md (записи 2026-06-23, ветка 1.0.3.34).

## 1.0.3.32 - CPU load, dashboard polling, MQTT AI types (июн 2026)

### Reset reason (MR-02m diag, июн 2026)
- **modbus_mqtt_bridge.py / device_config.py:** таблицы причин сброса приведены к `decode_reset_csr` (Input 65508); код **5** → POR/PDR, не WWDG.
- Декодирование reset reason: младший байт регистра (`& 0xFF`).

### MQTT system vars / uptime (июн 2026)
- **modbus_mqtt_bridge.py:** отдельный `_poll_uptime` каждые 5 с; `poll_slow_if_due` выполняется при IO backoff.
- **mqtt.js:** якорь uptime, 1 с tick и экстраполяция в live-ячейке; ingest monitor topics при batch poll.

### Web version (июн 2026)
- **VERSION**, `APP_VERSION`, все `?v=` в `index.html` / `login.html` — ровно **1.0.3.32** (без `.4` / `-1` суффиксов); `scripts/sync-app-version.py` синхронизирует все cache-bust в HTML.

> После деплоя исправлений смотрите docs/bugs/BUGLOG.md с кратким описанием багов.

### Dashboard / status.cgi (снижение нагрузки CPU)
- **Rolling scheduler:** очередь serial fetch (max 1 CGI in-flight), split LIGHT (6 s) / HEAVY (12 s), min gap 350 ms / heavy gap 2200 ms; priority 6 s, rs485 phase 10500 ms; bootstrap/login без burst prefetch.
- **RS-485 cache:** server-side TTL для part=rs485; routine poll без 
o_cache=1; интервал опроса 8-12 s.
- **cpu_usage sample cache:** Option C - delta по /tmp без sleep 100 ms на каждый priority tick.
- **Исправления UI:** пустые виджеты (progressive part=*), blocks config (
ull до загрузки), poll alert (per-part failures, benign abort), status.cgi chmod 755 / HTTP 403.
- **Hardware widgets:** GPIO/HW fetch даже при hardware=0 (TTL cache backend).

### MQTT / Modbus bridge
- **Миграция legacy AI sensor codes** в YAML и modbus_mqtt_bridge.py (i_sensor_schema: 2, 0..42); mqtt.js нормализует legacy при load/live poll.

### Flasher
- **AI type race fix:** сериализация device_config vs panel poll, edit-guard, per-port lock, refresh после apply.

### Версия web UI
- VERSION, cache-bust query strings синхронизированы с веткой **1.0.3.32**.

---

## 1.0.3.31 — Dashboard, RS-485, MQTT, HW (июнь 2026)

> После каждого исправления бага — запись в `docs/bugs/BUGLOG.md` и краткое описание здесь.

### Dashboard (вкладка «Сведения»)
- **Прогрессивная загрузка:** накопители, система, Ethernet, load avg, службы и HW опрашиваются параллельно (`part=storage|system|network|…`); каждый виджет обновляется по приходу своего ответа, без ожидания монолитного `part=main`.
- **Стабильная вёрстка:** skeleton/плейсхолдеры для Ethernet, RS-485, служб, load avg, swap, disk I/O — без «подпрыгивания» виджетов после загрузки данных.
- **Опрос status.cgi:** единый координатор (`initStatusPolling` / `teardownStatusPolling`), отмена in-flight fetch, rate-limit, перезапуск после BFCache; баннер при серии таймаутов.
- **Ethernet № 1/2:** префикс «Статический» / «Static» или «DHCP:» перед IP; RX/TX с двумя знаками в МБ (`fmtTrafficBytes`), обновление без «замирания».
- **Диск eMMC:** I/O read/write через findmnt → `mmcblk*` (не `/dev/root`); накопленные байты с загрузки.
- **Uptime:** скрыты нулевые дни («18ч 20м» вместо «0д 18ч 20м»).
- **Кнопка «Выход»:** hover как у переключателей языка/темы.

### RS-485
- Включён блок `SA02M_STATUS_ENABLE_RS485=1`; helper `sa02m-rs485-stats.sh` (sudo) для чтения `/proc/tty/driver/serial`.
- Подписи `RS-485-N (COMN+1)`, порядок 0-based; индикатор по TX/RX и delta FE/PE/OE (не lifetime OE).
- Убран stale JSON-кэш `rs485.json` — прямой `build_rs485_json`, опрос каждые 4 с.
- Modbus-мост: exclusive open порта, flush RX/TX при старте.
- Убрана flash-подсветка карточек и cyan TX/RX при изменении счётчиков; hover границ карточек сохранён.

### HW (дискретные выходы / USB)
- При `SA02M_STATUS_ENABLE_HARDWARE=0` — TTL-кэш метрик I2C/GPIO (`hw_metrics.snapshot`), без ложного «каналы не заданы».
- Кнопка **«Сброс» USB-питания** (async, ~10 с), блокировка повторного сброса; i18n для сообщений.
- **Исправлен сброс USB-питания:** gpioset держит линию 10 с в OFF и восстанавливает ON; статус UI совпадает с реальным состоянием (`lib_hw.sh`, `app.js`).

### MQTT (Modbus→MQTT)
- **Исправлено сохранение конфига:** `mqtt_config.cgi` снова исполняемый (755); при 403/CGI-сбое UI показывает понятную ошибку вместо «неизвестная»; toast при неудачной загрузке конфига.
- Таблица устройств: колонки **«Вх./вых.»** (физические каналы) и **«Каналы»** (число контролов с активным опросом, включая sys).
- Исправлен подсчёт каналов MR-02m (без sys в I/O); типы AI: fallback 0, edit-guard/pending при save и live-poll; live «Системные» после раскрытия аккордеона.
- Смена языка — мгновенный refresh динамических подписей (`refreshPriorityStatusI18n`, `refreshRs485I18n`, `mqttRefreshI18n`).

### i18n
- Динамические строки дашборда и MQTT через `uiT()`; обновление без ожидания следующего опроса CGI.

### Установка / деплой
- `scripts/03-webserver.sh`, `scripts/update-www-only.sh`: установка `sa02m-rs485-stats.sh`, sudoers для www-data.


### Веб UI: MQTT и прошивка (дополнение)
- **MQTT:** убраны маркеры «●» у подписей статуса (подключение, мост, устройства).
- **MQTT — таблица устройств:** колонка **«Топик»** (`/devices/{id}/#`), клик — копирование в буфер; toast «Топик скопирован».
- **MQTT — таблица устройств:** выбор строки кликом (toggle), подсветка выбранной строки (`mqtt.js`, `main.css`).
- **MQTT — таблица устройств:** заголовки колонок выровнены по центру.
- **MQTT — DI счётчики:** опрос di_N_count каждый цикл poll (Input Reg 77+2×(N−1), uint32 lo-hi); meta/title в мосте; nsureMr02mChannels() перед сохранением и отрисовкой MR-02m.
- **Toast MQTT:** уведомления перенесены в **правый верхний** угол (`main.css`).
- **Обновление прошивки (flasher):** понятные сообщения об ошибках сети/манифеста; вместо «Манифест 502» — **«Нет доступа к интернету»** (`firmware_repo.py`, `flasher.js`, i18n).


### Прошивки RS-485 и настройка модулей (дополнение, июнь 2026)

- **Кнопки прошивок:** «Скачать» / «Выбрать» / «Очистить»; POST /firmware/clear и FirmwareRepo.clear_cache() — удаление локальных образов, манифест сохраняется.
- **Очистить (backend/UI):** POST `/firmware/clear` возвращает полный status и entries; `list_entries()` синхронизирует `downloaded` с диском; UI сбрасывает выбор и корректно убирает строки «скачано» после очистки.
- **Загрузка .fw:** перезапись файла с тем же именем вместо filename.2.fw; UI поднимает обновлённую запись вверх списка; авто-выбор после скачивания/загрузки.
- **Список прошивок:** в таблице «Доступные прошивки» только скачанные в кеш образы (manifest + upload); записи манифеста без файла скрыты; «Скачать» — refresh манифеста и загрузка latest stable в кеш, затем обновление списка.
- **i18n:** описания прошивок в списке через `t()`; `flasherRerenderFirmware` при смене языка; убрана пометка «stable» из текстов каналов.
- **Окно настройки модуля:** вкладки сразу из сигнатуры сканирования (buildConfigSnapshotStubFromDevice); на «Сведения» плитки МК обновляются из panel-опроса (merge mcu).

### MR-02m: типы AI и Modbus-коды 0–42 (прошивка ≥1.0.9.1)
- Синхронизация кодов типа датчика в прошивальщике, MQTT-мосте и вкладке MQTT с `MR-02m/README.md` / `module_profiles.py` `AI_SENSOR_CHOICES` (0–42, не enum `ai_sensor_t`).
- **50П** (Pt50 α=0.00391, R₀=50 Ω): код **12** (2-пров.), **25** (3-пров.); **50М** (α=0.00428): **15** / **28** — по аналогии с 100П/100М (RTD bucket, 3-wire множества 21–33, масштаб °C ×0.1).
- Справочные пределы калибровки: 50П/100П/1000П −200…850 °C; 50М/100М/1000М −180…200 °C (из `table_rtd_alpha.h`); Pt50/Pt100 α385 −200…300 °C.
- **Миграция:** после обновления MR-02m ≥1.0.9.1 перепроверьте коды в SCADA/конфиге каналов — порядок NTC/RTD/U/I/TXA изменён; старые значения enum не совпадают с новыми Modbus selection codes.

---

## 1.0.3.29 — i18n RU/EN, время/RTC, сеть и прошиватель (июнь 2026)

### Локализация и интерфейс
- **RU/EN:** модуль `i18n.js`, атрибуты `data-i18n` в навигации и контенте; кнопка смены языка в шапке; подписи темы и пользователя локализуются.
- **Тема:** переключатель светлая/тёмная — кнопка в topbar (вместо внешнего SVG-object).
- Заголовок **«Сервер автоматизации СА-02м»** и отображение версии: выравнивание и интервал между названием и `v…`.
- Тосты, сервисы, заголовки вкладок Gateway и общие строки UI переведены через i18n.

### Вкладка «Время»
- Заголовок блока часового пояса уточнён; убрана подсказка про Москву.
- **Синхронизация RTC:** запись системного времени в DS3231/PCF8563 по I2C (`sync_rtc_from_system`); `apply.cgi`, `sa02m-rtc-sync`, загрузка времени в `sa02m-pre-start`; установка `sa02m-lib-rtc.sh` через `01-system.sh`.
- **«Время с RTC»:** общий `lib_rtc.sh` для `config.cgi` / `status.cgi` — чтение DS3231/PCF8563 по I2C при отсутствии `/dev/rtc1` в ядре; исправлено отображение «—».

### Сеть
- Блоки **Ethernet (eth0/eth1)** шире на десктопе; кнопка **«Применить»** выровнена вправо в форме интерфейса.

### Устройства RS-485 (прошиватель)
- Таблица после скана сортируется по **Modbus-адресу** по возрастанию.
- Описания прошивок и единицы (kB); адаптивная вёрстка кнопок прошивки на узких экранах.

### Инструменты
- `tools/www/check_i18n_missing.py` — проверка пропущенных ключей i18n.


## 1.0.3.28 — Dashboard, шлюз RS-485, прошиватель MR-02m (июнь 2026)

### Веб-интерфейс (Dashboard и навигация)
- Блок **HW outputs** перенесён выше **RS-485**; оба виджета занимают верхний ряд сетки (dash-span-top4, 4 колонки).
- Убраны подписи «Состояние:» у каналов HW; на виджетах дашборда — синяя подсветка при наведении.
- Шапка: единая строка «Сервер автоматизации СА-02м» + версия; клик по логотипу — «Общая информация»; убраны заголовки секций в боковом меню; иконка «Шлюз RS-485» обновлена.
- Синхронизация версии UI: www/network_config/VERSION, scripts/sync-app-version.py, cache-bust query-параметры.

### RS-485 на Dashboard
- Убран текст «○ свободен» / «● активен»; статус — **4 состояния** индикатора (серый / зелёный / оранжевый / красный) с подсказкой в 	itle.

### Шлюз RS-485 (Gateway)
- Панели COM одинаковой ширины (gw-device-stack); убраны пояснения режимов (gw-mode-hint); таблица портов с классом gw-ports-table и zebra-строками.
- Повторный клик по пункту «Шлюз RS-485» при открытой вкладке **сворачивает** подменю COM (gatewayNavClick).
- Убран лишний текст/упоминание COM5 в UI (профиль 1-eth: 4 порта).

### Прошиватель MR-02m (sa02m-flasher + UI)
- Маршрут прошивки по сигнатуре устройства (MR/MP .fw 115200 N1 vs WB .wbfw 19200 N2); multi-select и пакетная прошивка; UX манифеста («Скачать прошивки», список файлов, сообщения при DNS/offline).
- Быстрый prep→scan: пропуск stop/restart MPLC, если порт свободен по `fuser` (mplc_lease.is_port_poll_free).
- Политика кеша репозитория: «Проверить» скачивает stable/current; консолидация по channel/kind.
- Line profiles по сигнатуре (reg 129 / app line); исправления SSE reconnect после restart сервиса.
- Тесты: 	est_flash_route, 	est_module_line_profiles, 	est_runner_app_line, расширения firmware_repo/mplc_lease.

### Система и агенты
- sa02m-web-update-check: ручной режим (--manual / CGI 
orce=1) без спама 
etwork_or_git_failed в install.log; timer не пишет в journal.
- scripts/01-system.sh: DNS через шлюз (resolvconf head); документация SSH для агентов: docs/AGENTS_SSH_AND_DEVICE_ACCESS.md, 	ools/ssh/sa02m_remote.py.

---

## 1.0.3.17 — MQTT: доступность и надёжность в стиле wb-mqtt-serial

Применимо к Modbus→MQTT мосту (`opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`) и
системной телеметрии (`sa02m_telemetry.py`) — для всех модулей: **MR-02м**, **ДТВ**,
**СЭ-02м-3**.

- **Last Will (LWT):** при падении/обрыве моста брокер сам публикует
  `/devices/sa02m-bridge/meta/error="r"` (единый канал ошибок). Телеметрия — то же
  для `/devices/sa02m-<host>/meta/error`. Клиенты не доверяют устаревшим retained.
- **Доступность устройства (device-level):** после `offline_after_fails` подряд
  неответов мост публикует `/devices/<id>/meta/error="r"`, при восстановлении — `""`.
  Значения `controls/*` сохраняют последнее достоверное значение.
- **Экспоненциальный back-off опроса** «мёртвого» устройства (`backoff_base_s` …
  `backoff_max_s`) — не блокирует half-duplex RS-485 для живых устройств на шине.
- **Устройство-статус моста** `/devices/sa02m-bridge`: `connection` (switch),
  `devices_total`, `devices_online`, `poll_errors`.
- **Корректный graceful offline** при `systemctl stop` (мост и устройства помечаются
  offline до отключения от брокера).
- **Совместимость paho-mqtt 2.x:** телеметрия переведена на callback API v1
  (как мост) — фикс несовпадения сигнатур колбэков.
- Новые ключи конфигурации (`/etc/sa02m-modbus-mqtt.yaml`): `mqtt.availability`,
  `mqtt.bridge_device_id`, `mqtt.username`/`password`, и на устройство —
  `offline_after_fails`, `backoff_base_s`, `backoff_max_s`. Топики описаны в
  [docs/MQTT_TOPICS.md](docs/MQTT_TOPICS.md#доступность-как-в-wb-mqtt-serial).

---

## 1.0.3 — Устройства MR-02м (RS-485 / Modbus RTU / прошивка)

### Совместимость с репозиторием MR-02m (имена `.fw`, 2026-04)

- Сборка **MR-02m** (`make` / `full_build`) теперь сразу кладёт в `build/AppBoot/` канонические **`MR-02m_<M.N.P.S>.fw`** и **`MR-02m_bootloader_<версия_BL>.fw`** (см. корневой `Makefile` проекта MR-02m). Скрипт `opt/sa02m-flasher/scripts/prepare_firmware_for_site.py` при скане каталога по-прежнему в первую очередь выбирает `MR-02m*.fw`; комментарий в скрипте обновлён под новое поведение Makefile.

### Что нового

- Новая вкладка «Устройства MR-02м» в веб-интерфейсе: выбор RS-485 (COM1–COM5),
  запуск сканирования в двух режимах (стандартный адресный и быстрый Modbus
  `0xFD 0x46 0x01`), таблица найденных устройств (адрес, S/N, сигнатура, версии
  приложения и бутлоадера, скорость), массовая прошивка выбранных устройств.
- Поддержка прошивки MR-02m по адресу (reg `0x1000` + `0x2000`) и по серийному
  номеру через быстрый Modbus (`0xFD 0x46 0x08/0x09`), автоматический перевод в
  бутлоадер (reg `129`) и переход в приложение (reg `1004`) после прошивки.
- Репозиторий прошивок:
  - основной источник — манифест `https://cyntron.ru/upload/medialibrary/cyntron/firmware/index.json`
    (схема описана ниже), с локальным кешем в `/var/lib/sa02m-flasher/firmware/`;
  - резервный путь — ручная загрузка `.fw/.bin/.elf` через веб-UI (сигнатура и
    версия извлекаются из info-блока `.fw`).
  - **Один образ на всю линейку MR-02м:** подсказка «есть обновление» в таблице
    устройств показывается только для сигнатур из whitelist MR/MP-02м (как у
    пакетной прошивки) и только если на сайте в манифесте новее **приложение**
    (`latest_stable_version`, записи `kind: app`) и/или **бутлоадер**
    (`latest_bootloader_version`, записи `kind: bootloader` или имя файла
    `MR-02m_bootloader_*.fw`); прошивка по-прежнему только для этих сигнатур
    (либо с флагом обхода whitelist в UI для отладки).
- Координация с опросом RS-485: на время сканирования/прошивки демон
  останавливает службу `mplc.service` (список настраивается в
  `/etc/sa02m_flasher.conf`, ключ `MPLC_STOP_SERVICES`) и гарантированно
  запускает её обратно (в том числе `ExecStopPost`).
- Выгрузка прошивок на хостинг: хост SSH, пользователь, каталог на сервере и
  путь к `.ppk` не захардкожены в скриптах — задаются в локальном
  `firmware-site-export/site-deploy.config.json` (копия с
  `site-deploy.config.example.json`) или через переменные окружения
  `FW_UPLOAD_*` / `FW_PACK_SCAN_DIR`.
- Эксклюзивный захват порта через `flock` на `/var/lock/sa02m-flasher-<port>.lock`
  и предварительная проверка `fuser` — исключает конфликт двух операций.
- Post-mortem: каждое событие, уходящее в SSE (`jobs.py`), дублируется строкой
  JSON в `/var/log/sa02m-flasher/events.log` (ротация через тот же шаблон
  `*.log` в `logrotate.d/sa02m-flasher`).
- Unit-тесты (`unittest`): `opt/sa02m-flasher/tests/` — разбор манифеста и
  проверка `sha256` при скачивании (`test_firmware_repo.py`), cookie-сессия и
  internal token (`test_auth.py`), запись `events.log` (`test_jobs_events_log.py`).
  Запуск из каталога `opt/sa02m-flasher`:  
  `PYTHONPATH=. python -m unittest discover -s tests -p "test_*.py" -v`
- Скрипт подготовки файлов для сайта: `opt/sa02m-flasher/scripts/prepare_firmware_for_site.py`
  — канонические имена `MR-02m_<X.Y.Z.W>.fw` / `MR-02m_bootloader_<X.Y.Z.W>.fw` /
  `MR-02m_<slug>_<X.Y.Z.W>.fw` и `index.json` из каталога с `.fw`; опция
  `--bundle-dir` копирует переименованные `.fw` и пишет манифест в один каталог
  для выгрузки на сайт. Каталог `firmware-site-export/`: в git — скрипты `pack_*` /
  `upload_*`, шаблоны **`site-deploy.config.example.json`** и
  **`SITE_AND_FIRMWARE_UPLOAD.md.example`**; не в git — **`site-deploy.config.json`**
  (хост, пользователь, пути, путь к `.ppk`), манифест **`index.json`**, бинарники
  **`*.fw`**, приватная памятка **`SITE_AND_FIRMWARE_UPLOAD.md`** (см. **`.gitignore`**).

### Архитектура

- **Backend:** Python 3 демон `sa02m-flasher` (systemd unit
  `/etc/systemd/system/sa02m-flasher.service`). HTTP-API на stdlib
  `http.server.ThreadingHTTPServer` поверх unix-сокета
  `/run/sa02m-flasher/flasher.sock`. События (прогресс, лог, найденные устройства)
  стримятся в UI через Server-Sent Events.
- **Библиотека Modbus/flash:** перенос из референсного проекта
  `MR-02m-flasher/flasher_windows` (модули `modbus_rtu.py`, `modbus_io.py`,
  `serial_port.py`, `scanner.py`, `flash_protocol.py`, `firmware.py`,
  `serial_ranges.py`, `module_profiles.py`, `flasher_log.py`,
  `modbus_tcp.py`). Копируются как есть, без GUI-кода.
- **Nginx:** новые location-блоки `/_auth_check` (внутренняя авторизация
  через cookie `session_token`) и `/api/flasher/*` → `proxy_pass`
  `http://unix:/run/sa02m-flasher/flasher.sock`. SSE-эндпоинт выделен отдельно с
  `proxy_buffering off` и `proxy_read_timeout 3600s`.
- **Frontend:** новая страница `Устройства MR-02м` (`index.html`),
  модуль `www/network_config/static/js/flasher.js`, стили в
  `static/css/main.css`.

### Безопасность

- Отдельный системный пользователь `sa02m-flasher` (не `www-data`):
  в группах `dialout` (для `/dev/ttyS*`) и `www-data` (для доступа к сокету).
- `sudoers.d/sa02m-flasher` разрешает только конкретные команды
  (`systemctl {start,stop,is-active} mplc.service` и `fuser /dev/COM{1..5}`,
  `fuser /dev/ttyS{0,3,4,5,7}`).
- Systemd unit с усиленными параметрами (`ProtectSystem=strict`,
  `PrivateTmp`, `NoNewPrivileges=no` — иначе недоступен `sudo` для
  `mplc_lease`, `ReadWritePaths`).
- Авторизация API — по cookie `session_token=cyntron_session` через
  `auth_request /_auth_check` (CGI `auth_check.cgi`). При необходимости —
  дополнительный общий секрет `INTERNAL_TOKEN` (заголовок `X-SA02M-Auth`).

### Схема `index.json` на cyntron.ru

```json
{
  "schema": 1,
  "updated": "2026-04-20",
  "channels": {
    "stable": [
      {
        "file": "MR-02m_1.2.3.0.fw",
        "version": "1.2.3.0",
        "signatures": ["mp02m"],
        "device": "MR-02m",
        "size": 34816,
        "sha256": "…",
        "released": "2026-03-15",
        "notes": "исправление опроса ADS1220"
      }
    ],
    "beta": []
  }
}
```

Поля: `schema` (версия формата, сейчас `1`), `updated` (дата обновления
манифеста), `channels.<name>[]` (каналы `stable`/`beta`). Для каждой прошивки:
`file` (имя в каталоге `firmware/`), `version` (обязательно X.Y.Z.W — видно в
UI), `signatures[]` (допустимые сигнатуры устройств — демон подбирает
совместимые прошивки по сигнатуре из Modbus-регистра `290`), `size`, `sha256`
(для контроля целостности при скачивании), `released`, `notes`.

### Файлы

| Назначение | Путь в репозитории | На устройстве |
|-----------|--------------------|---------------|
| Python-демон | `opt/sa02m-flasher/sa02m_flasher/` | `/opt/sa02m-flasher/` |
| Конфигурация демона | `etc/sa02m_flasher.conf` | `/etc/sa02m_flasher.conf` |
| systemd unit | `etc/sa02m-flasher.service` | `/etc/systemd/system/sa02m-flasher.service` |
| sudoers | `etc/sudoers.d/sa02m-flasher` | `/etc/sudoers.d/sa02m-flasher` |
| logrotate | `etc/logrotate.d/sa02m-flasher` | `/etc/logrotate.d/sa02m-flasher` |
| CGI auth для nginx | `www/network_config/cgi-bin/auth_check.cgi` | `/var/www/network_config/cgi-bin/auth_check.cgi` |
| UI вкладка | `www/network_config/index.html` + `static/js/flasher.js` + `static/css/main.css` | `/var/www/network_config/…` |
| Скрипт установки | `scripts/04-flasher.sh` (+ правки `install.sh`, `03-webserver.sh` — nginx) | — |

### HTTP API (короткая справка)

Все эндпоинты — под префиксом `/api/flasher/`.

| Метод | Путь | Описание |
|-------|------|----------|
| GET  | `/health` | Проверка живости (открыт без авторизации). |
| GET  | `/ports` | Список COM1..COM5 (device_path, занятость, активная задача, статус mplc). |
| GET  | `/firmware` | Статус репозитория + список прошивок. |
| POST | `/firmware/refresh` | Перечитать `index.json`. Тело `{"download": bool}`. |
| POST | `/firmware/upload` | multipart/form-data `file=<.fw/.bin/.elf>`. |
| POST | `/scan` | Старт сканирования (JSON: `port`, `mode`, `baudrates[]`, `addr_min/max`, `parity`, `stopbits`). |
| POST | `/flash` / `/flash_batch` | Прошивка одного/нескольких устройств. |
| POST | `/cancel` | `{"job_id": "..."}` — отмена задачи. |
| GET  | `/jobs` | Список последних задач (snapshot). |
| GET  | `/jobs/<id>` | Снэпшот задачи (state, progress, events, devices). |
| GET  | `/jobs/<id>/events` | SSE-стрим (Content-Type: text/event-stream). |

---

## Содержание

1. [Реструктуризация проекта](#1-реструктуризация-проекта)
2. [Дизайн-система и UI](#2-дизайн-система-и-ui)
3. [Статические страницы (SPA)](#3-статические-страницы-spa)
4. [Страница входа — анимация огня](#4-страница-входа--анимация-огня)
5. [JavaScript — логика приложения](#5-javascript--логика-приложения)
6. [CGI API — новые и обновлённые эндпоинты](#6-cgi-api--новые-и-обновлённые-эндпоинты)
7. [status.cgi — расширение метрик](#7-statuscgi--расширение-метрик)
8. [Dashboard — новые виджеты](#8-dashboard--новые-виджеты)
9. [Сетевой watchdog — полная переработка](#9-сетевой-watchdog--полная-переработка)
10. [Модульный установщик](#10-модульный-установщик)
11. [Структура файлов](#11-структура-файлов)

---

## 1. Реструктуризация проекта

### Проблема (было)
Весь HTML/CSS/JS был встроен в один bash-скрипт `web/web` в виде heredoc-ов. Это делало невозможным:
- редактирование файлов в IDE без сложных правок установщика;
- версионирование отдельных компонентов;
- деплой без повторного запуска всего монолитного скрипта.

### Решение (стало)
Проект разделён на независимые файлы. Установщик **копирует** готовые файлы, а не генерирует их.

```
install.sh                        ← точка входа: sudo ./install.sh [--ip X] [--port Y]
scripts/
  lib.sh                          ← общие функции: log(), pkg_install(), svc_enable()
  01-system.sh                    ← ОС: пакеты, пользователи, udev-симлинки RS-485/COM
  02-network.sh                   ← сеть: eth0 static IP, watchdog, udev правила
  03-webserver.sh                 ← nginx + fcgiwrap + sudoers + деплой www/
etc/
  nginx/network_config.conf       ← шаблон nginx (токены __PORT__, __WEB_ROOT__)
  sa02m_hw.conf                   ← шаблон GPIO (DO, beeper, alarm LED)
  sa02m_network.conf              ← шаблон настроек watchdog (WATCHDOG_PING_*)
  fix-eth.sh                      ← скрипт восстановления сети
  fix-eth.service                 ← systemd unit (oneshot, triggered by udev)
  net-watchdog.sh                 ← постоянный демон мониторинга
  net-watchdog.service            ← systemd unit (simple, Restart=always)
  99-lan-recovery.rules           ← udev правила
www/network_config/               ← готовые файлы для деплоя в /var/www/network_config/
  index.html
  login.html
  static/css/main.css
  static/js/app.js
  static/logo.svg
  cgi-bin/                        ← API-скрипты (без HTML внутри)
    config.cgi  status.cgi  hw_set.cgi
    apply.cgi   login.cgi   logout.cgi
    restart.cgi reboot.cgi  log.cgi
```

---

## 2. Дизайн-система и UI

### Файл: `www/network_config/static/css/main.css` *(новый)*

Полноценная дизайн-система на CSS custom properties, вдохновлённая [mongoose.ws](https://mongoose.ws).

#### Цветовые токены
```css
--bg:          #1a1a1a   /* основной фон */
--bg-nav:      #1f1f1f   /* боковая панель */
--bg-card:     #252525   /* карточки виджетов */
--bg-toolbar:  #353535   /* верхняя панель */
--cyan:        #22d3ee   /* акцент — ссылки, иконки */
--cyan-btn:    #0891b2   /* кнопки */
--cyan-hover:  #06b6d4   /* hover кнопок */
--green:       #3fb950   /* успех, активный */
--yellow:      #e3b341   /* предупреждение */
--red:         #f85149   /* ошибка, опасность */
```

#### Компоненты
| Компонент | Описание |
|-----------|----------|
| `.btn`, `.btn-primary`, `.btn-danger` | Кнопки с `transform: scale(0.95)` при `:active` |
| `.toggle` | Переключатель (mongoose-точный) — анимация `cubic-bezier(.34,1.56,.64,1)` (bounce-эффект) |
| `.toggle:hover` | Свечение `box-shadow: 0 0 0 2px var(--cyan-dim)` |
| `.widget` | Карточка дашборда |
| `.badge-ok.pulse` | Пульсирующий badge активного сервиса (`@keyframes badge-pulse`) |
| `.rs485-port.act` | Подсветка активного RS-485 порта |
| `.gauge-arc` | SVG дуга с `transition: stroke-dasharray .4s ease` |
| `.bar-fill` | Прогресс-бар с `transition: width .4s ease` |

#### Анимации
```css
@keyframes fadeIn      /* появление вкладок (opacity + translateY) */
@keyframes badge-pulse /* пульс сервисных badge */
@keyframes spin        /* индикатор загрузки */
@keyframes pulse       /* свечение (как в mongoose) */
@keyframes blink       /* мигание */
@keyframes toastIn     /* появление уведомлений */
```

---

## 3. Статические страницы (SPA)

### `www/network_config/index.html` *(новый)*

Полноценный статический SPA-шаблон. **Не содержит PHP/CGI** — только HTML-разметка.

#### Структура
```
<header class="topbar">   IP-адрес, кнопка Выход
<nav class="sidebar">     навигация: Dashboard / Сеть / Время / Управление
<main class="main">
  #tab-dashboard           виджеты (CPU, RAM, Temp, Disk, Uptime, Net, RS-485, HW)
  #tab-network             формы eth0 / eth1
  #tab-time                форма timezone + datetime
  #tab-system              управление службами, лог
```

#### Авторизация
JavaScript проверяет cookie `session_token=cyntron_session` при загрузке.
Если cookie нет — немедленный редирект на `/login.html`.

### `www/network_config/login.html` *(новый)*

Статическая страница входа. Форма `POST → /cgi-bin/login.cgi`.
Если пользователь уже авторизован — автоматический редирект на `/`.

---

## 4. Страница входа — анимация огня

### Файл: `www/network_config/login.html`

Добавлена полноэкранная анимация огня на `<canvas>` за карточкой входа.

#### Алгоритм (Doom-style fire, 1993)
```
1. Canvas W×H пикселей (SCALE=3 → рендер в 3× меньшем буфере для ARM)
2. Нижняя строка постоянно = 255 (источник тепла)
3. Каждый кадр: pixel[y][x] = avg(4 соседей снизу) − random_decay
4. Палитра 256 цветов: чёрный → тёмно-красный → оранжевый → жёлтый → белый
5. requestAnimationFrame() — синхронизация с vsync браузера
```

#### Параметры
| Параметр | Значение | Описание |
|----------|----------|----------|
| `SCALE` | 3 | Масштаб пикселя (производительность на ARM) |
| `DECAY` | 1 | Скорость охлаждения |
| Палитра | 256 цветов | black→red→orange→yellow→white |

#### Тумблер управления
- Расположен: фиксированный, правый верхний угол (`position: fixed; top:18px; right:22px`)
- Иконка пламени мерцает (`@keyframes flicker`)
- Состояние сохраняется в `localStorage` (ключ `sa02m_fire`)
- По умолчанию: **включено**
- При выключении: плавное затухание (`transition: opacity .6s`)

#### Эффект карточки поверх огня
```css
background: rgba(30, 30, 30, 0.82);
backdrop-filter: blur(14px) saturate(1.4);
```

---

## 5. JavaScript — логика приложения

### Файл: `www/network_config/static/js/app.js` *(новый)*

Полная логика SPA (~500 строк), без зависимостей (vanilla JS).

#### Модули

| Функция | Описание |
|---------|----------|
| Auth guard | Проверка cookie при загрузке, редирект на `login.html` |
| `initNav()` | Переключение вкладок, ленивая загрузка конфига и лога |
| `fetchStatus()` | Polling `status.cgi` каждые 4 секунды, `fetchBusy` guard |
| `applyStatus(d)` | Рендер всех виджетов из JSON (CPU, RAM, Temp, Disk, RS-485 и др.) |
| `loadConfig()` | Загрузка текущих настроек из `config.cgi` в формы (один раз) |
| `renderRs485(ports)` | Рендер 5 карточек RS-485 с flash-анимацией при изменении TX/RX |
| `setHw(channel, val)` | POST в `hw_set.cgi`, toast-уведомление |
| `toast(msg, type)` | Временные уведомления (success/error/info) |
| `initForms()` | Обработчики форм eth0, eth1, time → `apply.cgi` |
| `validateNetForm()` | Валидация IP по regex `pattern` |
| `doRestart()`, `doReboot()` | Системные действия с подтверждением |
| `loadLog()` | Загрузка и подсветка журнала из `log.cgi` |
| `handleUrlStatus()` | Обработка `?status=applied/error_tz/...` после редиректа |

#### RS-485 активность
```javascript
// Сравниваем TX/RX с предыдущим опросом
const actNow = (p.tx !== prev.tx || p.rx !== prev.rx);
// Добавляем CSS-класс .act (синяя подсветка) на 1.8 секунды
card.classList.add('act');
card._actTimer = setTimeout(() => card.classList.remove('act'), 1800);
```

---

## 6. CGI API — новые и обновлённые эндпоинты

### `config.cgi` *(новый)*

Возвращает текущую конфигурацию системы в JSON.

**Запрос:** `GET /cgi-bin/config.cgi`

**Ответ:**
```json
{
  "eth0":     { "enabled": true,  "ip": "192.168.1.136", "netmask": "255.255.255.0", "gateway": "192.168.1.1", "dns": "77.88.8.8" },
  "eth1":     { "enabled": false, "ip": "",              "netmask": "",              "gateway": "",             "dns": "" },
  "timezone": "Europe/Moscow",
  "datetime": "2025-04-16 12:00:00"
}
```

Читает данные из `/etc/network/interfaces.d/eth{0,1}.conf` и `timedatectl`.

### `restart.cgi` *(новый, заменяет `restart_services.cgi`)*

`POST /cgi-bin/restart.cgi` → перезапуск nginx, fcgiwrap, networking, fix-eth.  
Ответ: `{"ok": true}` (JSON вместо HTML-редиректа).

### Обновлённые редиректы

| Файл | Было | Стало |
|------|------|-------|
| `login.cgi` | `Location: /cgi-bin/index.cgi` | `Location: /` |
| `logout.cgi` | `Location: /cgi-bin/index.cgi` | `Location: /login.html` |
| `apply.cgi` | `Location: index.cgi?status=…` | `Location: /?status=…` |
| `reboot.cgi` | HTML-редирект | JSON `{"ok":true}` + `sudo reboot &` |

### Очистка CGI от HTML

Все CGI теперь — **чистые API**: возвращают JSON или HTTP-редиректы.  
HTML-разметка полностью вынесена в статические файлы.

---

## 7. status.cgi — расширение метрик

### Файл: `cgi-bin/status.cgi` (полная переработка)

#### Новые поля JSON

| Поле | Источник | Описание |
|------|----------|----------|
| `load_1/5/15` | `/proc/loadavg` | Средняя нагрузка 1/5/15 мин |
| `proc_running` | `/proc/loadavg` | Процессов в состоянии R |
| `proc_total` | `/proc/loadavg` | Всего процессов |
| `cpu_freq_mhz` | `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq` | Текущая частота CPU |
| `cpu_max_mhz` | `cpuinfo_max_freq` | Максимальная частота |
| `cpu_throttle` | `freq/max * 100` | Throttle % |
| `cpu_model` | `/proc/cpuinfo` | Модель процессора |
| `swap_total/used/pct` | `/proc/meminfo` | Статистика swap |
| `temp_zones[]` | `/sys/class/thermal/thermal_zone*/` | Температуры по зонам |
| `disk_io_read/write_b` | `/sys/block/*/stat` | I/O диска (байт с загрузки) |
| `kernel` | `uname -r` | Версия ядра |
| `board` | `/proc/device-tree/model` | Модель платы (Armbian) |
| `mplc_status` | `pgrep -x mplc` | Статус процесса mplc |
| `mplc_uptime_s` | `/proc/<pid>/stat` | Время работы mplc |
| `rs485[]` | `/proc/tty/driver/*` + `/dev/RS-485-*` | Статистика RS-485 |

#### Структура RS-485 (массив 5 элементов)
```json
{
  "n": 0,
  "dev": "ttyS0",
  "st": "present",
  "open": 1,
  "tx": 12345,
  "rx": 67890,
  "fe": 0,
  "pe": 0,
  "oe": 0
}
```

**Поля ошибок:**
- `fe` — frame errors (ошибки фрейма)
- `pe` — parity errors (ошибки чётности)
- `oe` — overrun errors (переполнение буфера)

#### Определение in-use (без ethtool)
```bash
fuser "$real" >/dev/null 2>&1 && inuse=1
# Fallback: lsof если fuser недоступен
```

---

## 8. Dashboard — новые виджеты

### Виджет «Нагрузка (load avg)»
Три плитки: 1 мин / 5 мин / 15 мин + число процессов + текущая частота CPU.

### Виджет «Система»
Модель платы, CPU, версия ядра. Читается из `/proc/device-tree/model` (Armbian/Orange Pi).

### Виджет «Службы» — расширен
Добавлен mplc с временем работы (`fmtUptime(mplc_uptime_s)`).

### Виджет SWAP
Отображается в карточке RAM при `swap_total_kb > 0`.  
Прогресс-бар с градацией: оранжевый → красный при > 80%.

### Виджет «RS-485 (5 портов)»
Карточки RS-485-0..4 (ttyS0, ttyS3, ttyS4, ttyS5, ttyS7).

| Элемент | Описание |
|---------|----------|
| Цветная точка | Зелёная (открыт) / Серая (свободен) / Красная (не найден) |
| TX / RX | Накопленные байты с загрузки ОС, `fmtNum()` (К / М) |
| Активность | При изменении TX/RX — синяя подсветка границы на 1.8с |
| Ошибки | FE/PE/OE красным, только если > 0 |

### Виджет «Ethernet 1 (eth1)»
Статус линка (`up`/`down`/`absent`) + накопленные RX/TX байты.

---

## 9. Сетевой watchdog — полная переработка

### Критические ошибки в старом коде

| # | Файл | Проблема |
|---|------|----------|
| 1 | `99-lan-recovery.rules` | **Перенос строки внутри `RUN+=`** — правило никогда не срабатывало |
| 2 | `99-lan-recovery.rules` | `systemctl restart` без `--no-block` → **дедлок udev** |
| 3 | `fix-eth.sh` | Вся DHCP-логика (`dhclient`, `dhcpcd`) — **бесполезна** на устройстве со статическим IP |
| 4 | `fix-eth.sh` | `ethtool` для определения линка — пакет может не быть установлен |
| 5 | `fix-eth.service` | `TimeoutSec=10` — скрипт убивался до завершения (реальное время > 30с) |
| 6 | Весь стек | Нет постоянного watchdog — реакция только на физическое подключение кабеля |

### `fix-eth.sh` — новая логика

#### Трёхуровневая проверка здоровья
```
carrier_up()         → /sys/class/net/ethX/carrier (всегда есть, без ethtool)
has_ip()             → ip -4 addr show
check_connectivity() → пинг (см. приоритет ниже)
```

#### Приоритет выбора цели пинга
```
1. WATCHDOG_PING_ETH0=<IP>  в /etc/sa02m_network.conf  (явная настройка)
2. gateway в /etc/network/interfaces.d/eth0.conf        (стандартный шлюз)
3. Ни то, ни другое → пинг пропускается                 (LAN без маршрутизации)
4. WATCHDOG_PING_ETH1=skip  → пинг принудительно отключён
```

> **Важно:** отсутствие шлюза — нормальная ситуация для eth1 как локального интерфейса.
> В этом случае считаем интерфейс здоровым если есть carrier + IP.

#### Восстановление
```bash
ifdown "$iface"      # читает /etc/network/interfaces.d/*.conf
sleep 1
ifup "$iface"        # применяет статический IP правильно
```
Fallback если `ifdown/ifup` нет: `ip link set down/up` + ручная установка IP из конфига.

#### Защиты
- **Lock-файл** `/run/fix-eth/<iface>.lock` — предотвращает параллельный запуск
- **Cooldown 60с** — между попытками восстановления одного интерфейса
- **Ротация лога** — автоматически при > 512 КБ (сохраняет последние 200 строк)
- **Поддержка eth1** — автоматически обходит все `eth*.conf`

### `net-watchdog.sh` + `net-watchdog.service` *(новые)*

Постоянный фоновый демон.

```bash
# Каждые CHECK_INTERVAL=30 секунд:
for conf in /etc/network/interfaces.d/eth*.conf; do
    fix-eth.sh "<iface>"
done
```

```ini
[Service]
Type=simple
Restart=always        # перезапускается при падении
RestartSec=10
```

Покрывает сценарии, которые udev **не покрывает**:
- потеря IP без физического события
- зависание сетевого стека
- программный сбой после загрузки

### `fix-eth.service` — исправления

| Параметр | Было | Стало |
|----------|------|-------|
| `TimeoutSec` | `10` | `45` |
| `StartLimitBurst` | `2` | `3` |
| `StartLimitIntervalSec` | `5` | `60` |
| `RemainAfterExit` | `yes` | `no` |
| `StandardOutput` | *(нет)* | `journal` |

### `99-lan-recovery.rules` — исправления

```ini
# БЫЛО (СЛОМАНО — перенос строки в RUN+="..."):
ACTION=="add", SUBSYSTEM=="net", KERNEL=="eth0", RUN+="/usr/bin/systemctl restart 
fix-eth.service"

# СТАЛО (исправлено):
ACTION=="add", SUBSYSTEM=="net", KERNEL=="eth0", RUN+="/usr/bin/systemctl --no-block start fix-eth.service"
ACTION=="add", SUBSYSTEM=="net", KERNEL=="eth1", RUN+="/usr/bin/systemctl --no-block start fix-eth.service"
ACTION=="bind", SUBSYSTEM=="net", KERNEL=="eth0", RUN+="/usr/bin/systemctl --no-block start fix-eth.service"
ACTION=="bind", SUBSYSTEM=="net", KERNEL=="eth1", RUN+="/usr/bin/systemctl --no-block start fix-eth.service"
```

Добавлен `ACTION=="bind"` — срабатывает при привязке драйвера (например после `modprobe`), а не только при физическом подключении кабеля.

### `/etc/sa02m_network.conf` *(новый)*

Конфиг watchdog — без правки скриптов.
```bash
WATCHDOG_PING_ETH0=192.168.1.1  # переопределяет шлюз
WATCHDOG_PING_ETH1=10.0.0.2     # для eth1 без шлюза
WATCHDOG_PING_ETH1=skip         # отключить пинг для интерфейса
RECOVER_COOLDOWN=90             # изменить cooldown
```

---

## 10. Модульный установщик

### `install.sh` *(новый, заменяет `web/web`)*

```bash
sudo ./install.sh [--ip 192.168.1.136] [--port 9999] [--pass cyntron]
```

Вызывает модули последовательно:

| Модуль | Содержимое |
|--------|-----------|
| `scripts/lib.sh` | `log()`, `pkg_install()`, `svc_enable()`, `svc_restart()`, `check_root()` |
| `scripts/01-system.sh` | apt-update, пакеты, locale, timezone, пользователь hmi, serial getty off, udev RS-485/COM симлинки, mask apt timers |
| `scripts/02-network.sh` | `/etc/network/interfaces`, `eth0.conf`, деплой watchdog-скриптов и сервисов, `udevadm reload`, `net-watchdog enable` |
| `scripts/03-webserver.sh` | htpasswd, nginx конфиг, деплой `www/` → `/var/www/network_config/`, GPIO `sa02m_hw.conf`, sudoers, запуск служб |

### Параметры командной строки
```
--ip <addr>    IP-адрес eth0 (по умолчанию: 192.168.1.136)
--mask <mask>  маска подсети  (по умолчанию: 255.255.255.0)
--gw <gw>      шлюз           (по умолчанию: 192.168.1.1)
--port <port>  порт nginx     (по умолчанию: 9999)
--pass <pass>  пароль admin   (по умолчанию: cyntron)
```

---

## 11. Структура файлов

### Итоговое дерево проекта

```
СА-02м Web Interface v13.0/
│
├── install.sh                         ← sudo ./install.sh [опции]
│
├── scripts/
│   ├── lib.sh                         ← общие функции
│   ├── 01-system.sh                   ← ОС и система
│   ├── 02-network.sh                  ← сеть и watchdog
│   └── 03-webserver.sh                ← веб-сервер и деплой
│
├── etc/
│   ├── nginx/
│   │   └── network_config.conf        ← шаблон nginx
│   ├── fix-eth.sh                     ← восстановление сети (one-shot)
│   ├── fix-eth.service                ← systemd unit для udev
│   ├── net-watchdog.sh                ← постоянный мониторинг
│   ├── net-watchdog.service           ← systemd unit (daemon)
│   ├── 99-lan-recovery.rules          ← udev правила
│   ├── sa02m_hw.conf                  ← шаблон GPIO
│   └── sa02m_network.conf             ← шаблон настроек watchdog
│
└── www/
    └── network_config/
        ├── index.html                 ← SPA главная страница
        ├── login.html                 ← страница входа + анимация огня
        ├── static/
        │   ├── css/
        │   │   └── main.css           ← полная дизайн-система
        │   ├── js/
        │   │   └── app.js             ← вся логика SPA
        │   └── logo.svg
        └── cgi-bin/
            ├── status.cgi             ← метрики системы (JSON)
            ├── config.cgi             ← текущие настройки (JSON) [новый]
            ├── hw_set.cgi             ← управление GPIO (JSON)
            ├── apply.cgi              ← применить настройки сети/времени
            ├── login.cgi              ← аутентификация
            ├── logout.cgi             ← выход
            ├── restart.cgi            ← перезапуск служб (JSON) [новый]
            ├── reboot.cgi             ← перезагрузка (JSON)
            └── log.cgi                ← журнал установки (text)
```

### Деплой на устройстве

После `sudo ./install.sh` файлы размещаются:

| Исходник | Место на устройстве |
|----------|---------------------|
| `www/network_config/` | `/var/www/network_config/` |
| `etc/fix-eth.sh` | `/usr/local/bin/fix-eth.sh` |
| `etc/net-watchdog.sh` | `/usr/local/bin/net-watchdog.sh` |
| `etc/fix-eth.service` | `/etc/systemd/system/fix-eth.service` |
| `etc/net-watchdog.service` | `/etc/systemd/system/net-watchdog.service` |
| `etc/99-lan-recovery.rules` | `/etc/udev/rules.d/99-lan-recovery.rules` |
| `etc/nginx/network_config.conf` | `/etc/nginx/sites-available/network_config` |
| `etc/sa02m_hw.conf` | `/etc/sa02m_hw.conf` (если не существует) |
| `etc/sa02m_network.conf` | `/etc/sa02m_network.conf` (если не существует) |

---

*Документация сгенерирована автоматически по итогам сессии разработки. Версия 13.0, апрель 2025.*
