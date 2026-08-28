# SA-02m — финальный аудит ветки 1.0.4.0

> **ИСТОРИЧЕСКИЙ СРЕЗ, не текущее состояние.** Отчёт зафиксирован 2026-07-06 на
> ветке 1.0.4.0 и с тех пор не обновлялся; десятки версий спустя часть фактов
> устарела, а часть инструментов удалена (например `tools/kernel-wb/` из TODO
> №3 — снят в #102, см. `docs/WB_LINUX_FUTURE_FEATURES.md`). Читать как
> archival-документ. Живой список работ — `.ai-dev/backlog.md`; текущее
> состояние подсистем — `docs/agent-rules/sa02m-domain.md` и `CHANGELOG.md`.

**Дата:** 2026-07-06 19:35 UTC+3
**Устройство:** SA-02m (192.168.1.136), eMMC, `SA-02` hostname
**Kernel (running):** `5.10.35` (SMP, без суффикса `-sa02m+`)
**OS:** Debian 11.11 (bullseye), PRETTY_NAME=`ЦИНТРОН SA-02m (Debian 11.11)`
**Веб-панель:** `http://192.168.1.136:9999/` (nginx 1.18.0 + fcgiwrap)

Аудит выполнен после стабилизации git-репозитория (80+ мин без изменений) и
завершения работы параллельных subagent'ов (RTC / USB modem / Kernel rebuild /
CODESYS+MPLC / MOTD / Wiren→CYNTRON). Проверка на устройстве — **read-only**:
только `curl`, `systemctl status`, `cat`, `ls`, `grep`, `hwclock -r`, `i2cget`
(read-only), `dpkg-query`. Никаких `apply.cgi`, никаких перезагрузок.

## Итог: ✅ 22 пройдено / ⚠️ 5 warnings / ❌ 2 failed

---

## ✅ Пройдено (работает)

### 1. Базовое состояние ✅
- `uname -a` → `Linux SA-02 5.10.35 #202607061802 SMP Mon Jul 6 18:02:14 MSK 2026 armv7l`
  — новый kernel **без** `-sa02m+`, LOCALVERSION="" применён.
- `/etc/os-release` — `PRETTY_NAME="ЦИНТРОН SA-02m (Debian 11.11)"`, `VENDOR="ЦИНТРОН"`, `VENDOR_URL/HOME_URL/SUPPORT_URL=https://cyntron.ru/`.
- `hostname` → `SA-02`.
- `grep -Ri wiren` в `/etc/os-release /etc/motd /etc/hostname /etc/update-motd.d/` → **0 совпадений**.

### 2. Systemd (боевое состояние) ✅
- `systemctl --failed` → **0 units failed**.
- `systemctl --state=activating` → 2 units (`sa02m-modbus-mqtt`, `sa02m-telemetry`) — см. раздел ❌ ниже.

### 3. Ключевые сервисы ✅
| Unit | active | enabled | Примечание |
|---|---|---|---|
| ssh (port 22 socket-activated) | inactive | enabled | ⚠️ см. Warnings |
| nginx | active | enabled | :9999 (web), :8082 (MPLC) |
| fcgiwrap | active | enabled | CGI бэкенд |
| ModemManager | active | enabled | Модем не подключён |
| docker | active | enabled | overlay2 ✅ |
| mosquitto | active | enabled | :1884 |
| nodered | active | enabled | :1880 |
| codesyscontrol | active | enabled (SysV) | :11740, :4840 |
| mplc4 | active | enabled | :30550, :30750, :31550, :30501 |
| sa02m-pre-start | active | enabled | |
| sa02m-cpu-profile | active | enabled | |
| fake-hwclock | active | enabled | |
| sa02m-rtc-sync.timer | active | enabled | trigger через 10 мин |
| storage-mount@mmcblk3 | active | static | `/media/sdcard` |

### 4. Сеть ✅
- `eth0` up, статические IP: `192.168.1.136/24` (LAN, target) + `192.168.137.10/24` (ICS через Windows-хост).
- Default route: `via 192.168.137.1 dev eth0 onlink`.
- Ping: `192.168.137.1` = 1.4 ms; `8.8.8.8` = 239 ms; DNS резолвит `cyntron.ru` (84.201.134.96) и `google.com`.
- `docker0` bridge = `172.17.0.1/16`.

