# СА-02м — Web Interface

<p align="center">
  <img src="https://img.shields.io/badge/platform-Armbian%20%7C%20Linux%20ARM-orange?style=flat-square"/>
  <img src="https://img.shields.io/badge/stack-nginx%20%2B%20fcgiwrap%20%2B%20Bash%20CGI-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square"/>
  <img src="https://img.shields.io/badge/version-13.0-cyan?style=flat-square"/>
</p>

Веб-интерфейс для **[сервера автоматизации СА-02м](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/servery_avtomatizatsii/)** производства [ЦИНТРОН](https://cyntron.ru) на базе процессорного модуля [A40i-2eth](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/komplektuyushchie/7705/) (Allwinner A40i, Linux).

| Устройство | Описание | Ссылка |
|-----------|----------|--------|
| **СА-02м** | 5×RS-485, DO, uSD, USB, RTC, 1×Eth | [cyntron.ru](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/servery_avtomatizatsii/) |
| **СА-02м-2** | 4×RS-485, uSD, USB, RTC, 2×Eth | [cyntron.ru](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/servery_avtomatizatsii/) |
| **A40i-2eth** | Процессорный модуль (SoM), производство Россия | [cyntron.ru](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/komplektuyushchie/7705/) |

---

## Содержание

- [Возможности](#возможности)
- [Скриншоты](#скриншоты)
- [Требования](#требования)
- [Установка на СА-02м](#установка-на-са-02м)
  - [Способ 1 — через интернет (устройство онлайн)](#способ-1--через-интернет-устройство-онлайн)
  - [Способ 2 — без интернета (перенос с ПК)](#способ-2--без-интернета-перенос-с-пк)
  - [Запуск установщика](#запуск-установщика)
  - [Проверка установки](#проверка-установки)
  - [Первый вход](#первый-вход)
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
- [Сборка образа для СА-02м](#сборка-образа-для-са-02м)
  - [Аппаратная платформа](#аппаратная-платформа)
  - [Виртуальная машина для сборки](#виртуальная-машина-для-сборки)
  - [Процесс сборки Buildroot](#процесс-сборки-buildroot)
  - [Прошивка образа на eMMC](#прошивка-образа-на-emmc)
  - [Первоначальная настройка системы](#первоначальная-настройка-системы)
  - [Отключение UART-консоли](#отключение-uart-консоли)
  - [RS-485 и COM симлинки](#rs-485-и-com-симлинки)
  - [GPIO и периферия](#gpio-и-периферия)
  - [RTC (часы реального времени)](#rtc-часы-реального-времени)
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

## Установка на СА-02м

> СА-02м — программируемый контроллер на базе одноплатного компьютера под управлением Linux. Устройство, как правило, **не имеет доступа в интернет**, поэтому установка выполняется переносом файлов с ПК.

### Доступы по умолчанию

| | Логин | Пароль | Адрес |
|--|-------|--------|-------|
| **SSH** (PuTTY, терминал) | `root` | `cyntron` | `192.168.1.136`, порт `22` |
| **Веб-интерфейс** (браузер) | `admin` | `cyntron` | `http://192.168.1.136:9999` |

---

### Шаг 1 — Скачайте файлы на ПК

Скачайте ZIP-архив репозитория с GitHub:  
**[Code → Download ZIP](https://github.com/CYNTRON-git/web/archive/refs/heads/main.zip)**

Или клонируйте через git:
```bash
git clone https://github.com/CYNTRON-git/web.git
```

Распакуйте архив. Должна получиться папка `web` (или `web-main`) с файлами `install.sh`, `scripts/`, `etc/`, `www/`.

---

### Шаг 2 — Подключитесь по SSH (PuTTY)

1. Запустите **PuTTY** (или Windows Terminal, MobaXterm и т.п.)
2. Введите:
   - **Host Name:** `192.168.1.136`
   - **Port:** `22`
   - **Connection type:** SSH
3. Нажмите **Open**
4. При запросе логина введите: `root`
5. При запросе пароля введите: `cyntron`

> Если адрес устройства другой — уточните его, нажав кнопку **Reset** на корпусе или посмотрев через роутер.

---

### Шаг 3 — Скопируйте файлы через WinSCP

1. Запустите **[WinSCP](https://winscp.net/)** (бесплатно)
2. Создайте новое соединение:
   - **File protocol:** SFTP
   - **Host name:** `192.168.1.136`
   - **Port:** `22`
   - **User name:** `root`
   - **Password:** `cyntron`
3. Нажмите **Login**
4. В левой панели (ПК) откройте папку с распакованным репозиторием
5. В правой панели (устройство) перейдите в `/tmp`
6. Скопируйте папку `web` (или `web-main`) в `/tmp` на устройстве

После копирования на устройстве должна существовать папка `/tmp/web/` с файлами `install.sh`, `scripts/`, `etc/`, `www/`.

> **Альтернатива WinSCP** — через PowerShell:
> ```powershell
> scp -r .\web root@192.168.1.136:/tmp/web
> ```

---

### Шаг 4 — Запустите установщик через SSH

В окне PuTTY выполните последовательно:

```bash
# 1. Перейти в папку с установщиком
cd /tmp/web

# 2. Сделать скрипты исполняемыми
chmod +x install.sh scripts/*.sh etc/*.sh

# 3. Установить необходимые пакеты (требуется интернет или локальный apt)
apt-get install -y nginx fcgiwrap openssl net-tools psmisc

# 4. Запустить установщик
# Укажите нужный IP-адрес устройства, шлюз и пароль для веб-интерфейса
./install.sh --ip 192.168.1.136 --mask 255.255.255.0 --gw 192.168.1.1 --pass cyntron
```

> Если устройство **без интернета** — пропустите шаг `apt-get`. Пакеты должны быть установлены заранее (например, из базового образа). Установщик проверит их наличие.

Установщик автоматически выполняет:

| Шаг | Что происходит |
|-----|----------------|
| `01-system.sh` | Установка пакетов, настройка locale, udev-симлинки RS-485 |
| `02-network.sh` | Конфигурация eth0, деплой сетевого watchdog |
| `03-webserver.sh` | Настройка nginx + fcgiwrap, деплой веб-файлов, sudoers |

Процесс занимает **1–3 минуты**. По окончании в терминале появится:

```
════════════════════════════════════════
 Установка завершена!
 URL  : http://192.168.1.136:9999
 Логин: admin / cyntron
════════════════════════════════════════
 ✓ nginx работает
 ✓ fcgiwrap работает
```

Журнал установки сохраняется в `/var/log/sa02m_install.log`.

---

### Шаг 5 — Откройте веб-интерфейс

1. Откройте браузер на ПК
2. Перейдите: `http://192.168.1.136:9999`
3. Введите логин `admin`, пароль `cyntron`

---

### Способ через интернет (если устройство онлайн)

Если на СА-02м временно есть доступ в интернет — можно обойтись без WinSCP:

```bash
# В SSH-сессии на устройстве:
apt-get install -y git
git clone https://github.com/CYNTRON-git/web.git /tmp/web
cd /tmp/web
chmod +x install.sh scripts/*.sh etc/*.sh
./install.sh --ip 192.168.1.136 --pass cyntron
```

---

### Проверка установки

В SSH-терминале (PuTTY):

```bash
# Статус служб
systemctl status nginx fcgiwrap net-watchdog

# Проверить что nginx слушает нужный порт
ss -tlnp | grep nginx

# Проверить доступность локально
curl -s http://127.0.0.1:9999/login.html | grep -o '<title>.*</title>'

# Посмотреть журнал установки
tail -50 /var/log/sa02m_install.log
```

> **Если браузер не открывает страницу** — убедитесь, что ПК в той же подсети (`192.168.1.x`) и порт `9999` не заблокирован брандмауэром.

---

### Смена пароля веб-интерфейса

```bash
# В SSH на устройстве:
htpasswd /etc/nginx/.htpasswd admin
# Введите новый пароль дважды
systemctl reload nginx
```

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

## Сборка образа для СА-02м

Этот раздел описывает процесс сборки собственного образа Linux для одноплатного компьютера СА-02м на базе **Allwinner A40i** (Starterkit SK-A40i-NANO-2E).

---

### Аппаратная платформа

В основе СА-02м лежит процессорный модуль **[A40i-2eth](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/komplektuyushchie/7705/)** (ЦИНТРОН) на базе SoM **[SK-A40i-NANO-2E](http://starterkit.ru/html/index.php?name=shop&op=view&id=178)** (Starterkit). Производство — Россия.

| Параметр | СА-02м (1eth) | СА-02м-2 (2eth) |
|----------|--------------|----------------|
| Процессор | [Allwinner A40i](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/komplektuyushchie/7705/) — 4× ARM Cortex-A7, 1200 МГц | ← то же |
| Плата | [A40i-2eth](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/komplektuyushchie/7705/) / [SK-A40i-NANO-2E](http://starterkit.ru/html/index.php?name=shop&op=view&id=178), 30×51×4 мм | ← то же |
| ОЗУ | 512 МБ DDR3-1200 | ← то же |
| Хранилище | eMMC 8 ГБ (`/dev/mmcblk2`) | ← то же |
| Ethernet | 1× 100/10M (EMAC, eth0) | **2×** 100/10M (EMAC eth0 + GMAC eth1) |
| USB | 2× USB-host | ← то же |
| RS-485 / COM | **5** портов (ttyS0, ttyS3, ttyS4, ttyS5, ttyS7) | **4** порта (ttyS3, ttyS4, ttyS5, ttyS7) |
| Интерфейсы | CAN, UART, SPI, I2C, PWM, GPIO | ← то же |
| DO / Beeper / LED | **есть** (PCA9536 I2C) | Beeper + LED (**без DO**) |
| RTC | PCF8563 (I2C3, адрес `0x51`) | ← то же |
| GPIO расширитель | PCA9536 (I2C шина 2, адрес `0x41`) | ← то же |
| Питание | 5 В | ← то же |
| Температура | −40 … +85 °C (индустриальный диапазон) | ← то же |
| DTS compatible | `"sk,a40i-nano-2e"`, `"allwinner,sun8i-r40"` | ← то же |


> Купить модуль A40i-2eth: [cyntron.ru](https://cyntron.ru/catalog/ustroystva_avtomatizatsii/komplektuyushchie/7705/) · Документация и схема: [starterkit.ru](http://starterkit.ru/html/index.php?name=shop&op=view&id=178)

---

### Виртуальная машина для сборки

Для сборки образа предоставляется готовая виртуальная машина Linux с установленным Buildroot и всеми зависимостями.

**Скачать VM и материалы:**  
📦 **[https://disk.yandex.ru/d/wtRZcuZ-m1xOuA](https://disk.yandex.ru/d/wtRZcuZ-m1xOuA)**

Содержимое архива:
- Виртуальная машина для VirtualBox / VMware
- Buildroot `buildroot-2022.08.4-sk-a40i` с патчами для SK-A40i
- Готовые конфигурационные файлы (`defconfig`)
- Документация по плате SK-A40i-NANO (PDF)
- Готовые бинарные образы (для записи без сборки)

**Логин в VM:** `root` / `root`

> **Важно:** Сборка занимает **несколько часов** при первом запуске (компилируется toolchain, ядро, u-boot). Последующие пересборки — значительно быстрее.

---

### Процесс сборки Buildroot

#### 1. Запустите VM и откройте терминал

```bash
cd /home/user/src/buildroot-2022.08.4-sk-a40i
```

#### 2. Выберите конфигурацию

Доступны два варианта сборки:

| Конфигурация | Описание |
|-------------|---------|
| `sk_min_defconfig` | Минимальная файловая система, только базовые пакеты |
| `sk_qt5_defconfig` | Расширенная сборка с Qt5, стилями и сервисами |

```bash
# Очистить предыдущую сборку (при смене конфигурации)
make clean

# Загрузить нужный defconfig
make sk_min_defconfig
```

#### 3. Настройте параметры сборки

```bash
make menuconfig
```

В меню:
- **Target options** → выбрать плату: `Bootloaders → Starterkit A40i board → sk-a40i-nano-2e`
- **Filesystem images** → `exact size` установить нужный размер образа (по умолчанию 512 МБ)

Дополнительные опции:

```bash
# Конфигурация ядра Linux
make linux-menuconfig

# Конфигурация U-Boot
make uboot-menuconfig

# Конфигурация Busybox
make busybox-menuconfig
```

#### 4. Запустите сборку

```bash
make
```

#### 5. Результат сборки

После завершения файлы образа находятся в `output/images/`:

| Файл | Описание |
|------|----------|
| `sdcard.img` | Готовый образ для записи на eMMC (весь диск) |
| `zImage` | Ядро Linux |
| `sun8i-a40i-sk.dtb` / `sun8i-a40i-nano2e-none-sk.dtb` | Device Tree Blob |
| `u-boot-sunxi-with-spl.bin` | U-Boot с SPL |
| `boot.scr` | Скрипт загрузки U-Boot |
| `rootfs.ext4` | Корневая файловая система |

#### Полезные команды Buildroot

```bash
make                          # полная сборка системы
make linux-rebuild            # принудительная пересборка ядра
make uboot-rebuild            # принудительная пересборка U-Boot
make busybox-rebuild          # принудительная пересборка Busybox
make host-uboot-tools-rebuild # пересборка mkimage (нужно для boot.scr)
make <package>-rebuild        # пересборка любого пакета
```

> **Предупреждение:** `make clean` удаляет всё содержимое `output/`. Перед очисткой сохраните нужные конфигурации.

---

### Прошивка образа на eMMC

#### Способ 1 — запись образа через SD-карту (FEL/загрузчик)

Подходит для первоначального программирования через USB-OTG.

1. Скачайте `sdcard.img` из папки `output/images/`
2. Запишите на SD-карту с помощью [balenaEtcher](https://www.balena.io/etcher/) или `dd`:

```bash
# Linux
sudo dd if=sdcard.img of=/dev/sdX bs=4M status=progress && sync

# Windows (через balenaEtcher или ImageUSB)
```

3. Вставьте SD-карту в устройство, загрузитесь с неё
4. Скопируйте образ eMMC:

```bash
dd if=/mnt/sdcard.img of=/dev/mmcblk2 bs=1M && sync
```

5. Извлеките SD-карту и перезагрузитесь с eMMC.

#### Способ 2 — обновление только U-Boot (по сети)

Если система уже запущена и нужно обновить только загрузчик:

```bash
# На ПК — скопировать U-Boot на устройство
scp output/images/u-boot-sunxi-with-spl.bin root@192.168.1.136:/root/

# На устройстве — записать U-Boot в начало eMMC (seek=1 = 8KB offset)
ssh root@192.168.1.136 "dd if=/root/u-boot-sunxi-with-spl.bin of=/dev/mmcblk2 bs=8k seek=1 && sync"

# Перезагрузить
ssh root@192.168.1.136 reboot
```

#### Способ 3 — обновление ядра и DTB (по сети)

```bash
# Скопировать ядро и DTB на устройство
scp output/images/zImage root@192.168.1.136:/boot/
scp output/images/sun8i-a40i-nano2e-none-sk.dtb root@192.168.1.136:/boot/dtb/

ssh root@192.168.1.136 reboot
```

---

### Первоначальная настройка системы

После первой загрузки выполните следующие шаги (по SSH или через последовательный порт).

**Доступ по умолчанию:** `root` / `cyntron` (SSH, порт 22)

#### Задать hostname

```bash
hostnamectl set-hostname SA-02
```

#### Обновить систему (при наличии интернета)

```bash
apt-get update && apt-get -y upgrade
```

#### Установить полезные утилиты

```bash
apt-get install -y mc net-tools psmisc i2c-tools
```

#### Настроить статический IP (eth0)

```bash
cat > /etc/network/interfaces.d/eth0.conf << 'EOF'
auto eth0
allow-hotplug eth0
iface eth0 inet static
    address 192.168.1.136
    netmask 255.255.255.0
    gateway 192.168.1.1
    dns-nameservers 77.88.8.8 77.88.8.1
EOF

ifdown eth0 && ifup eth0
```

#### Настройка MAC-адреса (если нужен фиксированный)

```bash
# Через nmcli
nmcli connection modify "Wired connection 1" ethernet.cloned-mac-address 02:53:8B:00:D4:30

# Или в /etc/network/interfaces.d/eth0.conf добавить:
# hwaddress ether 02:53:8B:00:D4:30
```

#### Редактирование Device Tree (DTS → DTB)

```bash
# Декомпиляция DTB в DTS для редактирования
dtc -I dtb -O dts /boot/dtb/sun8i-a40i-nano2e-none-sk.dtb \
    -o /boot/dtb/sun8i-a40i-nano2e-none-sk.dts

# После редактирования — компиляция обратно
dtc -I dts -O dtb /boot/dtb/sun8i-a40i-nano2e-none-sk.dts \
    -o /boot/dtb/sun8i-a40i-nano2e-none-sk.dtb
```

---

### Отключение UART-консоли

По умолчанию ttyS0 занят консолью загрузчика и ядра. Для освобождения порта под RS-485:

#### Отключение getty на ttyS0

```bash
rm /etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service
systemctl daemon-reload
```

#### Отключение консоли в U-Boot (через Buildroot)

```bash
make uboot-menuconfig
```

Отключить опции:

```
SPL / TPL --->
  [ ] Support serial          ← снять

Device Drivers --->
  [*] Serial --->
    [ ] Require a serial port for console
    [ ] Provide a serial driver
    [ ] Provide a serial driver in SPL
```

```bash
make uboot-rebuild
make
```

#### Отключение консоли в ядре (через DTS)

В файле `output/build/linux-custom/arch/arm/boot/dts/sun8i-a40i-nano2e-none-sk.dts`:

```dts
chosen {
    /* убрать: stdout-path = "serial0:115200n8"; */
};
```

```bash
make linux-rebuild
make
```

#### Отключение консоли в скрипте загрузки U-Boot

В файле `board/starterkit/sk-a40i-sodimm/boot.cmd` удалить из строки параметры UART:

```bash
# Было:
setenv bootargs console=ttyS0,115200 earlyprintk root=/dev/mmcblk2p2 rootwait

# Стало:
setenv bootargs root=/dev/mmcblk2p2 rootwait
```

```bash
make host-uboot-tools-rebuild
make
```

---

### RS-485 и COM симлинки

Симлинки создаются автоматически установщиком `install.sh` через udev-правила. Конфигурация зависит от версии устройства.

> **Различия версий:**
> - **СА-02м** (1 Ethernet) — 5 портов RS-485, `ttyS0` доступен
> - **СА-02м-2** (2 Ethernet) — 4 порта RS-485, `ttyS0` занят второй Ethernet-функцией

#### СА-02м — 1 Ethernet, 5 портов RS-485

| Симлинк | Устройство | Описание |
|---------|-----------|----------|
| `/dev/RS-485-0` → `/dev/COM1` | `/dev/ttyS0` | RS-485 порт 1 |
| `/dev/RS-485-1` → `/dev/COM2` | `/dev/ttyS3` | RS-485 порт 2 |
| `/dev/RS-485-2` → `/dev/COM3` | `/dev/ttyS4` | RS-485 порт 3 |
| `/dev/RS-485-3` → `/dev/COM4` | `/dev/ttyS5` | RS-485 порт 4 |
| `/dev/RS-485-4` → `/dev/COM5` | `/dev/ttyS7` | RS-485 порт 5 |

```bash
ln -sf /dev/ttyS0 /dev/COM1  && ln -sf /dev/ttyS0 /dev/RS-485-0
ln -sf /dev/ttyS3 /dev/COM2  && ln -sf /dev/ttyS3 /dev/RS-485-1
ln -sf /dev/ttyS4 /dev/COM3  && ln -sf /dev/ttyS4 /dev/RS-485-2
ln -sf /dev/ttyS5 /dev/COM4  && ln -sf /dev/ttyS5 /dev/RS-485-3
ln -sf /dev/ttyS7 /dev/COM5  && ln -sf /dev/ttyS7 /dev/RS-485-4
```

#### СА-02м-2 — 2 Ethernet, 4 порта RS-485 (без DO)

`ttyS0` используется второй Ethernet-подсистемой и **недоступен** как RS-485.  
Beeper и Alarm LED присутствуют (PCA9536), **дискретный выход DO отсутствует**.

| Симлинк | Устройство | Описание |
|---------|-----------|----------|
| `/dev/RS-485-0` → `/dev/COM1` | `/dev/ttyS3` | RS-485 порт 1 |
| `/dev/RS-485-1` → `/dev/COM2` | `/dev/ttyS4` | RS-485 порт 2 |
| `/dev/RS-485-2` → `/dev/COM3` | `/dev/ttyS5` | RS-485 порт 3 |
| `/dev/RS-485-3` → `/dev/COM4` | `/dev/ttyS7` | RS-485 порт 4 |

```bash
ln -sf /dev/ttyS3 /dev/COM1  && ln -sf /dev/ttyS3 /dev/RS-485-0
ln -sf /dev/ttyS4 /dev/COM2  && ln -sf /dev/ttyS4 /dev/RS-485-1
ln -sf /dev/ttyS5 /dev/COM3  && ln -sf /dev/ttyS5 /dev/RS-485-2
ln -sf /dev/ttyS7 /dev/COM4  && ln -sf /dev/ttyS7 /dev/RS-485-3
```

#### Диагностика UART

```bash
# Через dmesg (сразу после загрузки)
dmesg | grep tty

# Через /proc (статистика TX/RX/ошибки)
cat /proc/tty/driver/serial

# Через setserial
setserial -g /dev/ttyS[0-9]
```

---

### GPIO и периферия

Управление дискретными выходами (DO), пищалкой (Beeper) и аварийным LED производится через I2C-расширитель **PCA9536** (I2C шина 2, адрес `0x41`).

#### Конфигурация направлений (все пины — выходы)

```bash
i2cset -y 2 0x41 0x03 0x00
```

#### Управление выходами

```bash
i2cset -y 2 0x41 0x01 0x0E   # Включить выход 1 (DO — белый индикатор)
i2cset -y 2 0x41 0x01 0x0D   # Включить выход 2 (Beeper)
i2cset -y 2 0x41 0x01 0x0B   # Включить выход 3 (Alarm LED)
i2cset -y 2 0x41 0x01 0x07   # Включить выход 4 (синяя система OK)
i2cset -y 2 0x41 0x01 0x00   # Выключить всё
i2cset -y 2 0x41 0x01 0xff   # Включить всё
```

#### Диагностика I2C шины

```bash
# Список I2C шин
i2cdetect -l

# Сканирование шины 2 (найти PCA9536 по адресу 0x41)
i2cdetect -y 2

# Сканирование шины 3 (PCF8563 RTC по адресу 0x51)
i2cdetect -y 3
```

#### Включение i2c-tools в образе (через Buildroot)

```bash
make menuconfig
# Target packages → Hardware handling → [*] i2c-tools
make
```

---

### RTC (часы реального времени)

Внешний RTC **PCF8563** подключён к I2C3 (адрес `0x51`).

#### Добавление в DTS

В файле `output/build/linux-custom/arch/arm/boot/dts/sun8i-a40i-nano2e-none-sk.dts`:

```dts
&i2c3 {
    status = "okay";
    pcf8563: rtc@51 {
        compatible = "nxp,pcf8563";
        reg = <0x51>;
    };
};
```

#### Включение драйвера в ядре (через Buildroot)

```bash
make linux-menuconfig
# Device Drivers → Real Time Clock → <*> Philips PCF8563/Epson RTC8564
```

#### Настройка системы на использование PCF8563

По умолчанию система использует встроенный RTC (`rtc0`). PCF8563 при инициализации регистрируется как `rtc1`. Чтобы указать системе использовать его:

```bash
make linux-menuconfig
# Device Drivers → Real Time Clock →
#   [*] Set system time from RTC on startup and resume
#   (rtc1) RTC used to set the system time
```

Проверка после загрузки:

```bash
dmesg | grep rtc
ls /dev/rtc*
hwclock -r   # прочитать время из PCF8563
```

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
