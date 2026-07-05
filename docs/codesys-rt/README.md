# SA-02m: Оптимизация для CODESYS 4.20.0.0 — Полная документация

**Дата аудита:** 2026-06-05  
**Устройство:** SA-02m (Allwinner A40i / sun8i-r40, Armbian 26.2.1 / Ubuntu 24.04 Noble)  
**Цель:** Оптимальная работа CODESYS Control for Linux ARM SL 4.20.0.0 (armhf)

> ### 🆕 2026-07: миграция на `wirenboard/linux` 5.10.35 armhf
>
> RT-ядро для CODESYS теперь собирается через новый порт на форк
> Wiren Board (см. [`../../kernel-port/README.md`](../../kernel-port/README.md),
> [`../../tools/kernel-wb/README.md`](../../tools/kernel-wb/README.md)).
>
> Вместо ручного «скопировать zImage из Linux RT/» — стандартный
> `.deb`-пакет: `apt install linux-image-sa02m-rt`. Postinst-hook
> [`50-sa02m-fat-sync`](../../etc/kernel-postinst.d/50-sa02m-fat-sync)
> сам копирует новый zImage/DTB на FAT-раздел `mmcblk2p1`.
>
> Новые версии ядер:
> - `5.10.35-sa02m`     — SMP baseline
> - `5.10.35-sa02m-rt`  — PREEMPT_RT (36-й rt-патчсет)
>
> Legacy-таблица ниже (6.1.0-rc6-rt4) остаётся релевантной до
> валидации нового ядра на боевом устройстве.

---

## Статус готовности к CODESYS RT (на 2026-06-05)

| Компонент | Статус | Действие |
|---|---|---|
| RT-модули `6.1.0-rc6-rt4` | ✅ Установлены на устройстве | — |
| Кастомный DTB `sun8i-a40i-sk.dtb` | ✅ В FAT-разделе | — |
| `threadirqs` в boot.scr | ✅ Уже включён | — |
| `zImage` PREEMPT_RT | ❌ Не задеплоен | Скопировать из `Linux RT/zImage` |
| `isolcpus=3` в boot.scr | ❌ Нет | Обновить boot.scr |
| CPU governor `performance` | ❌ `schedutil` | Создать systemd unit |
| `vm.swappiness = 0` | ❌ = 100 | `/etc/sysctl.d/99-codesys-rt.conf` |
| CODESYS установлен | ❌ Нет | dpkg -i `.deb` из пакета |
| Температура | ⚠️ 62–63°C idle | Радиатор + trip_point 70°C |
| `sa02m-eth0-led` FAILED | ⚠️ Ошибка | `eth0` → `end0` в unit-файле |

**Итог: 3 компонента ✅, для полного RT нужно 5 быстрых шагов (~30 мин работы).**

---

## Содержание