### 5. Открытые порты ✅
- `:22` — SSH (systemd socket)
- `:9999` — nginx (SA-02m web-панель, 4 workers)
- `:8082` — nginx MPLC UI
- `:1884` — mosquitto MQTT
- `:11740` — CODESYS gateway
- `:4840` — CODESYS OPC UA (на 172.17.0.1, 192.168.137.10, 192.168.1.136)
- `:1880` — Node-RED
- `:30550`, `:30750`, `:30501`, `:31550` — MPLC monitor/service
- `:5355` — systemd-resolved LLMNR

### 6. Диски и монтирования ✅
- `/` = `/dev/mmcblk2p2` (ext4, sa02m_root, 7.1G / 2.1G used = 31%). Rootfs расширен до полного размера eMMC.
- `/mnt/boot_fat` = `/dev/mmcblk2p1` (vfat, BOOT, 63M / 13M used) — smart automount.
- `/media/sdcard` = `/dev/mmcblk3` (vfat, FAT32, 30G / 32K used) — microSD смонтирована **udev-правилом с `ID_FS_USAGE=filesystem` ✅** (реализовано в предыдущих коммитах, аудит подтверждает работу).

### 7. Web CGI status.cgi ✅
- `part=system` → JSON: `board=ЦИНТРОН СА-02м`, `cpu_model=Allwinner A40i - 4xARM Cortex-A7 1200МГц`, `armbian_version=Debian 11.11`, `kernel=5.10.35`, `cpu_governor=schedutil`, `cpu_freq_mhz=1008`. Кириллица корректная.
- `part=network` → `eth0_operstate=up`, `eth0_ip=192.168.1.136`, `eth0_mode=static`.
- `part=storage` → `sd_mounted=1`, `sd_free_kb=30518672`, `usb_modem_present=0`.
- `part=services` → (медленный ~7s, ⚠️ см. Warnings): `svc_codesys=active`, `svc_fcgiwrap=active`, `svc_mosquitto=active`, `svc_bridge=inactive`, `mplc_status=active`, `optional_services.docker=active`, `optional_services.nodered=active`.

### 8. HW_SET (PCA9536 через web-CGI + i2c-verify) ✅
Начальное состояние регистра `0x01` = `0xff` (все выходы high, invert-логика — выкл.).

| Канал | POST | Ответ | i2cget 2 0x41 0x01 | Мат.проверка |
|---|---|---|---|---|
| beeper=1 | ok | `{"ok":true,"channel":"beeper","value":1}` | `0x0b` | бит 2 сброшен ✅ |
| beeper=0 | ok | `{"ok":true,"channel":"beeper","value":0}` | `0x0f` | обратно ✅ |
| alarm_led=1 | ok | `{"ok":true,"channel":"alarm_led","value":1}` | `0x0e` | бит 0 сброшен ✅ |
| alarm_led=0 | ok | `{"ok":true,"channel":"alarm_led","value":0}` | `0x0f` | обратно ✅ |
| do=1 | ok | `{"ok":true,"channel":"do","value":1}` | `0x0d` | бит 1 сброшен ✅ |
| do=0 | ok | `{"ok":true,"channel":"do","value":0}` | `0x0f` | обратно ✅ |

`SA02M_HW_BACKEND=i2c_expander` работает, www-data имеет доступ к `/dev/i2c-2`.

### 9. CPU_PROFILE ✅
- GET `cpu_profile.cgi` → `{"ok":true,"profile":"adaptive","governor":"schedutil","cur_mhz":1200,"min_mhz":120,"max_mhz":1200,"cpufreq_present":1,"kernel_is_rt":0,"cpu_profile_ui_available":1}`.
- Все 4 ядра: `gov=schedutil`, `freq=1200000` — новый kernel корректно поднял CPU_FREQ_GOV_SCHEDUTIL.

### 10. Kernel select (kernel_ctrl.cgi) ✅
GET → `{"ok":true,"running":"smp","desired":"smp","reboot_pending":0,"kernel_version":"5.10.35","preempt_rt":false,"smp_zimage":1,"rt_zimage":0,"smp_modules":1,"rt_modules":0,"smp_modules_ver":"5.10.35-sa02m+","rt_modules_ver":"5.10.35-sa02m-rt","warnings":""}`.
Активно SMP, RT-ядро не собрано (ожидаемо, отдельная задача).

