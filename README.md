# СА-02м — Web Interface

<p align="center">
  <img src="https://img.shields.io/badge/platform-Armbian%20%7C%20Linux%20ARM-orange?style=flat-square"/>
  <img src="https://img.shields.io/badge/stack-nginx%20%2B%20fcgiwrap%20%2B%20Bash%20CGI-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square"/>
  <img src="https://img.shields.io/badge/version-13.0-cyan?style=flat-square"/>
</p>

Веб-интерфейс для **сервера автоматизации СА-02м** на базе одноплатных компьютеров (Orange Pi, Raspberry Pi и аналоги) под управлением Armbian / Debian Linux.

---

## Содержание

- [Возможности](#возможности)
- [Скриншоты](#скриншоты)
- [Требования](#требования)
- [Быстрый старт](#быстрый-старт)
- [Параметры установки](#параметры-установки)
- [Структура проекта](#структура-проекта)
- [Описание компонентов](#описание-компонентов)
  - [Dashboard](#dashboard)
  - [Настройки сети](#настройки-сети)
  - [Управление железом (GPIO)](#управление-железом-gpio)
  - [RS-485 интерфейсы](#rs-485-интерфейсы)
  - [Страница входа](#страница-входа)
- [CGI API](#cgi-api)
- [Конфигурация GPIO](#конфигурация-gpio)
- [Сетевой watchdog](#сетевой-watchdog)
- [Структура файлов на устройстве](#структура-файлов-на-устройстве)
- [Обновление](#обновление)

---

## Возможности

### Мониторинг (Dashboard)
- **CPU** — загрузка с историей, модель, частота (с throttle-индикатором), load averages 1/5/15 мин
- **RAM + Swap** — использование памяти, прогресс-бары
- **Температура** — по всем thermal-зонам (zone0, zone1...)
- **Диск** — объём, использование, I/O (read/write байт с загрузки)
- **Uptime** — время работы системы
- **Сеть** — состояние eth0/eth1, RX/TX байт
- **Модель платы** — из `/proc/device-tree/model` (Armbian/Orange Pi)
- **Ядро** — версия Linux
- **Службы** — nginx, fcgiwrap, mplc (со временем работы)
- **RS-485 (5 портов)** — TX/RX, ошибки (FE/PE/OE), индикатор активности
- **Дискретный выход (DO)** — отображение и управление
- **Beeper** — управление пищалкой
- **Аварийный LED** — управление красным светодиодом

### Настройки
- Два Ethernet-интерфейса (eth0, eth1) — статические IP, маска, шлюз, DNS
- Часовой пояс и дата/время
- Перезапуск служб / перезагрузка устройства

### Безопасность
- HTTP Basic Auth + сессионный cookie
- Страница входа с анимацией огня (Doom-style fire)
- sudoers с минимально необходимыми правами для www-data

### Сетевой watchdog
- Двухуровневая защита: udev (реакция на события) + постоянный демон (каждые 30 с)
- Корректная работа без шлюза (eth1 как изолированный LAN)
- Настраиваемые цели пинга и cooldown через `/etc/sa02m_network.conf`

---

## Скриншоты

> _Тёмная тема с циановыми акцентами, вдохновлённая [mongoose.ws](https://mongoose.ws)._

```
┌─────────────────────────────────────────────────────────────┐
│  СА-02м         Dashboard  Сеть  Время  Управление   Выход │
├──────────┬──────────────────────────────────────────────────┤
│          │  CPU 12%   RAM 34%   Temp 52°C   Disk 18%        │
│          │  ─────────────────────────────────────────────   │
│ Dashboard│  Load  1m:0.14  5m:0.08  15m:0.05  | 48 proc    │
│ Сеть     │  eth0  192.168.1.136  UP  ↑12.4M ↓2.1M          │
│ Время    │  RS-485-0 ████ TX:12345 RX:67890  [ACTIVE]       │
│ Управл.  │  DO: ○  Beeper: ○  Alarm: ○                      │
└──────────┴──────────────────────────────────────────────────┘
```

---

## Требования

| Компонент | Версия |
|-----------|--------|
| ОС | Armbian / Debian / Ubuntu (ARM или x86) |
| nginx | ≥ 1.14 |
| fcgiwrap | ≥ 1.1 |
| bash | ≥ 4.x |
| openssl | любая |
| net-tools | любая |
| psmisc (`fuser`) | любая |

Все зависимости устанавливаются автоматически через `apt`.

---

## Быстрый старт

```bash
# Клонировать репозиторий
git clone https://github.com/CYNTRON-git/web.git
cd web

# Запустить установку с параметрами по умолчанию
sudo ./install.sh
```

После установки веб-интерфейс доступен по адресу:
```
http://<IP-устройства>:9999
```

**Логин:** `admin`  
**Пароль:** `cyntron` (задаётся параметром `--pass`)

---

## Параметры установки

```bash
sudo ./install.sh [ПАРАМЕТРЫ]

  --ip   <addr>    IP-адрес eth0              (по умолчанию: 192.168.1.136)
  --mask <mask>    Маска подсети               (по умолчанию: 255.255.255.0)
  --gw   <gw>      Шлюз по умолчанию          (по умолчанию: 192.168.1.1)
  --port <port>    Порт nginx                  (по умолчанию: 9999)
  --pass <pass>    Пароль пользователя admin   (по умолчанию: cyntron)
```

### Примеры

```bash
# Задать IP и пароль
sudo ./install.sh --ip 10.0.0.5 --gw 10.0.0.1 --pass MyPass123

# Другой порт, интерфейс без шлюза
sudo ./install.sh --ip 172.16.0.1 --mask 255.255.0.0 --gw "" --port 80

# Только обновить веб-файлы (если уже установлено)
sudo ./install.sh
```

---

## Структура проекта

```
web/
│
├── install.sh                    ← главный скрипт установки
│
├── scripts/
│   ├── lib.sh                    ← общие функции (log, pkg_install, svc_enable)
│   ├── 01-system.sh              ← система: пакеты, locale, udev, RS-485 симлинки
│   ├── 02-network.sh             ← сеть: eth0/1, watchdog, udev правила
│   └── 03-webserver.sh           ← nginx, fcgiwrap, sudoers, деплой www/
│
├── etc/
│   ├── nginx/
│   │   └── network_config.conf   ← шаблон nginx (токены __PORT__, __WEB_ROOT__)
│   ├── fix-eth.sh                ← скрипт восстановления интерфейса
│   ├── fix-eth.service           ← systemd unit (oneshot, запуск udev)
│   ├── net-watchdog.sh           ← демон мониторинга сети
│   ├── net-watchdog.service      ← systemd unit (Restart=always)
│   ├── 99-lan-recovery.rules     ← udev правила (eth0/eth1, add/bind)
│   ├── sa02m_hw.conf             ← шаблон GPIO-пинов
│   └── sa02m_network.conf        ← шаблон настроек watchdog
│
└── www/
    └── network_config/
        ├── index.html            ← SPA-шаблон (Dashboard, Сеть, Время, Управление)
        ├── login.html            ← страница входа + анимация огня
        ├── static/
        │   ├── css/main.css      ← дизайн-система (тёмная тема, анимации)
        │   └── js/app.js         ← вся логика SPA (vanilla JS, без фреймворков)
        └── cgi-bin/
            ├── status.cgi        ← GET /cgi-bin/status.cgi  → JSON метрики
            ├── config.cgi        ← GET /cgi-bin/config.cgi  → JSON настройки
            ├── hw_set.cgi        ← POST /cgi-bin/hw_set.cgi → управление GPIO
            ├── apply.cgi         ← POST /cgi-bin/apply.cgi  → сохранить сеть/время
            ├── login.cgi         ← POST /cgi-bin/login.cgi  → аутентификация
            ├── logout.cgi        ← GET  /cgi-bin/logout.cgi → выход
            ├── restart.cgi       ← POST /cgi-bin/restart.cgi → перезапуск служб
            ├── reboot.cgi        ← POST /cgi-bin/reboot.cgi → перезагрузка
            └── log.cgi           ← GET  /cgi-bin/log.cgi    → журнал установки
```

---

## Описание компонентов

### Dashboard

Автоматически обновляется каждые **4 секунды** через `GET /cgi-bin/status.cgi`.

#### Виджет CPU
- Процент загрузки (SVG-дуга с плавной анимацией)
- Текущая и максимальная частота (из `/sys/devices/system/cpu/cpu0/cpufreq/`)
- Throttle-индикатор: красный при throttle > 10%
- Load averages: 1 / 5 / 15 минут + число процессов

#### Виджет RAM
- Использование RAM в МБ / МБ (%)
- Прогресс-бар: зелёный → жёлтый → красный
- Мини-bar SWAP (отображается при swap > 0)

#### Виджет Температура
- Максимальная из всех thermal-зон
- Цвет: синий < 50°C, жёлтый < 70°C, красный ≥ 70°C

#### Виджет RS-485
5 карточек: **RS-485-0** ... **RS-485-4** (ttyS0, ttyS3, ttyS4, ttyS5, ttyS7)

| Элемент | Описание |
|---------|----------|
| Цветная точка | 🟢 порт открыт / ⚪ свободен / 🔴 не найден |
| TX / RX | Накопленные байты, автоформат (К / М / Г) |
| Активность | При изменении TX/RX — синяя подсветка на 1.8 с |
| Ошибки | FE / PE / OE — отображаются красным при > 0 |

#### Виджет Hardware Outputs
Три переключателя: **DO** (дискретный выход), **Beeper**, **Alarm LED**.  
Состояние читается из `/sys/class/gpio/gpioN/value`, управление через `POST /cgi-bin/hw_set.cgi`.

---

### Настройки сети

Форма для **eth0** и **eth1**:
- Включить/отключить интерфейс
- IP-адрес, маска подсети, шлюз, DNS
- Валидация IP прямо в браузере
- После сохранения: автоматический `ifdown` / `ifup`

Настройки записываются в:
- `/etc/network/interfaces.d/eth0.conf`
- `/etc/network/interfaces.d/eth1.conf`

---

### Управление железом (GPIO)

GPIO-пины настраиваются в `/etc/sa02m_hw.conf`:

```bash
SA02M_GPIO_DO=78          # дискретный выход
SA02M_GPIO_BEEPER=79      # пищалка
SA02M_GPIO_ALARM_LED=80   # аварийный LED
```

**API управления:**

```http
POST /cgi-bin/hw_set.cgi
Content-Type: application/x-www-form-urlencoded

channel=DO&value=1
```

Ответ: `{"ok": true, "channel": "DO", "value": 1}`

`www-data` имеет право писать в `/sys/class/gpio/` через `sudoers` без пароля.

---

### RS-485 интерфейсы

Пять интерфейсов доступны по симлинкам:

| Имя | Устройство | Описание |
|-----|-----------|----------|
| RS-485-0 | `/dev/ttyS0` | Первый порт |
| RS-485-1 | `/dev/ttyS3` | Второй порт |
| RS-485-2 | `/dev/ttyS4` | Третий порт |
| RS-485-3 | `/dev/ttyS5` | Четвёртый порт |
| RS-485-4 | `/dev/ttyS7` | Пятый порт |

Симлинки создаются в `/dev/RS-485-N` через udev-правила.  
Статистика читается из `/proc/tty/driver/serial`.

---

### Страница входа

Полноэкранная анимация огня (алгоритм **Doom 1993 fire**) на `<canvas>`.

- Тумблер в правом верхнем углу — включить/выключить анимацию
- Состояние сохраняется в `localStorage` (ключ `sa02m_fire`)
- По умолчанию: **включено**
- Эффект на карточке входа: `backdrop-filter: blur(14px)` — стеклянный эффект

Параметры алгоритма: SCALE=3 (оптимизация для ARM), DECAY=1, палитра 256 цветов.

---

## CGI API

Все CGI-скрипты возвращают **JSON** (без HTML). Аутентификация через cookie `session_token`.

### `GET /cgi-bin/status.cgi`

<details>
<summary>Пример ответа</summary>

```json
{
  "cpu_pct": 12,
  "mem_total_kb": 2048000,
  "mem_used_kb": 698000,
  "mem_pct": 34,
  "temp_c": 52,
  "disk_total_kb": 30000000,
  "disk_used_kb": 5400000,
  "disk_pct": 18,
  "uptime_s": 86400,
  "eth0_up": true,
  "eth0_ip": "192.168.1.136",
  "eth0_rx_b": 12400000,
  "eth0_tx_b": 2100000,
  "eth1_up": false,
  "load_1": 0.14,
  "load_5": 0.08,
  "load_15": 0.05,
  "proc_running": 1,
  "proc_total": 48,
  "cpu_freq_mhz": 1200,
  "cpu_max_mhz": 1800,
  "cpu_throttle": 0,
  "cpu_model": "Cortex-A7",
  "board": "Orange Pi Zero 2",
  "kernel": "5.15.93-sunxi64",
  "swap_total_kb": 1048576,
  "swap_used_kb": 0,
  "swap_pct": 0,
  "disk_io_read_b": 5242880,
  "disk_io_write_b": 1048576,
  "mplc_status": "running",
  "mplc_uptime_s": 3600,
  "temp_zones": [52, 48],
  "do_state": 0,
  "beeper_state": 0,
  "alarm_led_state": 0,
  "rs485": [
    { "n": 0, "dev": "ttyS0", "st": "present", "open": 1, "tx": 12345, "rx": 67890, "fe": 0, "pe": 0, "oe": 0 },
    { "n": 1, "dev": "ttyS3", "st": "present", "open": 0, "tx": 0, "rx": 0, "fe": 0, "pe": 0, "oe": 0 },
    { "n": 2, "dev": "ttyS4", "st": "absent",  "open": 0, "tx": 0, "rx": 0, "fe": 0, "pe": 0, "oe": 0 },
    { "n": 3, "dev": "ttyS5", "st": "present", "open": 0, "tx": 0, "rx": 0, "fe": 0, "pe": 0, "oe": 0 },
    { "n": 4, "dev": "ttyS7", "st": "present", "open": 0, "tx": 0, "rx": 0, "fe": 0, "pe": 0, "oe": 0 }
  ]
}
```

</details>

### `GET /cgi-bin/config.cgi`

```json
{
  "eth0":     { "enabled": true,  "ip": "192.168.1.136", "netmask": "255.255.255.0", "gateway": "192.168.1.1", "dns": "77.88.8.8" },
  "eth1":     { "enabled": false, "ip": "", "netmask": "", "gateway": "", "dns": "" },
  "timezone": "Europe/Moscow",
  "datetime": "2025-04-16 12:00:00"
}
```

### `POST /cgi-bin/apply.cgi`

```
eth0_ip=192.168.1.136&eth0_mask=255.255.255.0&eth0_gw=192.168.1.1&...
```

Ответ: HTTP `302 Location: /?status=applied` или `/?status=error_...`

### `POST /cgi-bin/hw_set.cgi`

```
channel=DO&value=1        → {"ok": true, "channel": "DO", "value": 1}
channel=BEEPER&value=0    → {"ok": true}
channel=ALARM_LED&value=1 → {"ok": true}
```

### `POST /cgi-bin/restart.cgi`

Перезапускает: `nginx`, `fcgiwrap`, `networking`, `fix-eth`.  
Ответ: `{"ok": true}`

### `POST /cgi-bin/reboot.cgi`

```json
{"ok": true}
```
Устройство перезагружается через 2 секунды.

---

## Конфигурация GPIO

Отредактируйте `/etc/sa02m_hw.conf` на устройстве:

```bash
# Номера GPIO-пинов (sysfs)
SA02M_GPIO_DO=78
SA02M_GPIO_BEEPER=79
SA02M_GPIO_ALARM_LED=80
```

Для определения правильного номера GPIO:
```bash
# Найти имя пина
cat /sys/kernel/debug/pinctrl/*/pins | grep -i "PH14"

# Формула для Allwinner: base + offset
# Пример: PH14 = 7*32 + 14 = 238
```

---

## Сетевой watchdog

### Архитектура

```
Физическое событие (кабель)
    │
    ▼
udev (99-lan-recovery.rules)         ─ реактивная защита
    │  --no-block
    ▼
fix-eth.service  ──→  fix-eth.sh     ─ восстановление
                           │
                       /sys/class/net/ethX/carrier
                       ip -4 addr show
                       ping (шлюз / custom / skip)
                           │
                       ifdown / ifup

net-watchdog.service ──→ net-watchdog.sh  ─ активная защита (каждые 30 с)
    │  (Restart=always)       │
    └──────────────────────────── вызывает fix-eth.sh для каждого iface
```

### Настройка `/etc/sa02m_network.conf`

```bash
# Пинговать конкретный хост вместо шлюза для eth0
WATCHDOG_PING_ETH0=192.168.1.1

# eth1 без шлюза — отключить пинг (считать здоровым при наличии carrier + IP)
WATCHDOG_PING_ETH1=skip

# Cooldown между попытками восстановления (секунды, по умолчанию 60)
RECOVER_COOLDOWN=90
```

### Логи watchdog

```bash
# Журнал fix-eth
journalctl -u fix-eth.service -f

# Журнал постоянного мониторинга
journalctl -u net-watchdog.service -f

# Файловый лог
tail -f /var/log/fix-eth.log
```

---

## Структура файлов на устройстве

После установки:

| Файл | Путь |
|------|------|
| Веб-файлы | `/var/www/network_config/` |
| fix-eth.sh | `/usr/local/bin/fix-eth.sh` |
| net-watchdog.sh | `/usr/local/bin/net-watchdog.sh` |
| fix-eth.service | `/etc/systemd/system/fix-eth.service` |
| net-watchdog.service | `/etc/systemd/system/net-watchdog.service` |
| udev правила | `/etc/udev/rules.d/99-lan-recovery.rules` |
| nginx конфиг | `/etc/nginx/sites-available/network_config` |
| GPIO конфиг | `/etc/sa02m_hw.conf` |
| Watchdog конфиг | `/etc/sa02m_network.conf` |
| Пароль nginx | `/etc/nginx/.htpasswd` |
| Sudoers | `/etc/sudoers.d/sa02m-www` |
| Журнал установки | `/var/log/sa02m_install.log` |

---

## Обновление

```bash
# Обновить только веб-файлы без переконфигурации системы
git pull
sudo cp -r www/network_config/* /var/www/network_config/
sudo chmod +x /var/www/network_config/cgi-bin/*.cgi
sudo systemctl reload nginx

# Полная переустановка
git pull
sudo ./install.sh --ip <IP> --pass <PASS>
```

---

## Лицензия

MIT © CYNTRON
