# СА-02м — руководство по снятию и тиражированию компактного образа eMMC

Подробная документация по снятию дискового образа с настроенной SA-02m (Armbian + web-проект), его **уменьшению до реально занятого пространства** и **массовой заливке** на другие платы.

**Скрипты:** [`tools/imaging/`](../tools/imaging/)  
**Краткая инструкция:** [`tools/imaging/README.md`](../tools/imaging/README.md)

---

## Содержание

1. [Постановка задачи](#1-постановка-задачи)
2. [Аппаратная платформа и разметка диска](#2-аппаратная-платформа-и-разметка-диска)
3. [Почему текущие методы дают большой образ](#3-почему-текущие-методы-дают-большой-образ)
4. [Сравнение подходов к уменьшению образа](#4-сравнение-подходов-к-уменьшению-образа)
5. [Архитектура решения](#5-архитектура-решения)
6. [Требования и роли](#6-требования-и-роли)
7. [Подготовка хоста (Windows + WSL2)](#7-подготовка-хоста-windows--wsl2)
8. [Подготовка донорского устройства](#8-подготовка-донорского-устройства)
9. [Снятие образа (`make-image.sh`)](#9-снятие-образа-make-imagesh)
10. [Уменьшение образа (PiShrink)](#10-уменьшение-образа-pishrink)
11. [Заливка на приёмники](#11-заливка-на-приёмники)
12. [Первая загрузка клона](#12-первая-загрузка-клона)
13. [Уникальные идентификаторы и безопасность](#13-уникальные-идентификаторы-и-безопасность)
14. [Чек-листы](#14-чек-листы)
15. [Диагностика и troubleshooting](#15-диагностика-и-troubleshooting)
16. [FAQ](#16-faq)
17. [Ссылки и материалы](#17-ссылки-и-материалы)
18. [Профили sa02m-1eth / sa02m-2eth](#18-профили-sa02m-1eth--sa02m-2eth)
19. [manifest.json релиза образа](#19-manifestjson-релиза-образа)
20. [SSH с Windows и автоматизация](#20-ssh-с-windows-и-автоматизация)
21. [Roadmap (следующие этапы)](#21-roadmap-следующие-этапы)

---

## 1. Постановка задачи

### Цель

Иметь **воспроизводимый** процесс:

1. Снять образ с эталонной SA-02m, на которой уже установлен Armbian, web-интерфейс и все нужные настройки.
2. Получить файл **минимального размера** (сжатый `.img.xz`), а не полный дамп eMMC 7.28 GiB.
3. Залить образ на N других плат SA-02m с автоматическим **расширением rootfs** на всю eMMC при первой загрузке.

### Контекст проекта

- Целевая ОС на SA-02m: **Armbian 25.11.2 noble** (Ubuntu 24.04), ядро `6.12.58-current-sunxi`.
- Web-проект устанавливается через [`install.sh`](../install.sh) (nginx + fcgiwrap + Bash CGI).
- На доноре могут временно находиться **средства сборки драйверов** (gcc, dkms, linux-headers) — их нужно удалить перед снятием образа (см. [`MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md`](../MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md)).

### Что заменяем

| Старый способ | Проблема |
|---|---|
| `autorun.sh`: `rmmod g_mass_storage; dd if=/dev/mmcblk2 of=/mnt/sdcard.img; sync; reboot` | Образ = **весь диск** 7.28 GiB |
| ImageUSB / FEL + полный `dd` | То же: 7.28 GiB, долгая заливка, плохое xz-сжатие «мусорных» секторов |
| Запись `sdcard.img` с SD-карты | Лимит vfat 4 GiB на файл; SD 29 GiB не решает проблему размера образа |

### Не путать с sa02m-flasher

| | Образ eMMC (этот документ) | sa02m-flasher |
|---|---|---|
| Назначение | Вся ОС Armbian + web + MPLC на **СА-02м** | Прошивка модулей **MR-02м** по RS-485 |
| Носитель | `/dev/mmcblk2` | Flash память MR-02m (Modbus) |
| Инструменты | `make-image.sh`, `flash-receiver.sh` | `opt/sa02m-flasher/`, веб «Устройства» |
| Когда использовать | **Новая плата**, полная переустановка | Обновление прошивки MR-02m на линии RS-485 |

---

## 2. Аппаратная платформа и разметка диска

Данные сняты с эталонного устройства **192.168.1.136** (май 2026).

### Процессор и память

| Параметр | Значение |
|---|---|
| SoC | Allwinner A40i, 4× Cortex-A7 @ 1200 MHz |
| RAM | 512 MiB DDR3 |
| eMMC | 8 GiB номинально, **7.28 GiB** видимый объём (`/dev/mmcblk2`) |
| Блочное устройство eMMC | `/dev/mmcblk2` |
| HW boot partitions | `mmcblk2boot0`, `mmcblk2boot1` по 8 MiB — **не используются** sunxi U-Boot |

### Таблица разделов (MBR, DOS)

```
Disk /dev/mmcblk2: 7.28 GiB, 7818182656 bytes, 15269888 sectors
Disk identifier: 0x852073e2

Device         Boot  Start      End  Sectors  Size   Id  Type
/dev/mmcblk2p1 *      2048   133119   131072   64M   c   W95 FAT32 (LBA)  → /boot
/dev/mmcblk2p2      133120 14942208 14809089  7.1G  83  Linux             → /
```

### Схема на диске

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 0 … 2047 секторов (0 … ~1 MiB)                                           │
│   U-Boot SPL + MBR + неиспользуемое пространство до p1                   │
│   ⚠ dd if=/dev/mmcblk2 с offset 0 ОБЯЗАТЕЛЬНО включает загрузчик       │
├──────────────────────────────────────────────────────────────────────────┤
│ mmcblk2p1  64 MiB  vfat   UUID=D8CE-50BA   монтируется как /boot         │
│   armbianEnv.txt, boot.scr, zImage, dtb, initrd                          │
├──────────────────────────────────────────────────────────────────────────┤
│ mmcblk2p2  7.1 GiB ext4   UUID=3599389b-2279-4cc9-8fd1-43135f13a73b     │
│   корневая ФС /  (armbianEnv: rootdev=UUID=3599389b-…)                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### Внешние носители на SA-02m

| Устройство | Размер | ФС | Точка монтирования | Замечание |
|---|---|---|---|---|
| `/dev/mmcblk3` | 29.1 GiB | vfat | `/media/sdcard` | **Лимит 4 GiB на один файл** — не подходит для полного `.img` |
| `/dev/sda1` | 7.2 GiB | NTFS | `/media/usb` | Почти равен eMMC; для старого `dd of=/mnt/sdcard.img` |

### Загрузка (Armbian / sunxi)

- Загрузчик: **U-Boot** с SPL (`u-boot-sunxi-with-spl.bin`), запись в eMMC: `dd … bs=8k seek=1` (смещение 8 KiB).
- Конфиг: `/boot/armbianEnv.txt`:
  ```ini
  rootdev=UUID=3599389b-2279-4cc9-8fd1-43135f13a73b
  rootfstype=ext4
  ```
- Сервис расширения ФС: **`armbian-resize-filesystem.service`** (enabled), срабатывает при наличии `/root/.not_logged_in_yet`.

### Занятость диска (до cleanup, май 2026)

| Каталог | Размер | Содержимое |
|---|---|---|
| `/` всего | **3.8 GiB** | 56% от 7.0 GiB |
| `/root` | 1.4 GiB | `backup`, `mplc_cyntron_build`, `sa02m-deploy*`, `cursor_build.swap`, `u-boot-sunxi-with-spl.bin` |
| `/usr` | 1.3 GiB | gcc-11/12/13, `build-essential`, `dkms`, `make` |
| `/var` | 1023 MiB | apt cache 592 MiB, apt lists 280 MiB, logs 50 MiB |
| `/opt` | 113 MiB | прикладное ПО |
| `/boot` | 5.9 MiB | ядро, dtb, initrd |

**Прогноз после cleanup:** ~**1.0–1.3 GiB** занято → финальный `.img.xz` ~**350–500 MiB**.

Актуально (2026-08-07, `.136` после `tools/imaging/cleanup-donor.sh --apply`): rootfs **used ~2.2 GiB** (было ~3.1), `/root` **~640 KiB** (было ~786 MiB стендового `sa02m-deploy-*` / `.npm` / `*.deb`). Скрипт: `--dry-run` / `--apply`; ТЗ — [`TZ_PRE_PRODUCTION_DONOR_CLEANUP.md`](TZ_PRE_PRODUCTION_DONOR_CLEANUP.md). `make-image.sh` вызывает cleanup с `--apply --purge-update-staging`.

### Снимок аудита эталона (192.168.1.136, май 2026)

Проверка по SSH (`private/.ssh/sa02m_sa02`). Используйте как эталонные значения при сравнении после cleanup/заливки.

| Параметр | Значение |
|---|---|
| Hostname | `SA-02` |
| Модель | `Cyntron A40i-2Eth` |
| ОС | Armbian 25.11.2 noble, ядро `6.1.0-rc6` |
| Root | `/dev/mmcblk2p2`, ext4, 7,0 GiB, **56%** занято (~3,8 GiB) **до** cleanup |
| Профиль RS-485 | `SA02M_SERIAL_PROFILE=sa02m-1eth` |
| Web-build SHA | `58dea9227014fea21cc4cfa415c58a9df221b187` |
| eth0 | `192.168.1.136/24`, UP |
| eth1 | DOWN |
| USB `/media/usb` | `sda1`, 7,2 GiB NTFS, почти пуст |
| SD `/media/sdcard` | `mmcblk3`, 29 GiB vfat, почти пуст — **не** использовать для raw `.img` >4 GiB (лимит vfat) |
| Утилиты **есть** | `dd`, `gzip`, `xz`, `rsync`, `parted`, `pv` |
| Утилиты **нет** | `growpart`, `partclone`, `zerofree`, `pigz` — на доноре не обязательны (PiShrink на хосте) |
| Armbian resize | `/usr/lib/armbian/armbian-resize-filesystem` |
| Boot на p1 | UUID `D8CE-50BA`; root UUID `3599389b-2279-4cc9-8fd1-43135f13a73b` |

**Замечания по SSH при аудите:**

- Короткие команды (`which dd`, `uname -a`) работают стабильно; **длинные one-liner** с десятком подкоманд могут **таймаутиться** на клиенте Windows OpenSSH — разбивайте на отдельные вызовы.
- На «рабочих» платах возможен **post-auth hang** SSH — см. [`SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md`](SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md). Для производства предпочтительна заливка через **USB gadget + flash-receiver.sh**, не только SSH.
- Разовый сбой `systemctl is-active`: `Transport endpoint is not connected` (D-Bus) — перед снятием образа рекомендуется **reboot** донора.

**microSD mmcblk3:** `fdisk -l` может показывать некорректную GPT при том, что система монтирует весь диск как vfat. Переформатировать SD перед использованием как носителя артефактов.

---

## 3. Почему текущие методы дают большой образ

### Блочное копирование

`dd if=/dev/mmcblk2 of=…` копирует **все 15 269 888 секторов** (7.28 GiB), включая:

- пустое пространство внутри раздела p2 (ext4 помечает блоки «свободными», но физически на eMMC могут оставаться старые данные);
- неиспользуемый хвост раздела p2 (если раздел создан на весь диск, а данных мало);
- boot-область и p1 целиком (64 MiB — это нормально и нужно сохранить).

### Плохое сжатие «сырого» dd

Алгоритмы **xz/gzip** хорошо сжимают **нули** и **повторяющиеся паттерны**. Случайные «остатки» удалённых файлов в свободных блоках ext4 сжимаются **плохо**. Поэтому:

1. **Zero-fill** перед dd — заполнить свободное место нулями (`dd if=/dev/zero of=/zero.fill; rm`).
2. **PiShrink** — уменьшить ext4 и обрезать файл образа до конца последнего раздела.

Оба шага критичны для минимального `.img.xz`.

---

## 4. Сравнение подходов к уменьшению образа

| Метод | Итоговый .img | Итоговый .img.xz | Авто-grow на новой eMMC | Сложность | Рекомендация |
|---|---|---|---|---|---|
| **dd всего диска + xz** | 7.28 GiB | ~2–3 GiB | нет (раздел уже на весь диск) | низкая | ❌ устарело |
| **dd + zero-fill + xz** | 7.28 GiB | ~1.5 GiB | нет | средняя | ⚠️ только если нет PiShrink |
| **dd + PiShrink + xz** | ~1.5 GiB | **~350–500 MiB** | **да** (rc.local + Armbian) | средняя | ✅ **основной** |
| **e2image -r -a** (только used blocks) | меньше | меньше | требует отдельной сборки образа | высокая | ❌ не для FEL/USB dd |
| **rsync / tar корня** | N/A | меньше | вручную | высокая | ❌ не сохраняет загрузчик/MBR |

**Выбранная связка:** cleanup → zero-fill → `dd` stream по ssh → **PiShrink** → `xz -9e`.

Аналогичный подход описан для Raspberry CM4/CM5 в [статье AntexGate на Habr](https://habr.com/ru/articles/1024312/) с использованием [PiShrink](https://github.com/Drewsif/PiShrink).

---

## 5. Архитектура решения

```
  ЭТАП A — ДОНОР (SA-02m)              ЭТАП B — ХОСТ (WSL2 Ubuntu)
  ─────────────────────────            ──────────────────────────────

  cleanup-donor.sh                     make-image.sh
       │                                    │
       ├─ удалить /root мусор               ├─ ssh: cleanup (если не сделан)
       ├─ purge gcc/dkms/headers            ├─ ssh: zero-fill
       ├─ apt clean, journal vacuum         ├─ ssh: dd mmcblk2 | xz → raw.img.xz
       ├─ reset machine-id, ssh keys         ├─ xz -d → raw.img
       └─ touch .not_logged_in_yet          ├─ pishrink.sh → shrunk.img
                                            ├─ xz -9e → shrunk.img.xz
                                            └─ sha256sum

  ЭТАП C — ПРИЁМНИКИ (SA-02m × N)
  ────────────────────────────────

  flash-receiver.sh (или autorun.sh → symlink)
       │
       ├─ rmmod g_mass_storage
       ├─ sha256 verify
       ├─ xz -dc | dd of=/dev/mmcblk2
       └─ reboot
            │
            ▼ first boot
       armbian-resize-filesystem.service  → growpart + resize2fs
       regen-ssh-host-keys.service        → ssh-keygen -A
       systemd-machine-id-setup           → новый machine-id
```

### Файлы в репозитории

| Файл | Где выполняется | Назначение |
|---|---|---|
| [`tools/imaging/cleanup-donor.sh`](../tools/imaging/cleanup-donor.sh) | на доноре (stdin через `ssh bash -s`) | подготовка к снятию |
| [`tools/imaging/make-image.sh`](../tools/imaging/make-image.sh) | на хосте Linux/WSL2 | полный цикл снятия |
| [`tools/imaging/flash-receiver.sh`](../tools/imaging/flash-receiver.sh) | на приёмнике | заливка `.img.xz` |
| [`tools/imaging/prepare-flash-media.sh`](../tools/imaging/prepare-flash-media.sh) | на хосте | упаковка USB: образ + sha256 + autorun |
| [`tools/imaging/manifest.example.json`](../tools/imaging/manifest.example.json) | — | шаблон manifest (make-image пишет `.manifest.json` автоматически) |
| [`tools/imaging/README.md`](../tools/imaging/README.md) | — | краткая операционная справка |

---

## 6. Требования и роли

### Роли

| Роль | Описание | Типичный IP |
|---|---|---|
| **Донор** | Эталонная SA-02m с финальной конфигурацией | `192.168.1.136` |
| **Хост** | ПК с WSL2 Ubuntu для PiShrink и хранения образов | Windows + WSL |
| **Приёмник** | Новая/чистая SA-02m для заливки | любой |

### Доступ к донору

- SSH: `root@192.168.1.136`, ключ: `private/.ssh/sa02m_sa02`
- Проверка:
  ```bash
  ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "uname -a; df -h /"
  ```

### Зависимости на хосте (WSL2)

| Пакет / утилита | Зачем |
|---|---|
| `openssh-client` | ssh к донору |
| `xz-utils` | сжатие/распаковка |
| `e2fsprogs` | `e2fsck`, `resize2fs` (PiShrink) |
| `parted`, `util-linux` | `growpart`, `truncate`, `sfdisk` |
| `kpartx` | loop-устройства (PiShrink) |
| `pishrink.sh` | уменьшение образа |
| `wget` | установка PiShrink |

### Зависимости на приёмнике

- `xz` — распаковка при заливке (`flash-receiver.sh`)
- `sha256sum` — проверка целостности
- `dd`, `sync`, `reboot`

---

## 7. Подготовка хоста (Windows + WSL2)

### 7.1. Установка WSL2 Ubuntu

```powershell
# PowerShell (администратор)
wsl --install -d Ubuntu
```

После перезагрузки — создать пользователя Ubuntu, затем внутри WSL:

```bash
sudo apt update
sudo apt install -y \
    kpartx parted util-linux e2fsprogs xz-utils zerofree wget openssh-client

sudo wget -O /usr/local/bin/pishrink.sh \
    https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
sudo chmod +x /usr/local/bin/pishrink.sh
pishrink.sh --help   # проверка
```

### 7.2. SSH-ключ

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 ~/.ssh/
chmod 600 ~/.ssh/sa02m_sa02

ssh -i ~/.ssh/sa02m_sa02 -o StrictHostKeyChecking=accept-new root@192.168.1.136 uname -nrm
```

### 7.3. Каталог для артефактов

```bash
cd /mnt/c/Users/admin/Downloads/SA-02m-web-build/tools/imaging
chmod +x cleanup-donor.sh make-image.sh flash-receiver.sh
mkdir -p out
```

### 7.4. Доступ WSL2 к локальной сети (обязательно для SSH на донор)

По умолчанию WSL2 сидит за NAT (`172.x.x.x`) и не видит устройства в LAN так же, как Windows. Для `make-image.sh` / SSH на `192.168.x.x` нужен режим **mirrored**:

```powershell
# PowerShell от администратора, из корня репозитория:
powershell -ExecutionPolicy Bypass -File tools\imaging\setup-wsl-network.ps1
```

Скрипт создаёт `%USERPROFILE%\.wslconfig` с `networkingMode=mirrored` и выполняет `wsl --shutdown`. После перезапуска WSL использует те же сетевые интерфейсы, что и Windows (все NIC, VPN, маршруты).

Проверка:

```bash
ip -br a          # должны появиться адреса Windows (192.168.x.x и т.д.), не только 172.x
ping -c1 192.168.1.136
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.136 uname -nrm
```

Требования: `wsl --version` → WSL ≥ 2.0. Если mirrored недоступен — обновить WSL: `wsl --update`.

### 7.5. Замечания по WSL2

- PiShrink требует **`sudo`** и loop-устройства — в WSL2 это работает (kernel ≥ 5.4).
- Если `sudo` зависает в integrated terminal IDE — запускайте из обычного окна **Windows Terminal → Ubuntu**.
- Диск `C:` доступен как `/mnt/c/…` — образы можно хранить на Windows-разделе.

---

## 8. Подготовка донорского устройства

Скрипт: [`cleanup-donor.sh`](../tools/imaging/cleanup-donor.sh)

### 8.1. Когда запускать

- После того как на доноре **полностью установлен и проверен** web-проект (`install.sh`), сеть, драйверы, все настройки.
- **Перед каждым** production-снятием образа (не на «рабочей» плате, где ещё собирают драйверы).

### 8.2. Что удаляется (пошагово)

#### Шаг 1 — мусор в `/root` и `/home`

Явные glob-списки (не `rm -rf /root/*`). В т.ч. **`/root/sa02m-deploy-*`** (раньше чистилось только точное имя `sa02m-deploy`). Deny-лист защищает `/var/www/network_config`, `/opt/mplc4`, `/opt/codesys`, `/opt/sa02m-*`, Alice certs, flasher firmware, сеть/nginx/MQTT.

| Путь / паттерн | Причина удаления |
|---|---|
| `/root/sa02m-deploy`, `sa02m-deploy-*`, `sa02m-install-*` | staging веб-деплоев |
| `/root/deploy-*.tar*` , `deploy.tar.gz` | архивы деплоя |
| `/root/.npm`, `.cache`, history, Trash | кеши разработчика |
| `/root/mplc_backup*`, `*-backup-*.tgz`, `zImage*.bak*` | стендовые бэкапы / эксперименты |

**Не удаляются** (нужны для работы/установки): `/opt/vendor-installers/**`, `/opt/mplc4`, `/opt/codesys`, `/opt/sa02m-*`, веб, nginx/MQTT/SSH, flasher firmware, Node-RED (`/root/.node-red`), `/tmp/sa02m-gpioset-*` (USB power). `/root/*.deb` / `mplc_update` — только с `--purge-installers`. `/tmp` целиком не сносится.
| `/var/lib/sa02m-update/{staging,incoming,runner}/*`, `/tmp/sa02m-*` | эфемерный update staging |

#### Шаг 2 — тулчейн (apt purge)

Пакеты по шаблонам:

- `build-essential`, `dkms`, `make`
- `gcc`, `gcc-11`, `gcc-12`, `gcc-13`, `g++`, `cpp`
- `gcc-arm-linux-gnueabihf`, `gcc-13-arm-linux-gnueabihf`
- `libgcc-*-dev`, `libstdc++-*-dev`
- `linux-headers-*`, `linux-source-*`

> **Важно:** после cleanup на доноре **нельзя** собирать ядерные модули без переустановки пакетов. Держите отдельную «dev»-плату или восстанавливайте тулчейн из [`MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md`](../MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md).

#### Шаг 3 — apt cache

```bash
apt-get clean
rm -rf /var/lib/apt/lists/*
```

Освобождает ~**870 MiB** (`/var/cache/apt` + lists).

#### Шаг 4 — journald и логи

- `journalctl --vacuum-time=1s`
- truncate всех `*.log`, удаление ротированных логов
- очистка `/tmp`, `/var/tmp`

#### Шаг 5 — уникальные идентификаторы

| Действие | Файл | На клоне |
|---|---|---|
| `truncate -s 0` | `/etc/machine-id` | systemd создаст новый при boot |
| symlink | `/var/lib/dbus/machine-id` → `/etc/machine-id` | — |
| `rm` | `/etc/ssh/ssh_host_*` | `regen-ssh-host-keys.service` |

Сервис `regen-ssh-host-keys.service`:

```ini
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
Before=ssh.service
ExecStart=/usr/bin/ssh-keygen -A
```

#### Шаг 6 — Armbian firstrun

```bash
touch /root/.not_logged_in_yet
```

При первой загрузке **каждого клона** Armbian выполнит:

- `armbian-resize-filesystem.service` — расширение p2 на всю eMMC;
- стандартный firstrun (locale, пользователь и т.д., если настроено).

### 8.3. Запуск cleanup

Без `--apply` скрипт **ничего не удаляет** (dry-run). `make-image.sh` / `capture-image-win.py` передают `--apply` явно.

**Вручную на доноре:**

```bash
bash tools/imaging/cleanup-donor.sh --dry-run --report
bash tools/imaging/cleanup-donor.sh --apply --report
```

**С Windows-хоста (предпочтительно):**

```bash
py -3 tools/imaging/run-cleanup-donor.py --dry-run --report
py -3 tools/imaging/run-cleanup-donor.py --apply --report
```

**С хоста через SSH stdin:**

```bash
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.136 'bash -s -- --dry-run --report' < tools/imaging/cleanup-donor.sh
```

**Через make-image.sh** — cleanup с `--apply` выполняется автоматически (флаг `--no-cleanup` отключает).

### 8.4. Zero-fill (заполнение нулями)

Выполняется **после cleanup**, **перед dd**:

```bash
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.136 \
  'dd if=/dev/zero of=/zero.fill bs=4M status=progress 2>&1 | tail -1; sync; rm -f /zero.fill; sync'
```

| Аспект | Детали |
|---|---|
| Зачем | нули в свободных блоках → xz сжимает raw-образ в ~2× лучше |
| Износ eMMC | одна полная запись свободного объёма (~3 GiB) — допустимо для редких релизов |
| Время | ~3–8 минут на SA-02m |
| Пропуск | `--no-zerofill` в `make-image.sh` (быстрее, хуже сжатие) |

---

## 9. Снятие образа (`make-image.sh`)

### 9.1. Полный цикл (одна команда)

```bash
cd /mnt/c/Users/admin/Downloads/SA-02m-web-build/tools/imaging
chmod +x *.sh
./make-image.sh --ip 192.168.1.136 --key ~/.ssh/sa02m_sa02 --out-dir ./out \
    --profile sa02m-1eth --version 1.0.0
```

Без `--profile` / `--version` имя файла будет `sa02m-YYYYMMDD-HHMM-shrunk.img.xz`.

### 9.2. Параметры

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--ip` | `192.168.1.136` | IP донора |
| `--key` | `$HOME/.ssh/sa02m_sa02` | путь к приватному ключу |
| `--out-dir` | `./out` | каталог артефактов |
| `--profile` | — | `sa02m-1eth` или `sa02m-2eth` → имя `sa02m-1eth-vX.Y.Z-shrunk.img.xz` (с `--version`) |
| `--version` | — | версия релиза, напр. `1.0.0` |
| `--no-cleanup` | cleanup **включён** | пропустить cleanup |
| `--no-zerofill` | zerofill **включён** | пропустить zero-fill |
| `--no-manifest` | manifest **включён** | не писать `.manifest.json` |
| `--xz-level` | `1` | xz при стриме dd (1=быстро) |
| `--final-xz-level` | `9e` | xz финального shrunk-образа |

### 9.3. Внутренние шаги make-image.sh

| # | Действие | Выходной файл |
|---|---|---|
| 0 | проверка ssh, xz, pishrink, python3, ключ; сбор метаданных донора | — |
| 1 | cleanup на доноре | — |
| 2 | zero-fill | — |
| 3 | stop nginx/fcgiwrap/sa02m-flasher/mplc; `dd if=/dev/mmcblk2` → pipe → `xz` | `out/sa02m-YYYYMMDD-HHMM-raw.img.xz` |
| 4 | `xz -d`; `sudo pishrink.sh -a -v`; `xz -9e` | `out/…-shrunk.img.xz` |
| 5 | `sha256sum` | `…-shrunk.img.xz.sha256` |
| 6 | `manifest.json` (python3) | `…-shrunk.manifest.json` |

### 9.4. Почему stream через ssh, а не запись на SD

| Проблема SD `/dev/mmcblk3` | Решение stream |
|---|---|
| vfat: **max 4 GiB на файл** | файл пишется на хост, без лимита vfat |
| 29 GiB SD почти пуста, но не помогает размеру | — |
| нужно ~7 GiB свободного на носителе | на хосте достаточно места под `.xz` (~1.5 GiB raw + ~0.5 GiB shrunk) |
| два прохода (dd на SD, копирование на ПК) | один проход по сети |

### 9.5. Ручной stream (без make-image.sh)

```bash
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.136 \
  'systemctl stop nginx php8.3-fpm 2>/dev/null; rmmod -f g_mass_storage 2>/dev/null; sync; sync; dd if=/dev/mmcblk2 bs=4M status=none' \
  | xz -T0 -1 -v > sa02m-raw.img.xz
```

### 9.6. Ожидаемые размеры и время

| Артефакт | Размер | Время (ориентир) |
|---|---|---|
| `raw.img` (распакованный) | 7.28 GiB | — |
| `raw.img.xz` | ~1.2–1.8 GiB | 5–15 мин (зависит от сети 100M/1G) |
| `shrunk.img` (после PiShrink) | ~1.3–1.8 GiB | 2–5 мин |
| `shrunk.img.xz` | **~350–500 MiB** | 5–20 мин (xz -9e) |

---

## 10. Уменьшение образа (PiShrink)

[PiShrink](https://github.com/Drewsif/PiShrink) — bash-скрипт, изначально для Raspberry Pi, совместим с **MBR + ext4** (наша разметка SA-02m).

### 10.1. Алгоритм PiShrink

1. Подключить образ через loop (`/dev/loopN`).
2. `e2fsck -fy /dev/loopNp2` — проверка ext4.
3. `resize2fs -M /dev/loopNp2` — сжать ФС до минимума (только используемые блоки + метаданные).
4. `parted/sfdisk` — уменьшить конец раздела p2 до размера ФС + запас (~256 MiB).
5. `truncate -s …` — обрезать файл `.img` до конца последнего раздела.
6. Вставить **`/etc/rc.local`** (или патч init) со скриптом:
   ```bash
   growpart /dev/mmcblk2 2
   resize2fs /dev/mmcblk2p2
   ```
   который выполнится **один раз** при первой загрузке клона.

### 10.2. Ручной запуск PiShrink

```bash
xz -d -k sa02m-YYYYMMDD-HHMM-raw.img.xz
sudo pishrink.sh -a -v sa02m-YYYYMMDD-HHMM-raw.img
xz -T0 -9e -f sa02m-YYYYMMDD-HHMM-raw.img   # PiShrink перезаписывает входной файл
```

Флаги:

| Флаг | Значение |
|---|---|
| `-a` | auto-yes (без интерактива) |
| `-v` | verbose |
| `-Z` | xz внутри PiShrink (в `make-image.sh` **не используем** — сжимаем сами с `-9e`) |

### 10.3. Совместимость с Armbian resize

На SA-02m **два** механизма расширения rootfs:

| Механизм | Триггер | Действие |
|---|---|---|
| `armbian-resize-filesystem.service` | `/root/.not_logged_in_yet` | growpart + resize2fs (штатный Armbian) |
| `/etc/rc.local` от PiShrink | первый boot | то же |

Оба безопасны: второй завершится без изменений, если раздел уже расширен.

### 10.4. Ограничения PiShrink

| Ситуация | Поведение |
|---|---|
| eMMC приёмника **меньше** shrunk-образа | ❌ dd не поместится — нужен образ меньше целевой eMMC |
| eMMC приёмника **больше** | ✅ growpart растянет p2 |
| Нестандартная GPT / LVM | ⚠️ PiShrink рассчитан на MBR + последний ext4-раздел |
| Зашифрованный root (LUKS) | ❌ не поддерживается PiShrink «из коробки» |

---

## 11. Заливка на приёмники

Скрипт: [`flash-receiver.sh`](../tools/imaging/flash-receiver.sh)

### 11.1. Вариант A — USB mass storage gadget + autorun (основной)

**Исторический autorun.sh на доноре:**

```sh
rmmod -f g_mass_storage
dd if=/dev/mmcblk2 of=/mnt/sdcard.img && sync
reboot
```

**Новый flash-receiver.sh:**

```sh
rmmod -f g_mass_storage
xz -dc /mnt/sa02m-shrunk.img.xz | dd of=/dev/mmcblk2 bs=4M conv=fsync
sync; reboot
```

#### Подготовка носителя

**Автоматически (рекомендуется):**

```bash
cd tools/imaging
./prepare-flash-media.sh \
    --image ./out/sa02m-1eth-v1.0.0-shrunk.img.xz \
    --dest /mnt/c/USB/SA02m
```

Скрипт кладёт на носитель:

```
/mnt/sa02m-shrunk.img.xz          # копия образа (имя по умолчанию для flash-receiver)
/mnt/sa02m-shrunk.img.xz.sha256
/mnt/flash-receiver.sh
/mnt/autorun.sh                   → ln -s flash-receiver.sh
/mnt/manifest.json                # если есть *.manifest.json рядом с образом
```

**Вручную** — скопировать на USB-флешку или раздел, который монтируется как `/mnt`:

```
/mnt/sa02m-shrunk.img.xz
/mnt/sa02m-shrunk.img.xz.sha256
/mnt/flash-receiver.sh
/mnt/autorun.sh          → симлинк: ln -s flash-receiver.sh autorun.sh
```

Пример `.sha256`:

```
a1b2c3d4…  sa02m-shrunk.img.xz
```

#### Переменные окружения flash-receiver.sh

| Переменная | По умолчанию | Описание |
|---|---|---|
| `IMG_DIR` | `/mnt` | каталог с образом |
| `IMG_NAME` | `sa02m-shrunk.img.xz` | имя файла образа |
| `TARGET_DEV` | `/dev/mmcblk2` | целевое блочное устройство |

Пример с другим именем файла:

```sh
IMG_NAME=sa02m-20260520-shrunk.img.xz sh /mnt/flash-receiver.sh
```

#### Лог заливки

`/var/log/flash-receiver.log` — все этапы с timestamp.

#### Время заливки

| Объём записи | Время (ориентир) |
|---|---|
| 7.28 GiB (старый способ) | 10–15 мин |
| ~1.5 GiB shrunk + xz | **2–4 мин** |

### 11.2. Вариант B — FEL + ImageUSB (голые платы)

Для плат **без рабочей ОС** или когда USB-gadget недоступен:

1. Перевести SA-02m в **FEL** (USB-OTG, см. [README проекта — прошивка](../README.md#прошивка-образа-на-emmc)).
2. Загрузить U-Boot через `sunxi-fel`.
3. **Либо** записать распакованный `.img` через ImageUSB / `dd` с ПК (нужен **полный** `.img`, не `.xz`).
4. **Либо** загрузиться с SD/USB и запустить `flash-receiver.sh`.

Распаковка на хосте перед ImageUSB (или `./prepare-imageusb.sh --image …`):

```bash
./prepare-imageusb.sh --image ./out/sa02m-YYYYMMDD-shrunk.img.xz --dest /mnt/c/SA02m
# ImageUSB → Write → sa02m-…-shrunk.img
```

> ImageUSB пишет **файл образа** напрямую — xz нужно распаковать **на ПК**, не на устройстве.  
> После первой загрузки `sa02m-rootfs-expand.service` (или `armbian-resize-filesystem`) растягивает p2 до ~7 GiB.

### 11.3. Вариант C — заливка по SSH (если приёмник уже в сети)

```bash
scp -i ~/.ssh/sa02m_sa02 out/sa02m-*-shrunk.img.xz root@192.168.1.XXX:/tmp/
scp -i ~/.ssh/sa02m_sa02 out/sa02m-*-shrunk.img.xz.sha256 root@192.168.1.XXX:/tmp/
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.XXX \
  'cd /tmp && sha256sum -c sa02m-*-shrunk.img.xz.sha256 && \
   xz -dc sa02m-*-shrunk.img.xz | dd of=/dev/mmcblk2 bs=4M conv=fsync && sync && reboot'
```

### 11.4. Вариант D — сетевой провижионер (серийное производство)

По аналогии с [CM Provisioner / AntexGate](https://habr.com/ru/articles/1024312/):

- **Сервер:** DHCP + TFTP + HTTP, хранит `sa02m-shrunk.img.xz` + sha256.
- **Приёмник:** скрипт при boot скачивает образ, проверяет sha256, пишет в `/dev/mmcblk2`, reboot.

Подходит для партий 20+ плат; требует отдельной инфраструктуры (не входит в текущие скрипты).

---

## 12. Первая загрузка клона

После успешной заливки и reboot:

```
  Power ON
     │
     ▼
  U-Boot SPL (из первых секторов образа)
     │
     ▼
  boot.scr → загрузка zImage + dtb + initrd
     │
     ▼
  kernel: root=UUID=3599389b-… (mmcblk2p2)
     │
     ▼
  systemd multi-user.target
     │
     ├─► armbian-resize-filesystem.service
     │      (если /root/.not_logged_in_yet)
     │      growpart /dev/mmcblk2 2
     │      resize2fs /dev/mmcblk2p2
     │      → rootfs ≈ 7.0 GiB
     │
     ├─► regen-ssh-host-keys.service
     │      (если нет /etc/ssh/ssh_host_ed25519_key)
     │      ssh-keygen -A
     │
     ├─► systemd-machine-id-setup
     │      (если /etc/machine-id пуст)
     │
     └─► nginx, fcgiwrap, web UI — готовы к работе
```

### Проверка после первой загрузки

```bash
ssh -o StrictHostKeyChecking=accept-new root@<IP_КЛОНА>

df -h /                    # rootfs ≈ 7.0G, Used ≈ 1.2G
ls -la /etc/ssh/ssh_host_* # ключи созданы
cat /etc/machine-id        # не пустой, уникальный
systemctl status nginx fcgiwrap
curl -k https://localhost:9999/   # web UI
```

---

## 13. Уникальные идентификаторы и безопасность

### Что сбрасывается cleanup-ом

| Идентификатор | На клоне | Конфликт в сети? |
|---|---|---|
| `/etc/machine-id` | новый UUID | нет |
| `/etc/ssh/ssh_host_*` | новые ключи | нет (но клиент увидит «host key changed» — нормально) |
| `/root/.not_logged_in_yet` | Armbian firstrun | нет |

### Что остаётся одинаковым на всех клонах

| Параметр | Значение | Риск |
|---|---|---|
| UUID rootfs p2 | `3599389b-2279-4cc9-8fd1-43135f13a73b` | **нет** — UUID локален для каждого устройства |
| UUID boot p1 | `D8CE-50BA` | нет |
| Hostname | `SA-02` (если не меняли) | ⚠️ если два клона в одной L2-сети с одним IP — конфликт **IP**, не UUID |
| MAC eth0/eth1 | одинаковые на клонах | **нет** — link-файлы обновляются автоматически сервисом `sa02m-net-autolink` |
| Пароль root | `cyntron` (по умолчанию Armbian) | сменить после первого входа на каждом клоне |

### Автоматическое обновление MAC-адресов в link-файлах

Начиная с версии 1.0.3.23 при переносе образа на другое устройство **ручное обновление MAC-адресов в link-файлах не требуется**.

Сервис `sa02m-net-autolink` (установлен через `scripts/01-system.sh`) запускается **до** `systemd-networkd` и автоматически обновляет `/etc/systemd/network/10-eth0.link` и `11-eth1.link` при обнаружении смены MAC-адресов.

**Как работает:**
- Находит все физические Ethernet-интерфейсы через `/sys/class/net/*/device`
- Для Cyntron A40i-2Eth: первый интерфейс имеет MAC с префиксом `02:53:xx` → `eth0`, второй `12:53:xx` → `eth1`
- Если MACs совпадают с записанными — быстрый выход без изменений (идемпотентный)
- Если MACs изменились — обновляет link-файлы и перезапускает `systemd-networkd`
- Лог: `/var/log/sa02m_net_autolink.log`

**Проверка статуса:**

```bash
systemctl status sa02m-net-autolink.service
cat /var/log/sa02m_net_autolink.log
```

**Ручной запуск (диагностика):**

```bash
bash /usr/local/sbin/sa02m-net-autolink.sh
```

### Рекомендации для production

1. После заливки задавать **уникальный IP** (или DHCP) на каждом клоне через web UI / `install.sh --ip`.
2. Сменить пароль root / admin web UI.
3. При необходимости — уникальный hostname: `hostnamectl set-hostname SA-02-NNN`.
4. **Не** клонировать Wi-Fi credentials: если используется NetworkManager — удалить `/etc/NetworkManager/system-connections/*` в cleanup (опционально, добавить в cleanup-donor.sh при необходимости).

### Опционально: уникальный UUID rootfs

Если политика безопасности требует уникальный UUID раздела:

```bash
tune2fs -U random /dev/mmcblk2p2
# обновить /boot/armbianEnv.txt rootdev=UUID=<новый>
update-initramfs -u
reboot
```

Для типичного тиража SA-02m с фиксированными статическими IP **не требуется**.

---

## 14. Чек-листы

### 14.1. Перед снятием образа (донор)

- [ ] Web UI работает, все сервисы `active (running)`
- [ ] Версия проекта зафиксирована (git tag / CHANGELOG)
- [ ] На доноре **нет** незавершённых apt/dpkg операций
- [ ] Донор доступен по ssh с ключом `sa02m_sa02`
- [ ] Принято решение: cleanup **уничтожит** gcc/dkms на доноре
- [ ] WSL2 на хосте: установлены xz, pishrink, e2fsprogs
- [ ] На хосте ≥ **3 GiB** свободного места в `out/`
- [ ] Сеть стабильна (лучше прямое подключение 1G, не Wi-Fi)

### 14.2. После make-image.sh (хост)

- [ ] Файл `sa02m-*-shrunk.img.xz` создан
- [ ] Файл `.sha256` создан
- [ ] Файл `*.manifest.json` создан (если не `--no-manifest`)
- [ ] Размер shrunk.xz в диапазоне **300–600 MiB** (иначе — проверить cleanup/zerofill)
- [ ] Артефакты скопированы в архив релиза / NAS

### 14.3. Перед заливкой (приёмник)

- [ ] Образ и `.sha256` на носителе
- [ ] `flash-receiver.sh` + symlink `autorun.sh`
- [ ] Приёмник получает питание, USB подключён (для gadget-режима)
- [ ] Целевое устройство — **приёмник**, не донор (dd уничтожит eMMC)

### 14.4. После заливки (приёмник)

- [ ] Устройство загрузилось с eMMC (без SD)
- [ ] `df -h /` — rootfs ~7 GiB, Used ~1.2 GiB
- [ ] SSH с **новым** host key (не «WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED» на том же IP — ожидаемо)
- [ ] Web UI на `:9999` открывается
- [ ] Задан уникальный IP / hostname для этого экземпляра
- [ ] RS-485, GPIO, flasher — smoke test по чек-листу проекта

---

## 15. Диагностика и troubleshooting

| Симптом | Вероятная причина | Решение |
|---|---|---|
| `pishrink.sh: not found` | не установлен на хосте | см. [§7.1](#71-установка-wsl2-ubuntu) |
| `ssh: connect timed out` | донор offline / неверный IP | ping, проверить eth0, кабель |
| `Connection reset` / `Connection refused` после cleanup | host keys удалены до dd, sshd не стартует | см. [`restore-donor-ssh.sh`](../tools/imaging/restore-donor-ssh.sh) на serial; **make-image v1.2+** делает id reset только в одной сессии с dd |
| raw.img.xz > 2.5 GiB | не был cleanup или zerofill | повторить с cleanup + zerofill |
| shrunk.img.xz > 800 MiB | много данных на доноре | проверить `du -hxd1 /` на доноре |
| `e2fsck: Bad magic number` | повреждение при dd/stream | повторить stream; проверить сеть |
| PiShrink завис на resize2fs | фрагментация ext4 | на доноре: `e2fsck -fy /dev/mmcblk2p2`; повторить |
| `No space left on device` при zero-fill | диск реально полон | cleanup, `df -h /` |
| После заливки: kernel panic / не грузится | битый образ / прерванный dd | проверить sha256; повторить заливку |
| rootfs не расширился (df ~1.5G) | в образе `armbian-resize-filesystem` **disabled** (донор уже прошёл firstrun) | пересобрать образ (`stream-after-cleanup` включает resize); на уже прошитой плате: `/usr/lib/armbian/armbian-resize-filesystem start` или `/usr/local/sbin/sa02m-rootfs-expand.sh start` |
| SSH: тот же host key на двух платах | cleanup не делали перед образом | на клоне: `rm /etc/ssh/ssh_host_*; ssh-keygen -A; systemctl restart ssh` |
| `sha256 mismatch` в flash-receiver | повреждён файл на носителе | перекопировать образ + .sha256 |
| `xz: Cannot allocate memory` на приёмнике | 512 MiB RAM мало для xz -9 | xz -dc использует ~50–100 MiB — обычно OK; закрыть лишние процессы |
| WSL sudo зависает | IDE terminal | Windows Terminal → Ubuntu |
| vfat: cannot create file > 4G | запись raw.img на SD | использовать stream на хост (make-image.sh) |

### Команды диагностики на доноре

```bash
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,UUID
fdisk -l /dev/mmcblk2
df -hT
du -hxd1 / | sort -hr | head -15
systemctl is-enabled armbian-resize-filesystem.service
test -f /root/.not_logged_in_yet && echo FIRSTRUN_FLAG_SET || echo FIRSTRUN_FLAG_CLEAR
lsmod | grep g_mass_storage
```

### Команды диагностики на хосте

```bash
xz -l out/sa02m-*-shrunk.img.xz          # информация о сжатом файле
sha256sum -c out/sa02m-*-shrunk.img.xz.sha256
sudo fdisk -l out/sa02m-*-shrunk.img     # разметка внутри образа (после xz -d -k)
```

---

## 16. FAQ

**Можно ли снять образ без cleanup?**  
Да (`--no-cleanup`), но образ будет ~2–3× больше и будет содержать gcc, кэш apt и мусор в `/root`.

**Нужно ли перезагружать донор после cleanup?**  
Нет. Сразу zero-fill → dd stream.

**Можно ли использовать PiShrink прямо на SA-02m?**  
Теоретически да (если установить зависимости и хватит RAM/места), но **не рекомендуется**: PiShrink монтирует loop и изменяет **сам загрузочный диск**. Безопаснее делать на хосте.

**Сохранится ли U-Boot при shrunk-образе?**  
Да. PiShrink обрезает файл **после** последнего раздела, не трогая секторы 0…2047 с SPL.

**Можно ли залить shrunk-образ на eMMC 4 GiB?**  
Только если shrunk.img **< 4 GiB**. После cleanup типичный shrunk ~1.5 GiB — влезет.

**Что если на приёмнике уже есть данные?**  
`dd of=/dev/mmcblk2` **полностью перезаписывает** eMMC. Все данные на приёмнике будут уничтожены.

**Как обновить только web-проект без полного образа?**  
Используйте [`install.sh`](../install.sh) или [`scripts/update-www-only.sh`](../scripts/update-www-only.sh) — полный образ нужен только для **новых плат** или полной переустановки ОС.

**Донор после снятия образа — рабочий?**  
Да, но **без gcc/dkms** (если был cleanup). SSH host keys удалены — при следующем ssh клиент может ругаться; на доноре выполните `ssh-keygen -A` или перезагрузите (если не трогали firstrun flag для донора intentionally).

> **Примечание:** cleanup ставит `/root/.not_logged_in_yet` — при **следующей перезагрузке самого донора** Armbian выполнит firstrun/resize. Если донор должен остаться «как был» без firstrun — после снятия образа удалите флаг: `rm /root/.not_logged_in_yet`.

---

## 17. Ссылки и материалы

| Ресурс | URL |
|---|---|
| PiShrink | https://github.com/Drewsif/PiShrink |
| Habr: массовая прошивка AntexGate (PiShrink + xz) | https://habr.com/ru/articles/1024312/ |
| Armbian resize service | `/lib/systemd/system/armbian-resize-filesystem.service` на устройстве |
| Сборка драйвера на устройстве | [`MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md`](../MPLC_CYNTRON_DRIVER_BUILD_ON_DEVICE.md) |
| SSH-доступ SA-02m | [`docs/SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md`](SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md) |
| Основной README проекта | [`README.md`](../README.md) |
| SSH / serial расследование | [`SA02M_SSH_SERIAL_INVESTIGATION_1.0.3.3.md`](SA02M_SSH_SERIAL_INVESTIGATION_1.0.3.3.md) |
| Шаблон manifest | [`tools/imaging/manifest.example.json`](../tools/imaging/manifest.example.json) |

---

## 18. Профили sa02m-1eth / sa02m-2eth

Golden-образ **фиксирует** карту UART/COM. Для разных изделий нужны **отдельные образы** или явная перенастройка после заливки.

| Параметр | sa02m-1eth (СА-02м) | sa02m-2eth (СА-02м-2) |
|---|---|---|
| Ethernet | 1× eth0 | 2× eth0 + eth1 |
| RS-485 / COM | **5** портов | **4** порта |
| DO (дискретный выход) | есть | нет |
| Файл профиля | `/etc/sa02m_serial_profile.conf` | то же |
| Карта портов | `/etc/sa02m_serial_map.conf` | то же |

### Как зафиксировать профиль на доноре

```bash
# Перед install.sh или вручную:
echo 'SA02M_SERIAL_PROFILE=sa02m-1eth' > /etc/sa02m_serial_profile.conf
# или
sudo ./install.sh --ip 192.168.1.136 --pass cyntron --serial-profile sa02m-1eth
```

### Опасность автоопределения

На стенде с **двумя Ethernet** (eth1 присутствует) установщик может ошибочно выбрать `sa02m-2eth`, даже если изделие **1eth**. На доноре для образа **1eth** профиль задавайте **явно** — см. [`SA02M_SSH_SERIAL_INVESTIGATION_1.0.3.3.md`](SA02M_SSH_SERIAL_INVESTIGATION_1.0.3.3.md).

### Автодетект варианта (универсальный образ)

Начиная с версии 1.0.3.22 образ и установщик **универсальны**: вариант определяется автоматически по числу физических Ethernet-интерфейсов (`/sys/class/net/end*/device`).

#### Приоритет определения

1. Переменная окружения `SA02M_HW_VARIANT=sa02m-1eth|sa02m-2eth`
2. Файл `/etc/sa02m_hw_variant.conf` (строка `SA02M_HW_VARIANT=…`)
3. Автодетект: ≥2 физических `end*` → `sa02m-2eth`, иначе `sa02m-1eth`

#### Фиксация варианта при установке

```bash
# SA-02m (1 Ethernet):
sudo ./install.sh --variant sa02m-1eth

# SA-02m-2 (2 Ethernet):
sudo ./install.sh --variant sa02m-2eth
```

#### Ручная запись конфига на устройстве

```bash
echo 'SA02M_HW_VARIANT=sa02m-1eth' > /etc/sa02m_hw_variant.conf  # донор 192.168.1.136
echo 'SA02M_HW_VARIANT=sa02m-2eth' > /etc/sa02m_hw_variant.conf  # SA-02m-2 192.168.1.113
```

#### Что настраивается автоматически по варианту

| Параметр | sa02m-1eth | sa02m-2eth |
|---|---|---|
| IP end0 | `192.168.1.136` | `192.168.0.136` |
| Шлюз | `192.168.1.1` | `192.168.0.1` |
| end1 | — | DHCP (metric 100) |
| COM-портов | 5 (ttyS0+S3+S4+S5+S7) | 4 (ttyS3+S4+S5+S7) |

#### Шаблон конфига

Репозиторий содержит шаблон `/etc/sa02m_hw_variant.conf` → файл `etc/sa02m_hw_variant.conf` в корне репо. Устанавливается скриптами 01-system.sh.

### Именование образов

```
sa02m-1eth-v1.0.0-shrunk.img.xz
sa02m-2eth-v1.0.0-shrunk.img.xz
```

---

## 19. manifest.json релиза образа

Для каждого production-релиза образа храните **manifest** рядом с `.img.xz` и `.sha256`.

Шаблон: [`tools/imaging/manifest.example.json`](../tools/imaging/manifest.example.json)

### Обязательные поля

| Поле | Назначение |
|---|---|
| `image_name` | имя файла `.img.xz` |
| `image_sha256` | контрольная сумма (из `.sha256`) |
| `created_at` | ISO8601 UTC |
| `serial_profile` | `sa02m-1eth` или `sa02m-2eth` |
| `sa02m_web_build.git_commit` | SHA деплоя web-build |
| `partitions.root.uuid` | UUID root для проверки после заливки |

### Заполнение после make-image.sh

`make-image.sh` **автоматически** создаёт `…-shrunk.manifest.json` рядом с образом (шаг 6). Поля `image_sha256`, `serial_profile`, `sa02m_web_build.git_commit` заполняются с донора.

Ручная правка — только если нужны доп. поля (`notes`, `expected_sizes`):

```bash
cd tools/imaging/out
# отредактировать sa02m-1eth-v1.0.0-shrunk.manifest.json
```

### Хранение

- NAS / GitLab artifacts / Яндекс.Диск;
- отдельный каталог на `cyntron.ru/upload/…` (аналог репозитория прошивок MR-02m);
- **не** коммитить большие `.img.xz` в git — только manifest + sha256 + ссылка на артефакт.

---

## 20. SSH с Windows и автоматизация

### Ключ и путь

| | |
|---|---|
| Приватный ключ (repo) | `private/.ssh/sa02m_sa02` |
| Публичный | `private/.ssh/sa02m_sa02.pub` |
| Донор (эталон) | `root@192.168.1.136` |
| Пароль (если ключ не настроен) | `cyntron` |

**PowerShell:**

```powershell
$Key = "C:\Users\admin\Downloads\SA-02m-web-build\private\.ssh\sa02m_sa02"
ssh -i $Key root@192.168.1.136 "uname -a; df -h /"
```

**WSL2 (для make-image.sh):**

```bash
cp /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 ~/.ssh/
chmod 600 ~/.ssh/sa02m_sa02
```

### Рекомендации для скриптов

| Практика | Зачем |
|---|---|
| `ConnectTimeout=10`, `ServerAliveInterval=30` | не зависать при обрыве |
| Короткие команды вместо mega one-liner | меньше ложных таймаутов на Windows |
| `BatchMode=yes` для CI | без интерактива |
| Не запускать `journalctl -f`, `dmesg -w` по SSH | риск зависания платы — см. SSH doc |

### Полный цикл с Windows

1. **WSL2 Ubuntu** — установить PiShrink и зависимости (§7.1).
2. `cd /mnt/c/.../tools/imaging && ./make-image.sh …`
3. Артефакты в `out/` на диске `C:`.
4. Скопировать `shrunk.img.xz` + `.sha256` + `flash-receiver.sh` на USB для приёмников.

---

## 21. Roadmap (следующие этапы)

### Этап 1 — MVP (текущий)

- [x] `cleanup-donor.sh`, `make-image.sh`, `flash-receiver.sh`, `prepare-flash-media.sh`
- [x] Документация `SA02M_IMAGING_GUIDE.md`
- [x] Автоматический `*.manifest.json` в `make-image.sh`
- [ ] Пробная заливка на вторую плату + QA по §14.4

### Этап 2 — производство

- [ ] Отдельные golden для `sa02m-1eth` и `sa02m-2eth`
- [ ] Инструкция для оператора (1 страница: USB → питание → QA)
- [ ] Post-flash autotest (paramiko + чек-лист §14.4)
- [ ] Версия образа в веб-UI «Управление» (опционально)

### Этап 3 — масштаб (аналог Habr / CM Provision)

- [ ] Provision-хост SA-02m: HTTP-каталог `.img.xz`, batch по подсети
- [ ] **Не** использовать CM Provision (только Raspberry CM4/CM5)
- [ ] DHCP + скрипт first-boot для скачивания образа по URL (для партий 20+)

---

## История документа

| Версия | Дата | Изменения |
|---|---|---|
| 1.0 | 2026-05-20 | Первоначальная версия: диагностика 192.168.1.136, скрипты tools/imaging/, PiShrink pipeline |
| 1.1 | 2026-05-20 | Аудит эталона, профили 1eth/2eth, manifest, SSH/Windows, roadmap |
| 1.2 | 2026-05-20 | `prepare-flash-media.sh`; make-image: `--profile`, `--version`, auto manifest |
| 1.3 | 2026-06-02 | Универсальный образ SA-02m/SA-02m-2: автодетект варианта, `--variant`, auto IP/GW |