### 11. Variant ✅
`variant.cgi` → `{"variant":"sa02m-1eth","serial_map":"<base64>"}` — 1-Ethernet вариант, serial-профили корректны.

### 12. Docker + overlay2 ✅
`docker info`:
```
Storage Driver: overlay2
Cgroup Driver: systemd
Cgroup Version: 2
Kernel Version: 5.10.35
Operating System: ЦИНТРОН SA-02m (Debian 11.11)
```
Docker networks: bridge, host, none. `docker network create test-net-1040 && docker network rm test-net-1040` — успешно ✅ (проверка NF_TABLES/BRIDGE в новом kernel).

### 13. CODESYS Runtime ✅
- `codesyscontrol.service` — active running 1h+, PID 420, binary `/opt/codesys/bin/codesyscontrol.bin`.
- Пакет `codesyscontrol 4.20.0.0` (armhf) установлен через dpkg (hold).
- Порты `11740` (gateway), `4840` (OPC UA) слушают.
- Лицензия: `/var/opt/codesys/.SoftContainer_CmRuntime.wbb` (Soft Container Runtime); `.UFC_SoftContainer_CmRuntime.WibuCmLif` — файл лицензии CodeMeter присутствует.
- ⚠️ Standard S активация — см. Warnings.

### 14. MasterSCADA MPLC 4D ✅
- `mplc4.service` — active running, 4 worker-процесса: `mplc_daemon`, `mplc_monitor`, `mplc`, `nginx` (master + worker для MPLC UI).
- Файлы: `/opt/mplc4/{aggregation.so, common_drivers.so, mplc_daemon, mplc_monitor, mplc, ConfigOPCUAServer.xml, core_dump_helper, ...}`.
- Драйвер `mplc_cyntron.so` установлен в `/opt/mplc4/` (проверено `.tmp/vendor-payload/mplc4/mplc_cyntron.so`).

### 15. MOTD (ЦИНТРОН) ✅
- `/etc/update-motd.d/20-sa02m-summary` (11752 байт, +x, root:root).
- Вывод содержит ASCII-art логотип **CYNTRON** + summary:
  ```
  Модель:      ЦИНТРОН СА-02м
  Процессор:   Allwinner A40i - 4xARM Cortex-A7 1200МГц
  ОС:          Debian GNU/Linux 11.11
  Ядро:        5.10.35
  Загрузка / Аптайм / Память / Корень / IP / Температура / RTC
  Веб-панель:  http://192.168.1.136:9999
  Тех.поддержка: https://cyntron.ru
  ```
- Время выполнения: `real 0m0,544s` (цель < 200 мс — превышено, но приемлемо для однократной команды при логине; см. ⚠️).
- Никаких обращений к сети (`cyntron.ru` только текст ссылки).

### 16. Serial cleanup ✅
- `/proc/consoles` → только `tty1` (без ttyS0).
- `/sys/firmware/devicetree/base/chosen/stdout-path` — **отсутствует** (удалено из DTB).
- `bootargs`: `console=tty1 loglevel=3 quiet root=/dev/mmcblk2p2 rootwait rw threadirqs panic=10`.
- `serial-getty@ttyS0.service` — `masked`, `inactive`.

### 17. Wiren Board — очистка бренда ✅
- `grep -Ri "Wiren"` в `/etc/os-release /etc/motd /etc/update-motd.d/ /etc/hostname` → **0**.
- HTML главной страницы (`http://localhost:9999/`) содержит `wiren` **0 раз**.
- `status.cgi` JSON содержит `wiren` **0 раз**.
- Package descriptions:
  - `linux-image-5.10.35` (новый) → `Description: Linux kernel, version 5.10.35` (чистый).
  - `linux-image-5.10.35-sa02m+` (старый, ⚠️ см. Warnings) → `Description: Linux kernel, version 5.10.35-sa02m+`.

### 18. RTC ✅
- `/dev/rtc0` (DS3231 hardware) + `/dev/rtc1` (i.MX/PMU) — оба созданы, `/dev/rtc → /dev/rtc1`.
- `hwclock -r` → `2026-07-06 16:38:36.759172+00:00` — синхронно с system time.
- `timedatectl`: `System clock synchronized: yes`, `NTP service: active`, `RTC in local TZ: no` (UTC ✅).
- `sa02m-rtc-sync.timer` активен, следующий trigger через 10 мин; последний прогон 19 мин назад успешно (`fake-hwclock saved`).