1. [Инвентаризация системы](#1-инвентаризация-системы)
2. [Критические находки аудита](#2-критические-находки-аудита)
3. [Архитектура загрузки устройства](#3-архитектура-загрузки-устройства)
4. [Этапы сборки RT-ядра](#4-этапы-сборки-rt-ядра)
5. [Деплой ядра на устройство](#5-деплой-ядра-на-устройство)
6. [Установка CODESYS](#6-установка-codesys)
7. [Системный тюнинг для RT](#7-системный-тюнинг-для-rt)
8. [Устранение найденных ошибок](#8-устранение-найденных-ошибок)
9. [Чек-лист верификации](#9-чек-лист-верификации)
10. [Откат при сбое](#10-откат-при-сбое)

---

## 1. Инвентаризация системы

### Железо
| Параметр | Значение |
|---|---|
| SoC | Allwinner A40i (sun8i-r40, Cortex-A7 quad-core) |
| Частота CPU | 720–1200 MHz (сейчас: 720–912 MHz — schedutil throttling) |
| RAM | 512 MB (MemAvailable ~332 MB) |
| eMMC | 7 GB (`/dev/mmcblk2`), root занят на 20% |
| SD-карта | 30 GB (`/dev/mmcblk3`), смонтирована в `/media/sdcard` |
| Питание/cooling | Температура 62–63°C в idle — **требует проверки охлаждения** |

### ОС и ядро
| Параметр | Значение |
|---|---|
| ОС | Armbian 26.2.1 / Ubuntu 24.04 Noble (armhf) |
| Текущее ядро | **`6.1.0-rc6 #1 SMP`** (собрано 2026-06-03 13:31) — **без PREEMPT_RT** |
| RT-модули | `/lib/modules/6.1.0-rc6-rt4/` — **уже установлены** (120 модулей, собраны 2026-05-22) |
| Установленных пакетов | 581 (dpkg) |

### Разметка eMMC
```
/dev/mmcblk2p1  FAT16  64 MB   — boot-раздел (U-Boot читает отсюда)
/dev/mmcblk2p2  ext4   7.1 GB  — rootfs (/, /boot/*)
mmcblk2boot0/1          —       SPL + U-Boot (raw, не в разделах)
```

### Текущие сервисы (запущенных: 26)
- **MasterSCADA 4D** (`/opt/mplc4/`) — soft PLC, работает параллельно с SA-02m stack
- SA-02m stack: `mosquitto`, `sa02m-modbus-mqtt`, `sa02m-mqtt-opcua`, `sa02m-mqtt-snmp`, `sa02m-serial-gateway`, `sa02m-cloud-agent`, `nginx`, `chrony`
- `sa02m-userspace-watchdog`, `sa02m-failure-monitor`, `net-watchdog`
- CODESYS — **не установлен**

### Лицензия CODESYS
| Параметр | Значение |
|---|---|
| Пакет | `CODESYS Control for Linux ARM SL 4.20.0.0` (armhf `.deb`) |
| Версия | 4.20.0.0 (SDK 3.5.21.41) |
| Тип лицензии | Standard S |
| Ticket | `7PWFL-GKTKH-UM6EU-JUZXJ-N5MY5` |
| `3S.dat` | `/etc/3S.dat`, `/app/3S.dat` — **уже присутствуют** |
| Требуется IDE | CODESYS ≥ 3.5.17.0 |

---

## 2. Критические находки аудита

### 2.1 Нет PREEMPT_RT — главный приоритет

Ядро `6.1.0-rc6 #1 SMP` без RT-патча. CODESYS требует детерминизма цикла:

| Ядро | Типичный jitter | Для CODESYS |
|---|---|---|
| SMP (текущее) | 10–50 мс | Неприемлемо |
| PREEMPT_RT | < 100 мкс | Оптимально |

**Уже готово для деплоя RT:**
- `/lib/modules/6.1.0-rc6-rt4/` — 120 `.ko` модулей установлены (May 22)
- `/mnt/boot_fat/sun8i-a40i-sk.dtb` — кастомный DTB для SA-02m уже в FAT-разделе
- `threadirqs` уже прописан в `boot.scr` (threadirqs = базовое требование RT)
- **Не хватает только:** зкомпилированного `zImage` с RT-патчем

> **Вывод:** ядро RT уже почти готово — только одна сборка zImage отделяет от полноценного RT.

### 2.2 CPU governor `schedutil` → 12.6% CPU впустую

`sugov:0` (schedutil frequency governor kernel thread) потребляет >12% CPU непрерывно. При RT-ядре и governor `performance` этот процесс исчезает полностью.

### 2.3 Температура 62–63°C без нагрузки CODESYS

Cortex-A7 throttle-порог ~80–85°C. При добавлении ПЛК-нагрузки + governor `performance` возможен throttle → нестабильный цикл.  
**Требуется:** физический радиатор на A40i (если не установлен) + программный trip-point 70°C.

### 2.4 `vm.swappiness = 100` при отсутствии swap

Ядро агрессивно пытается evict страницы при swappiness=100, добавляя латентность. Swap не настроен вообще — параметр полностью бессмысленный. Необходимо `vm.swappiness = 0`.

### 2.5 MasterSCADA 4D работает параллельно

`/opt/mplc4/mplc_daemon` + `mplc_monitor` + собственный nginx. За 5 часов потребил 14 мин CPU. При запуске CODESYS необходимо решить: работают вместе или CODESYS заменяет MasterSCADA?

> **Рекомендация:** запустить CODESYS на отдельном изолированном CPU3, MasterSCADA — на CPU0-2. Если CODESYS должен заменить MasterSCADA полностью — отключить `mplc4.service`.

### 2.6 Сломанные симлинки в `/boot/` (ext4) — НЕ КРИТИЧНО

`/boot/zImage → vmlinuz-6.12.58-current-sunxi` (файл не существует). Это **не влияет на загрузку** — U-Boot читает из FAT-раздела `/dev/mmcblk2p1`, не из ext4 `/boot/`. Симлинки — артефакт Armbian apt-пакетов.

---

## 3. Архитектура загрузки устройства

```
eMMC (/dev/mmcblk2):
  Sector 0–2047     → U-Boot SPL + U-Boot (raw, вне разделов)
  /dev/mmcblk2p1    → FAT16 64MB (BOOT-РАЗДЕЛ)
  /dev/mmcblk2p2    → ext4 7GB (rootfs)
```

**Последовательность загрузки:**
```
ROM bootloader
  └─ U-Boot SPL (mmcblk2boot0)
       └─ U-Boot (mmcblk2, raw offset)
            └─ читает FAT16 (/dev/mmcblk2p1):
                 ├─ boot.scr         ← U-Boot скрипт
                 ├─ zImage           ← ядро Linux
                 └─ sun8i-a40i-sk.dtb ← кастомный DTB SA-02m
```

**Содержимое текущего `/dev/mmcblk2p1` (FAT16):**
```
zImage                    6.3 MB   (6.1.0-rc6 SMP, built 2026-06-03 16:32)
sun8i-a40i-sk.dtb         31 KB    (кастомный DTB SA-02m, built 2026-06-03 15:43)
sun8i-a40i-sk.dtb.bak     32 KB    (резервная копия)
zImage.bak-               6.4 MB   (резервная копия предыдущего ядра)
boot.scr                  261 B    (текущий U-Boot скрипт)
boot.scr.bak              244 B    (резервная копия скрипта)
```

**Текущий `boot.scr`:**
```bash
setenv bootargs root=/dev/mmcblk2p2 rootwait threadirqs
mmc dev 1
fatload mmc 1 ${kernel_addr_r} zImage
fatload mmc 1 ${fdt_addr_r} sun8i-a40i-sk.dtb
bootz ${kernel_addr_r} - ${fdt_addr_r}
```

> `threadirqs` — **уже включено**, это базовое требование PREEMPT_RT. Хороший знак.

**Вывод для деплоя:** чтобы загрузить RT-ядро, достаточно скопировать новый `zImage` в FAT-раздел. DTB и boot.scr уже готовы.

---

## 4. Деплой RT-ядра (готовый образ — сборка НЕ нужна)

### Состояние готовности

Аудит папки `Сборка линукс/Linux RT/` показал:

| Артефакт | Расположение | Статус |
|---|---|---|
| `zImage` (6.1.0-rc6-rt4, ARMv7) | `Linux RT/zImage` | **Готов**, собран 2026-03-25 |
| Модули 118× `.ko` | `Linux RT/6.1.0-rc6-rt4.zip` | Те же что на устройстве |
| Модули на устройстве | `/lib/modules/6.1.0-rc6-rt4/` | **Уже установлены** (120 шт.) |
| DTB `sun8i-a40i-sk.dtb` | FAT `/dev/mmcblk2p1` | **Уже в FAT-разделе** |
| `threadirqs` в boot.scr | FAT `/dev/mmcblk2p1/boot.scr` | **Уже включён** |

**Vermagic модулей на устройстве:**
```
6.1.0-rc6-rt4 SMP preempt_rt mod_unload ARMv7 p2v8
```
Совпадает с модулями из `Linux RT/6.1.0-rc6-rt4.zip` — **одна и та же сборка**.

> **Сборка ядра не требуется.** Готовый `zImage` нужно просто скопировать в FAT-раздел.

---

### 4.1 Быстрый деплой (рекомендуется, ~5 минут)

```powershell
# PowerShell на Windows:
# 1. Скопировать RT zImage на устройство:
$KEY = "C:\Users\admin\Downloads\SA-02m-web-build\private\.ssh\sa02m_sa02"
$ZIMAGE = "C:\Users\admin\YandexDisk\ЦИНТРОН\Сборка линукс\Linux RT\zImage"
scp -i $KEY $ZIMAGE root@192.168.1.136:/tmp/zImage-rt4
```

```bash
# SSH на устройстве:
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  mkdir -p /mnt/boot_fat
  mount /dev/mmcblk2p1 /mnt/boot_fat

  # Резервная копия SMP-ядра:
  cp /mnt/boot_fat/zImage \"/mnt/boot_fat/zImage.bak-smp-\$(date +%Y%m%d)\"

  # Установить RT-ядро:
  cp /tmp/zImage-rt4 /mnt/boot_fat/zImage

  sync
  umount /mnt/boot_fat
  echo 'RT kernel deployed. Ready to reboot.'
"
```

### 4.2 Обновление boot.scr (добавить isolcpus)

Текущий `boot.scr` не содержит `isolcpus`. Добавляем для изоляции CPU3 под CODESYS.

```bash
# На Windows:
# Сначала установить u-boot-tools в WSL2 или использовать python-mkimage
wsl sudo apt install -y u-boot-tools
```

```bash
# В WSL2 (или Linux):
cat > /tmp/boot.cmd << 'EOF'
setenv bootargs root=/dev/mmcblk2p2 rootwait threadirqs isolcpus=3 nohz_full=3 rcu_nocbs=3
mmc dev 1
fatload mmc 1 ${kernel_addr_r} zImage
fatload mmc 1 ${fdt_addr_r} sun8i-a40i-sk.dtb
bootz ${kernel_addr_r} - ${fdt_addr_r}
EOF

mkimage -C none -A arm -T script -d /tmp/boot.cmd /tmp/boot.scr

# Деплой:
scp -i /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 \
    /tmp/boot.scr root@192.168.1.136:/tmp/boot.scr-new
```

```bash
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  mount /dev/mmcblk2p1 /mnt/boot_fat
  cp /mnt/boot_fat/boot.scr /mnt/boot_fat/boot.scr.bak-pre-rt
  cp /tmp/boot.scr-new /mnt/boot_fat/boot.scr
  sync && umount /mnt/boot_fat
"
```

### 4.3 Перезагрузка и проверка

```bash
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "reboot"
sleep 35
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "uname -v; cat /sys/devices/system/cpu/isolated"
# Ожидаемо:
# #1 SMP PREEMPT_RT ...
# 3
```

---

### 4.4 Если нужна пересборка ядра (на будущее / изменение конфига)

Сборка через **Buildroot** (как описано в `Сборка линукс/Linux RT/Сборка ядра на ВМ.txt`) или **WSL2 cross-compile**:

**Среда сборки:**

| Вариант | Время | Сложность | Рекомендация |
|---|---|---|---|
| **WSL2 Ubuntu 22.04** | ~45–90 мин | Низкая | **Рекомендуется** |
| Buildroot VM (Linux) | ~60–120 мин | Средняя | По инструкции в txt |
| На самом устройстве | ~8–16 часов | Высокая | Не рекомендуется |

**Инструкция WSL2 cross-compile (краткая):**

```bash
# Подготовка WSL2:
sudo apt update && sudo apt install -y \
    gcc-arm-linux-gnueabihf bc libssl-dev libelf-dev flex bison make wget

# Исходники ядра:
wget https://cdn.kernel.org/pub/linux/kernel/v6.x/testing/linux-6.1-rc6.tar.xz
tar xf linux-6.1-rc6.tar.xz && cd linux-6.1-rc6

# RT патч (URL из Сборка ядра на ВМ.txt):
wget -O - https://cdn.kernel.org/pub/linux/kernel/projects/rt/6.1/older/patch-6.1-rc6-rt4.patch.gz \
    | zcat | patch -p1

# Конфигурация:
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- sunxi_defconfig
scripts/config --enable PREEMPT_RT --enable HZ_1000 --enable NO_HZ_FULL \
               --disable DEBUG_PREEMPT --disable PROVE_LOCKING --disable LOCKDEP
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- olddefconfig

# Сборка (45–90 мин):
make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- -j$(nproc) zImage modules

# Результат: arch/arm/boot/zImage
```

**Menuconfig параметры** (скриншот `Настройки для сборки.jpg`):
- `CONFIG_PREEMPT_RT` = y (Fully Preemptible Kernel)
- `CONFIG_HIGH_RES_TIMERS` = y
- `CONFIG_NO_HZ_FULL` = y
- `CONFIG_HZ_1000` = y
- `CPU_FREQ_DEFAULT_GOV_PERFORMANCE` = y

---

## 5. Деплой ядра на устройство

### 5.1 Копирование zImage на FAT-раздел

```bash
# С WSL2 (Windows) напрямую через SCP:
scp -i /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 \
    arch/arm/boot/zImage \
    root@192.168.1.136:/tmp/zImage-rt4

# На устройстве — монтировать FAT и скопировать:
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  # Создать резервную копию текущего ядра:
  mkdir -p /mnt/boot_fat && mount /dev/mmcblk2p1 /mnt/boot_fat
  cp /mnt/boot_fat/zImage /mnt/boot_fat/zImage.bak-smp-$(date +%Y%m%d)

  # Скопировать RT-ядро:
  cp /tmp/zImage-rt4 /mnt/boot_fat/zImage

  # Sync и umount:
  sync && umount /mnt/boot_fat
  echo 'Kernel deployed. Ready to reboot.'
"
```

### 5.2 Обновление boot.scr с isolcpus

Добавить `isolcpus=3` для изоляции CPU3 под CODESYS:

```bash
# Создать новый boot.cmd:
cat > /tmp/boot.cmd << 'EOF'
setenv bootargs root=/dev/mmcblk2p2 rootwait threadirqs isolcpus=3 nohz_full=3 rcu_nocbs=3
mmc dev 1
fatload mmc 1 ${kernel_addr_r} zImage
fatload mmc 1 ${fdt_addr_r} sun8i-a40i-sk.dtb
bootz ${kernel_addr_r} - ${fdt_addr_r}
EOF

# Скомпилировать в U-Boot script (mkimage):
mkimage -C none -A arm -T script -d /tmp/boot.cmd /tmp/boot.scr

# Деплой на устройстве:
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  mount /dev/mmcblk2p1 /mnt/boot_fat
  cp /mnt/boot_fat/boot.scr /mnt/boot_fat/boot.scr.bak-pre-rt
  # Скопировать новый boot.scr
  sync && umount /mnt/boot_fat
"
scp -i private/.ssh/sa02m_sa02 /tmp/boot.scr root@192.168.1.136:/tmp/
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  mount /dev/mmcblk2p1 /mnt/boot_fat
  cp /tmp/boot.scr /mnt/boot_fat/boot.scr
  sync && umount /mnt/boot_fat
"
```

> `mkimage` входит в пакет `u-boot-tools`: `sudo apt install u-boot-tools`

### 5.3 Перезагрузка и проверка

```bash
# Перезагрузка:
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "reboot"

# Подождать ~30 секунд, затем проверить:
sleep 35
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "uname -v"
# Ожидаемый вывод: ... PREEMPT_RT ...
```

---

## 6. Установка CODESYS

### 6.1 Распаковка пакета

`.package` файл — это ZIP-архив.

```powershell
# Windows PowerShell:
$pkg = "C:\Users\admin\YandexDisk\ЦИНТРОН\Сборка линукс\cds\Лицензия\CODESYS Control for Linux ARM SL 4.20.0.0.package"
$out = "C:\Temp\codesys-arm-sl"
New-Item -ItemType Directory -Force -Path $out
Expand-Archive -Path $pkg -DestinationPath $out
# Найти .deb:
Get-ChildItem $out -Recurse -Filter "*.deb"
```

Ожидаемые файлы:
```
codesyscontrol_linuxarm_4.20.0.0_armhf.deb  (~30 MB)
codesyscontrol_linuxarm_4.20.0.0_armhf.ipk
codesyscontrol.devdesc.xml
```

### 6.2 Копирование и установка на устройстве

```powershell
# Скопировать .deb на устройство:
scp -i private\.ssh\sa02m_sa02 `
    "C:\Temp\codesys-arm-sl\codesyscontrol_linuxarm_4.20.0.0_armhf.deb" `
    root@192.168.1.136:/tmp/
```

```bash
# На устройстве:
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  dpkg -i /tmp/codesyscontrol_linuxarm_4.20.0.0_armhf.deb
  systemctl enable codesyscontrol
"
```

### 6.3 Настройка CPU affinity в конфиге CODESYS

Найти конфиг CODESYS после установки:
```bash
find / -name 'CODESYSControl*.cfg' 2>/dev/null
# Обычно: /etc/CODESYSControl_User.cfg или /var/opt/codesys/CODESYSControl_User.cfg
```

Добавить/изменить секцию `[SysProcess]`:
```ini
[SysProcess]
; Привязать CODESYS runtime к изолированному CPU3:
CpuAffinity=3
; Приоритет планировщика SCHED_FIFO:
SchedulerPriority=80
```

### 6.4 Лицензия

```bash
# Убедиться что 3S.dat присутствует:
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "ls -la /etc/3S.dat"
# Если нет — скопировать из cds/etc/3S.dat:
# scp -i private/.ssh/sa02m_sa02 cds/etc/3S.dat root@192.168.1.136:/etc/
```

Активация лицензии Standard S выполняется из CODESYS IDE (Windows):
- Ticket: `7PWFL-GKTKH-UM6EU-JUZXJ-N5MY5`
- Device → Communication → Activate license

### 6.5 Запуск

```bash
ssh -i private/.ssh/sa02m_sa02 root@192.168.1.136 "
  systemctl start codesyscontrol
  systemctl status codesyscontrol
"
```

---

## 7. Системный тюнинг для RT

### 7.1 CPU governor → performance

Создать systemd service для постоянного применения:

```bash
# Файл: /etc/systemd/system/cpu-governor.service
cat << 'EOF' > /etc/systemd/system/cpu-governor.service
[Unit]
Description=Set CPU governor to performance
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now cpu-governor.service
```

Проверка:
```bash
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
# Ожидаемо: performance (×4)
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
# Ожидаемо: 1200000 (1200 MHz)
```

### 7.2 sysctl RT tuning

```bash
cat << 'EOF' > /etc/sysctl.d/99-codesys-rt.conf
# Отключить выгрузку страниц (нет swap → значение бессмысленно, но влияет на eviction heuristics)
vm.swappiness = 0

# Снять ограничение на RT-процессы (без этого CODESYS не может занять >95% CPU в SCHED_FIFO)
kernel.sched_rt_runtime_us = -1

# Тюнинг гранулярности планировщика для RT:
kernel.sched_min_granularity_ns = 100000
kernel.sched_wakeup_granularity_ns = 500000

# Сетевой тюнинг (для Modbus/OPC-UA latency):
net.core.busy_read = 50
net.core.busy_poll = 50
net.ipv4.tcp_low_latency = 1
EOF

sysctl --system
```

### 7.3 IRQ affinity (применять после `isolcpus=3`)

После загрузки с `isolcpus=3` перенести все прерывания на CPU0-2:

```bash
cat << 'EOF' > /usr/local/sbin/irq-affinity.sh
#!/bin/bash
# Назначить все IRQ на CPU0-2 (bitmask 0b0111 = 7), CPU3 оставить для CODESYS
for irq_dir in /proc/irq/*/; do
    irq=$(basename "$irq_dir")
    [ "$irq" = "0" ] && continue   # skip spurious
    echo 7 > "/proc/irq/${irq}/smp_affinity" 2>/dev/null || true
done
echo "IRQ affinity set: CPU0-2 only"
EOF

chmod +x /usr/local/sbin/irq-affinity.sh
```

Добавить вызов в `cpu-governor.service`:
```ini
ExecStart=/bin/bash -c "echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
ExecStart=/usr/local/sbin/irq-affinity.sh
```

### 7.4 Thermal trip point

Снизить порог дросселирования до 70°C (вместо дефолтного 85°C):

```bash
cat << 'EOF' > /etc/systemd/system/thermal-tune.service
[Unit]
Description=Set thermal throttle threshold to 70°C
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c "\
  for zone in /sys/class/thermal/thermal_zone*/trip_point_1_temp; do \
    echo 70000 > \$zone 2>/dev/null || true; \
  done"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now thermal-tune.service
```

### 7.5 Удаление ненужных пакетов

Освобождает ~80–120 MB и снижает фоновую нагрузку:

```bash
# Build toolchain (не нужен на production):
apt remove --purge -y \
    gcc g++ cpp \
    make cmake \
    autoconf automake libtool \
    bison flex \
    libncurses-dev libssl-dev libelf-dev \
    git binutils

# Очистка:
apt autoremove --purge -y
apt clean
```

> **Проверить перед удалением:** что в `/opt/mplc4/` и `sa02m-*` сервисах нет компиляции на лету. Судя по бинарникам boost в `/opt/mplc4/` — компиляция не нужна.

---

## 8. Устранение найденных ошибок

### 8.1 sa02m-eth0-led: FAILED

**Причина:** сервис ищет интерфейс `eth0`, но Linux 24.04 переименовал его в `end0`.

```bash
# Найти файл сервиса:
find /etc/systemd /lib/systemd -name 'sa02m-eth0-led*' 2>/dev/null

# Заменить eth0 на end0 в unit-файле и скрипте:
sed -i 's/eth0/end0/g' /etc/systemd/system/sa02m-eth0-led*.service
sed -i 's/eth0/end0/g' /etc/systemd/system/sa02m-eth0-led*.path
sed -i 's/eth0/end0/g' /usr/local/bin/sa02m-eth0-led.sh
sed -i 's/eth0/end0/g' /usr/local/bin/sa02m-eth0-led-poll.sh

systemctl daemon-reload
systemctl restart sa02m-eth0-led.path sa02m-eth0-led.service 2>/dev/null || true
```

### 8.2 chronyd: IPv6 command socket

**Причина:** chrony пытается слушать `[::1]:323` (IPv6), который недоступен.

```bash
# Добавить в /etc/chrony/chrony.conf:
echo "cmdaddress 127.0.0.1" >> /etc/chrony/chrony.conf
systemctl restart chrony
```

### 8.3 vm.swappiness

Устраняется в п. 7.2 (`vm.swappiness = 0`).

---

## 9. Чек-лист верификации

После всех изменений последовательно проверить:

```bash
# 1. RT-ядро загружено:
uname -v | grep PREEMPT_RT
# Ожидаемо: ... PREEMPT_RT ...

# 2. isolcpus работает:
cat /sys/devices/system/cpu/isolated
# Ожидаемо: 3

# 3. CPU governor:
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
# Ожидаемо: performance performance performance performance

# 4. Текущая частота (без throttle):
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
# Ожидаемо: 1200000

# 5. Температура (должна быть ≤ 70°C в idle):
cat /sys/class/thermal/thermal_zone*/temp
# Ожидаемо: < 70000

# 6. sysctl RT:
sysctl kernel.sched_rt_runtime_us vm.swappiness
# Ожидаемо: -1 и 0

# 7. CODESYS запущен:
systemctl status codesyscontrol
# Ожидаемо: active (running)

# 8. Нет sugov:0 (governor overhead):
ps aux | grep sugov
# После перехода на performance — процесс отсутствует

# 9. Jitter тест (latency):
# Установить rt-tests:
apt install -y rt-tests
# Запустить cyclictest на CPU3:
taskset -c 3 cyclictest --mlockall --smp -p99 -i200 -d0 -D30s
# Ожидаемо: Max latency < 200 мкс
```

---

## 10. Откат при сбое

### Если RT-ядро не загружается (не стартует):

```bash
# На устройстве через serial console или после восстановительной загрузки с SD:
mount /dev/mmcblk2p1 /mnt/boot_fat
cp /mnt/boot_fat/zImage.bak-smp-YYYYMMDD /mnt/boot_fat/zImage
# Восстановить boot.scr:
cp /mnt/boot_fat/boot.scr.bak-pre-rt /mnt/boot_fat/boot.scr
sync && umount /mnt/boot_fat
reboot
```

### Если устройство недоступно по сети:

Использовать процедуру прошивки с USB-флешки согласно `sa02m_flash_workflow.mdc` (imageUSB → USB → SA-02m autorun.sh → прошивка eMMC).

### Быстрая проверка загрузки без коммита:

Перед окончательным деплоем: скопировать RT-ядро как `zImage.rt-test` и добавить временную команду U-Boot для загрузки альтернативного ядра через `bootcmd`. Это позволит протестировать RT-ядро без замены рабочего SMP.

---

## Итоговая архитектура после оптимизации

```
Ядро: Linux 6.1.0-rc6 + PREEMPT_RT patch-rt4
Загрузка: U-Boot → FAT16 → zImage (RT) + sun8i-a40i-sk.dtb

CPU распределение:
  CPU0-2  →  SA-02m stack (MQTT, Modbus, OPC-UA, Serial GW, nginx, MasterSCADA)
             IRQ affinity: CPU0-2 (bitmask 7)
  CPU3    →  CODESYS Control 4.20.0.0 (SCHED_FIFO prio 80, isolcpus)

Параметры RT:
  governor:             performance (1200 MHz fixed)
  kernel.sched_rt_runtime_us: -1 (no cap)
  vm.swappiness:        0
  threadirqs:           on (в boot.scr)
  isolcpus:             3 (в boot.scr)

Ожидаемый jitter CODESYS:  < 200 мкс (cyclictest)
Ожидаемая температура idle: 55–65°C (с радиатором)
```

---

*Документ создан: 2026-06-05. Автор: аудит SA-02m для CODESYS RT.*
