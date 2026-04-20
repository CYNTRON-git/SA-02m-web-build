# СА-02м Web Interface — Журнал изменений

**Версия 1.0.3** | Апрель 2026  
Платформа: Armbian Linux (ARM) · nginx + fcgiwrap · Bash CGI + Python-демон `sa02m-flasher`

---

## 1.0.3 — Устройства MR-02м (RS-485 / Modbus RTU / прошивка)

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
- Координация с опросом RS-485: на время сканирования/прошивки демон
  останавливает службу `mplc.service` (список настраивается в
  `/etc/sa02m_flasher.conf`, ключ `MPLC_STOP_SERVICES`) и гарантированно
  запускает её обратно (в том числе `ExecStopPost`).
- Эксклюзивный захват порта через `flock` на `/var/lock/sa02m-flasher-<port>.lock`
  и предварительная проверка `fuser` — исключает конфликт двух операций.

### Архитектура

- **Backend:** Python 3 демон `sa02m-flasher` (systemd unit
  `/etc/systemd/system/sa02m-flasher.service`). HTTP-API на stdlib
  `http.server.ThreadingHTTPServer` поверх unix-сокета
  `/run/sa02m-flasher.sock`. События (прогресс, лог, найденные устройства)
  стримятся в UI через Server-Sent Events.
- **Библиотека Modbus/flash:** перенос из референсного проекта
  `MR-02m-flasher/flasher_windows` (модули `modbus_rtu.py`, `modbus_io.py`,
  `serial_port.py`, `scanner.py`, `flash_protocol.py`, `firmware.py`,
  `serial_ranges.py`, `module_profiles.py`, `flasher_log.py`,
  `modbus_tcp.py`). Копируются как есть, без GUI-кода.
- **Nginx:** новые location-блоки `/_auth_check` (внутренняя авторизация
  через cookie `session_token`) и `/api/flasher/*` → `proxy_pass`
  `http://unix:/run/sa02m-flasher.sock`. SSE-эндпоинт выделен отдельно с
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
  `PrivateTmp`, `NoNewPrivileges`, `ReadWritePaths`).
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