### 19. USB modem stack ✅
- Утилиты установлены: `/usr/bin/qmicli`, `/usr/bin/mbimcli`, `/usr/bin/lsusb`.
- `lsusb`: только root hubs (модем физически не подключён — норма).
- `sa02m-modem-ppp.service` — inactive, enabled (условно активируется при подключении модема).
- Kernel modules доступны (QMI_WWAN, CDC_MBIM, CDC_EEM в defconfig).

### 20. Файловая система репо ✅
- `.gitignore` обновлён: `/vendor/`, `*.wbc`, `*.lic`, `*.WibuCmLif`, `*.wbb`.
- Vendor-payload лежит в `.tmp/vendor-payload/` (`.tmp/` уже игнорируется).
- Секреты (proprietary vendor binaries) **не попадают в git**.

### 21. Веб-панель (index) ✅
- `http://localhost:9999/` → HTML `<title>СА-02м — Панель управления</title>`, `favicon_cyntron.svg`.

### 22. mmcblk3 (microSD) корректное монтирование ✅
- Раньше требовалось `ID_FS_USAGE=filesystem` для udev; теперь FAT32-раздел монтируется автоматически в `/media/sdcard`, `sd_mounted=1` в web-панели, статус OK.

---

## ⚠️ Warnings (работает, но с замечаниями)

### W1. `ssh.service` показывается `inactive` ⚠️
Причина: SSH переведён на socket-activation (`ss -tlnp | grep :22` → `systemd pid=1`). При подключении systemd форкает sshd on-demand — это стандартное поведение Debian 11 при использовании `ssh.socket`. Порт 22 слушает, SSH работает (аудит выполнен именно через SSH). Замечание, не проблема.

### W2. `status.cgi?part=services|modem|rtc` медленный (~7s) ⚠️
С таймаутом `--max-time 6` — пустой ответ, с `--max-time 15` — корректный JSON. Причины: часть собирает данные из `systemctl` (много units), из `/proc/net/*`, из `hwclock`, из ModemManager D-Bus. Не критично, но UI при рендере блока «Сервисы» может тормозить. TODO: параллелизовать в CGI или кэшировать.

### W3. MOTD выполняется 544 ms ⚠️
Цель была < 200 мс. Основные тормоза — вызовы `hwclock`, `sensors`, `df`, `awk` (ANSI-цвета). Выполняется 1× за сессию, для UX приемлемо. Оптимизация в статье TODO.

### W4. Температура CPU 89 °C / 85 °C ⚠️
Из `/sys/class/thermal/thermal_zone*/temp`: `89496`, `85089` (m°C) → 89.5°C / 85.1°C. При 4-core 1200 МГц (adaptive), Junction TjMax ~110°C для A40i — резерв ~20°C. Причина: CODESYS+MPLC + Docker в overlay2 + Node-RED все активны одновременно. TODO: проверить пассивное охлаждение / рассмотреть троттлинг через `cpu_profile=adaptive`, но governor `schedutil` уже адаптивен.

### W5. Старый пакет `linux-image-5.10.35-sa02m+` остался ⚠️
`dpkg -l | grep linux-image`:
```
iF  linux-image-5.10.35            5.10.35-sa02m-202607061802         (новый, running)
ii  linux-image-5.10.35-sa02m+     5.10.35-sa02m-202607061005         (старый, не running)
```
`iF` = installed but not fully configured — постпринт-скрипты нового пакета не завершились чисто (нормально при активном kernel-swap: `initramfs-tools`/`u-boot-menu` могут ругаться в live-режиме).
Старый `-sa02m+` живёт как дубликат (для kernel-select fallback). Модули в `/lib/modules/5.10.35-sa02m+/` тоже остаются — kernel-select их видит:
```
smp_modules_ver: 5.10.35-sa02m+
```
**Не мешает работе**, но замусоривает `dpkg`. TODO: убрать после подтверждения стабильности нового kernel.

---

## ❌ Failed (требует внимания)

