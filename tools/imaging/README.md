# SA-02m — снятие компактного образа eMMC

Утилиты для **снятия**, **уменьшения** и **тиражирования** образа SA-02m (Armbian + web-проект).

| | |
|---|---|
| **Полное руководство** | [`docs/SA02M_IMAGING_GUIDE.md`](../../docs/SA02M_IMAGING_GUIDE.md) |
| **Заменяет** | `dd if=/dev/mmcblk2 of=/mnt/sdcard.img` (7.28 GiB) |
| **Результат** | `sa02m-*-shrunk.img.xz` ~**350–500 MiB** + `.sha256` + `.manifest.json` |

---

## Быстрый старт

### 1. Хост (один раз): WSL2 + сеть + PiShrink

**Локальная сеть:** один раз от администратора:

```powershell
powershell -ExecutionPolicy Bypass -File tools\imaging\setup-wsl-network.ps1
```

(зеркальная сеть WSL2 — см. [§7.4 в SA02M_IMAGING_GUIDE.md](../../docs/SA02M_IMAGING_GUIDE.md#74-доступ-wsl2-к-локальной-сети-обязательно-для-ssh-на-донор))

```bash
sudo apt update && sudo apt install -y kpartx parted util-linux e2fsprogs xz-utils wget openssh-client python3
sudo wget -O /usr/local/bin/pishrink.sh https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
sudo chmod +x /usr/local/bin/pishrink.sh

mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 ~/.ssh/
chmod 600 ~/.ssh/sa02m_sa02
```

### Учётки устройства (в репозитории)

Файл **[`tools/sa02m-device.env`](../sa02m-device.env)** — единый источник:

| | |
|---|---|
| SSH | `root` / `cyntron` @ `192.168.1.136` |
| Веб | `admin` / `cyntron` @ `:9999` |

Скрипты `capture-image.*`, `make-image.sh`, `sa02m_remote.py` читают этот файл (переопределение — переменные окружения `SA02M_*`).

### 2. Снять образ (полный цикл) → `.img`

**Windows (PowerShell):**

```powershell
cd C:\Users\admin\Downloads\SA-02m-web-build
.\tools\imaging\capture-image.ps1
# или: .\tools\imaging\capture-image.ps1 -Name SA-02m-lab -Ip 192.168.1.136
```

**WSL2 / Linux:**

```bash
cd /mnt/c/Users/admin/Downloads/SA-02m-web-build/tools/imaging
chmod +x *.sh
./capture-image.sh --ip 192.168.1.136 --name SA-02m-20260730
```

**Выход в `out/`:**

- `SA-02m-YYYYMMDD.img` ← для ImageUSB / переноса
- `SA-02m-YYYYMMDD.img.xz` + `.sha256` + `.manifest.json`

Релизный вариант (только xz, без сырого имени):

```bash
./make-image.sh --ip 192.168.1.136 --key ~/.ssh/sa02m_sa02 --out-dir ./out \
    --profile sa02m-1eth --version 1.0.0 --keep-raw-img
```

### 3. Подготовить носитель / образ для приёмника

**Вариант A — USB + flash-receiver (цех, устройство само пишет eMMC):**

```bash
./prepare-flash-media.sh \
    --image ./out/sa02m-1eth-v1.0.0-shrunk.img.xz \
    --dest /mnt/c/USB/SA02m
```

На носителе: `sa02m-shrunk.img.xz`, `.sha256`, `flash-receiver.sh`, `autorun.sh` → symlink.

**Вариант B — ImageUSB (Windows, FEL/USB → запись .img на eMMC):**

```bash
./prepare-imageusb.sh \
    --image ./out/sa02m-1eth-v1.0.0-shrunk.img.xz \
    --dest /mnt/c/Users/admin/Downloads/SA02m-imageusb
```

На ПК: распакованный `.img` + `.sha256` + `IMAGEUSB.txt`. **xz распаковывается на хосте**, ImageUSB пишет `.img` напрямую.

**Вариант C — buildroot / USB-host + `sdcard.img` (ручной autorun):**

На флешке: `sdcard.img` (PiShrink `.img`, не `.xz`) + `boot.scr` + [`autorun.sh`](autorun.sh) (= [`autorun-fel.sh`](autorun-fel.sh)).

- BusyBox: **без** `status=progress`
- сам монтирует `/dev/sda1` → `/mnt`, пишет `dd` в `/dev/mmcblk2`
- после `dd` дополнительно пишет `boot.scr` на FAT p1 (с `threadirqs` для защиты
  от I2C/PCA9536 IRQ storm на рабочей плате)
- на новом rootfs включает только first-boot wiring: single resize, cloud wipe,
  watchdog units без permanent mask

С ПК (флешка вставлена в ПК):

```powershell
Copy-Item tools\imaging\autorun.sh E:\autorun.sh   # буква флешки
# рядом должны быть sdcard.img и boot.scr
```

На плате (buildroot, COM, root/root) — autorun **не** стартует сам:

```sh
mount /dev/sda1 /mnt
sh /mnt/autorun.sh
```

> ⚠ **Не** заливать только `.img` без first-boot patch. Патч обязан проверить
> rootfs resize wiring **и FAT `boot.scr` с `threadirqs`**. Без `threadirqs`
> рабочая плата с PCA9536 может зависнуть на I2C IRQ storm до `sa02m-pre-start`
> (нет пищалки, службы не стартуют).

### 4. Залить на новую плату

| Режим | Действие | Первая загрузка |
|---|---|---|
| flash-receiver | USB → `flash-receiver.sh` → reboot | rootfs ~7G (sa02m-rootfs-expand) |
| ImageUSB | ImageUSB → Write `.img` на eMMC | то же |

Проверка: `df -h /` → **Size ≈ 7.0G**, не 1.8G.

---

## Состав каталога

| Файл | Где | Назначение |
|---|---|---|
| [`cleanup-donor.sh`](cleanup-donor.sh) | донор (ssh) | фазы 1–4: мусор, тулчейн, apt/logs (**без** сброса ssh keys) |
| [`stream-after-cleanup.sh`](stream-after-cleanup.sh) | донор (одна ssh-сессия) | zerofill → id reset → dd по **Ethernet/SSH** |
| [`serial-restore-ssh.py`](serial-restore-ssh.py) | хост (COM7 115200) | **только** восстановление sshd; образ по serial **не** передаётся |
| [`fix-donor-after-abort.sh`](fix-donor-after-abort.sh) | донор (ssh/serial) | после прерванного снятия: kill dd, machine-id, ssh keys, сервисы |
| [`restore-donor-ssh.sh`](restore-donor-ssh.sh) | донор | обёртка над `fix-donor-after-abort.sh` |
| [`make-image.sh`](make-image.sh) | хост WSL2 | cleanup → stream по ssh → PiShrink → manifest |
| [`prepare-flash-media.sh`](prepare-flash-media.sh) | хост | упаковка USB для flash-receiver |
| [`prepare-imageusb.sh`](prepare-imageusb.sh) | хост | распаковка .img.xz → .img для ImageUSB (Windows) |
| [`flash-receiver.sh`](flash-receiver.sh) | приёмник | sha256 → `xz -dc \| dd /dev/mmcblk2` → reboot |
| [`autorun-fel.sh`](autorun-fel.sh) / [`autorun.sh`](autorun.sh) | USB + buildroot | `sdcard.img` → `dd` eMMC + watchdog mask до reboot |
| [`manifest.example.json`](manifest.example.json) | — | справочный шаблон (make-image пишет `.manifest.json`) |

---

## Параметры make-image.sh

```bash
./make-image.sh [--ip 192.168.1.136] [--key ~/.ssh/sa02m_sa02] \
                [--out-dir ./out] [--profile sa02m-1eth] [--version 1.0.0] \
                [--no-cleanup] [--no-zerofill] [--no-manifest] \
                [--xz-level 1] [--final-xz-level 9e]
```

---

## Документация

| Документ | Содержание |
|---|---|
| [**SA02M_IMAGING_GUIDE.md**](../../docs/SA02M_IMAGING_GUIDE.md) | полное руководство |
| [README §Способ 4](../../README.md#способ-4--тиражирование-компактного-образа-sa-02m-web) | краткая ссылка из основного README |

---

## Восстановление после сбоя

Если `make-image` прервался (ssh timeout, обрыв dd):

```bash
# WSL — полное восстановление донора
ssh -i ~/.ssh/sa02m_sa02 root@192.168.1.136 'bash -s' < fix-donor-after-abort.sh

# Windows serial (COM7) — то же через консоль
py -3 serial-restore-ssh.py COM7
```

IP донора по умолчанию **192.168.1.136** (не путать с IP хоста). Переопределение: `DEVICE_IP=192.168.1.136 ./capture-SA-02m_210526.sh`.

---

## Версия

**1.2** (2026-05-20) — `prepare-flash-media.sh`, auto manifest, `--profile` / `--version`.
