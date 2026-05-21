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

### 2. Снять образ (полный цикл)

```bash
cd /mnt/c/Users/admin/Downloads/SA-02m-web-build/tools/imaging
chmod +x *.sh
./make-image.sh --ip 192.168.1.136 --key ~/.ssh/sa02m_sa02 --out-dir ./out \
    --profile sa02m-1eth --version 1.0.0
```

**Выход в `out/`:**

- `sa02m-1eth-v1.0.0-shrunk.img.xz`
- `sa02m-1eth-v1.0.0-shrunk.img.xz.sha256`
- `sa02m-1eth-v1.0.0-shrunk.manifest.json`

### 3. Подготовить USB для приёмника

```bash
./prepare-flash-media.sh \
    --image ./out/sa02m-1eth-v1.0.0-shrunk.img.xz \
    --dest /mnt/c/USB/SA02m
```

На носителе: `sa02m-shrunk.img.xz`, `.sha256`, `flash-receiver.sh`, `autorun.sh` → symlink.

### 4. Залить на новую плату

Подключить USB → питание → `flash-receiver.sh` / `autorun.sh` → reboot → QA (§14.4 в guide).

---

## Состав каталога

| Файл | Где | Назначение |
|---|---|---|
| [`cleanup-donor.sh`](cleanup-donor.sh) | донор (ssh) | подготовка к снятию |
| [`make-image.sh`](make-image.sh) | хост WSL2 | cleanup → zero-fill → dd\|xz → PiShrink → manifest |
| [`prepare-flash-media.sh`](prepare-flash-media.sh) | хост | упаковка USB для flash-receiver |
| [`flash-receiver.sh`](flash-receiver.sh) | приёмник | sha256 → `xz -dc \| dd /dev/mmcblk2` → reboot |
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

## Версия

**1.2** (2026-05-20) — `prepare-flash-media.sh`, auto manifest, `--profile` / `--version`.