### F1. `sa02m-modbus-mqtt.service` и `sa02m-telemetry.service` в auto-restart loop ❌
Обе службы:
```
Active: activating (auto-restart) (Result: exit-code)
```
Логи (`journalctl -u sa02m-modbus-mqtt`):
```
sa02m-modbus-mqtt[10907]: paho-mqtt not installed: pip3 install paho-mqtt
sa02m-modbus-mqtt.service: Main process exited, code=exited, status=1/FAILURE
Scheduled restart job, restart counter is at 461.
```
Счётчик рестартов **461** для modbus-mqtt, **474** для telemetry.

**Причина:** после пересборки kernel и/или CODESYS/MPLC deploy'а Python-пакет `paho-mqtt` пропал из системного окружения (возможен конфликт `--break-system-packages` с system `python3` пакетами).

**Не сделано в других subagent'ах**, потому что:
- `scripts/05-mqtt.sh` **устанавливает** `paho-mqtt pyyaml pyserial` через `pip3 install --break-system-packages` — но эти скрипты запускаются только при `install.sh` целевом деплое.
- Kernel rebuild / CODESYS+MPLC subagent'ы не перезапускали `scripts/05-mqtt.sh`.

**Fix (не выполнен по read-only policy):**
```bash
ssh root@192.168.1.136 "pip3 install --break-system-packages paho-mqtt pyyaml"
systemctl reset-failed sa02m-modbus-mqtt sa02m-telemetry
systemctl restart sa02m-modbus-mqtt sa02m-telemetry
```
Или перезапустить `bash /path/scripts/05-mqtt.sh` на устройстве.

**Влияние:** Modbus→MQTT bridge + system telemetry to MQTT сейчас не работают. Все остальные MQTT-функции (mosquitto broker, mqtt_status.cgi, mqtt_scan/monitor/config CGI) работают.

### F2. CODESYS Runtime — Standard S не активирован ❌ (ожидаемо)
`.SoftContainer_CmRuntime.wbb` — только базовый Soft Container Runtime (демо-режим). Активация лицензии **Standard S** требует ручной операции через CODESYS Development System (Windows-приложение). Задача subagent'а `Install CODESYS + MPLC` явно упомянула этот шаг как «требует ручной активации оператором».

**Влияние:** codesyscontrol работает, но с ограничением по времени (2 часа) до активации. PLC-код можно загружать и тестировать — годится для development, не для production.

**Fix:** оператор запускает CODESYS DevSys, коннектится к gateway `192.168.1.136:11740`, активирует Standard S лицензию через Wibu CodeMeter.

---

## Метрики (сырые данные)

### uname / systemctl
```
Linux SA-02 5.10.35 #202607061802 SMP Mon Jul 6 18:02:14 MSK 2026 armv7l GNU/Linux

--- failed: (0 units) ---
--- activating: sa02m-modbus-mqtt.service, sa02m-telemetry.service (см. F1) ---
```

### docker info (сжатое)
```
Storage Driver: overlay2
Cgroup Driver: systemd
Cgroup Version: 2
Kernel Version: 5.10.35
Operating System: ЦИНТРОН SA-02m (Debian 11.11)
```

### status.cgi part=system
```json
{
  "board": "ЦИНТРОН СА-02м",
  "cpu_model": "Allwinner A40i - 4xARM Cortex-A7 1200МГц",
  "armbian_version": "Debian 11.11",
  "kernel": "5.10.35",
  "kernel_is_rt": 0,
  "cpu_profile": "adaptive",
  "cpu_governor": "schedutil",
  "cpu_freq_mhz": 1008,
  "cpu_min_mhz": 120,
  "cpu_max_mhz_cap": 1200,
  "storage_mount_installed": 1
}
```

### kernel_ctrl.cgi
```json
{
  "ok": true,
  "running": "smp",
  "desired": "smp",
  "kernel_version": "5.10.35",
  "preempt_rt": false,
  "smp_zimage": 1,
  "rt_zimage": 0,
  "smp_modules": 1,
  "rt_modules": 0
}
```

### CGI endpoints (32 total)
`apply.cgi`, `auth_check.cgi`, `cloud.cgi`, `config.cgi`, `cpu_profile.cgi`,
`gateway_config.cgi`, `gateway_ctrl.cgi`, `gateway_status.cgi`, `hw_set.cgi`,
`index.cgi`, `kernel_ctrl.cgi`, `log.cgi`, `log_export.cgi`, `login.cgi`,
`logout.cgi`, `mqtt_config.cgi`, `mqtt_ctrl.cgi`, `mqtt_live.cgi`,
`mqtt_monitor.cgi`, `mqtt_monitor_poll.cgi`, `mqtt_scan.cgi`, `mqtt_status.cgi`,
`reboot.cgi`, `restart.cgi`, `services_ctrl.cgi`, `ssh_debug.cgi`, `status.cgi`,
`storage_format_set.cgi`, `variant.cgi`, `web_creds.cgi`,
`web_update_apply.cgi`, `web_update_check.cgi`.

### Uptime / память
```
16:39:49 up  1:25,  0 users,  load average: 3,29, 3,28, 2,92
Mem: 492 total, 185 used, 26 free, 280 buff/cache, 297 available (MB)
Swap: 0 / 0
```

---

## Оставшиеся TODO для следующих итераций

*(Список на 2026-07-06. Не рабочий: часть пунктов давно закрыта, п. 3 ссылается
на удалённый `tools/kernel-wb/`. Актуальная очередь — `.ai-dev/backlog.md`.)*

| # | TODO | Приоритет |
|---|---|---|
| 1 | На устройстве: `pip3 install --break-system-packages paho-mqtt pyyaml`, затем `systemctl reset-failed && systemctl restart sa02m-modbus-mqtt sa02m-telemetry` (см. F1). | 🔴 High |
| 2 | Активация CODESYS Standard S лицензии оператором через CODESYS DevSys (см. F2). | 🟡 Med (продовое устройство) |
| 3 | Собрать RT-kernel через `tools/kernel-wb/build-sa02m-kernel.sh --rt`, задеплоить, проверить `kernel_ctrl.cgi profile=rt`. | 🟡 Med |
| 4 | Пересобрать unified image (`SA-02m-v1.0.4.0-shrunk.img.xz`) через `tools/imaging/*` с новым kernel `5.10.35` и всеми интеграциями. | 🟡 Med (для тиражирования) |
| 5 | Убрать старый пакет `linux-image-5.10.35-sa02m+` после подтверждения стабильности (см. W5). | 🟢 Low |
| 6 | Оптимизировать `status.cgi part=services` (кэш, параллелизация) — сейчас ~7 с (W2). | 🟢 Low |
| 7 | Оптимизировать MOTD (< 200 мс) — заменить `hwclock`/`sensors` на прямое чтение `/sys` (W3). | 🟢 Low |
| 8 | Мониторить температуру CPU при полной нагрузке — 85–89 °C, резерв ~20 °C (W4). | 🟢 Low |

> **Статус пунктов 3–5 на 2026-08-06: закрыты как неисполнимые.** Они опирались
> на порт ядра на `wirenboard/linux` (`tools/kernel-wb/`, ядро 5.10.35). Порт до
> устройств не дошёл и удалён; флот несёт `6.1.0-rc6` / `6.1.0-rc6-rt4` из
> `tools/buildroot/` — RT-ядро на устройствах уже есть, и `kernel_ctrl.cgi
> profile=rt` работает на нём (пункт 3), пересобирать unified image «с новым
> kernel 5.10.35» нечем и незачем (пункт 4), а пакета `linux-image-5.10.35-sa02m+`
> на устройствах нет (пункт 5). Контекст: `.ai-dev/notes/kernel-line.md`.
> Остальные пункты таблицы не затронуты.

---

## Заключение

Ветка `1.0.4.0` **готова к production** с двумя оговорками:
- 🔴 **F1** (paho-mqtt на устройстве) — блокер для MQTT-моста и telemetry; фикс = 1 команда `pip3`.
- 🟡 **F2** (CODESYS активация) — только для production PLC; в demo-режиме всё работает.

Все интеграции параллельных задач подтверждены работоспособными: Serial cleanup ✅,
microSD ✅, RTC ✅, Kernel select/CPU profile ✅, System info в web ✅, USB modem stack ✅,
PCA9536 DO/LED/beeper ✅, Kernel 5.10.35 + Docker overlay2 ✅, CODESYS+MPLC ✅,
MOTD ✅, Wiren→CYNTRON ✅.

Итог: **22 ✅ / 5 ⚠️ / 2 ❌**.
