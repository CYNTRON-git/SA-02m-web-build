# SA-02m: Vendor integrations (CODESYS Runtime, MasterSCADA MPLC)

Проприетарные runtime'ы (CODESYS Control SL, MasterSCADA MPLC 4D) устанавливаются
на СА-02м **опциональными** шагами `install.sh`. Скрипты — `scripts/08-codesys.sh`
и `scripts/09-mplc.sh` — не тянут дистрибутивы из сети: они ищут vendor-payload
локально и, если он отсутствует, **пропускают** установку без ошибки.

Сами `.deb`, `.tar.gz`, `.wbc`, `.lic` **не коммитятся в git** (см. `.gitignore`)
— они большие, распространяются под vendor-EULA и лежат на build-host.

## Где скрипты ищут payload

| Компонент | Приоритет 1 (переменная) | Приоритет 2 (target) | Приоритет 3 (build-host) |
|---|---|---|---|
| CODESYS Runtime | `SA02M_CODESYS_DEB=/path/.deb` | `/opt/vendor-installers/codesys/*.deb` | `$REPO/vendor/codesys/*.deb` |
| MPLC 4D | `SA02M_MPLC_DIR=/path/dir` | `/opt/vendor-installers/mplc4/` | `$REPO/vendor/mplc4/` |

Приоритет 3 используется автоматически в `tools/debian-rootfs/create-sa02m-rootfs.sh`:
если каталог `$REPO/vendor/{codesys,mplc4}/` есть на build-host, его содержимое
копируется в rootfs → `/opt/vendor-installers/{codesys,mplc4}/`. После первой
прошивки `install.sh` (шаги `08` и `09`) их подхватывает.

## Подготовка vendor payload

### CODESYS Control for Linux ARM SL 4.20.0.0

Исходник: `\\build-host\...\cds\Лицензия\CODESYS Control for Linux ARM SL 4.20.0.0.package`
Это ZIP-архив CODESYS IDE. Внутри — armhf `.deb`:

```powershell
$pkg = "C:\...\CODESYS Control for Linux ARM SL 4.20.0.0.package"
$out = "C:\Users\admin\Downloads\SA-02m-web-build\vendor\codesys"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Expand-Archive -Path $pkg -DestinationPath $env:TEMP\codesys-pkg -Force
Copy-Item "$env:TEMP\codesys-pkg\Delivery\linuxarm\*_armhf.deb" $out
```

Файл на выходе: `vendor/codesys/codesyscontrol_linuxarm_4.20.0.0_armhf.deb` (~15 MB).

**Лицензия.** Файл `.wbc` в исходном каталоге отсутствует — Standard S активируется
вручную из **CODESYS Development System** (Windows IDE):

1. `Devices` → `Communication Settings` → `Add device by IP` → `192.168.1.136:11740`.
2. `Device` → `License Manager` → `Add License` → ввести Ticket-ID
   (см. `docs/codesys-rt/README.md`, поле «Ticket»).
3. `.wbc`-файл сохраняется автоматически в `/var/opt/codesys/`.
4. `systemctl restart codesyscontrol` — runtime переходит из demo в Standard S.

До активации runtime стартует в **demo-режиме (~2 часа)** — это фиксируется
в `/var/opt/codesys/codesyscontrol.log`. Скрипт `08-codesys.sh` явно выводит
предупреждение в лог установки.

**Зависимость `codemeter | codemeter-lite`** отсутствует в Debian bullseye main.
Пакет ставим через `dpkg -i --force-depends`; сразу после этого — `apt-mark hold
codesyscontrol`, чтобы `apt-get -f install` его не снял. См. `etc/sa02m-apt-hold-codesys.sh`.

**Порты:** `11740/TCP` (Gateway), `1217/UDP` (Discovery), `4840/TCP` (OPC UA).

### MasterSCADA MPLC 4D Runtime (armhf)

Исходник: `\\build-host\...\MasterSCADA\MPLC\linux-armv7hf\` (5 файлов).
Копируем каталог целиком в `vendor/mplc4/`:

```powershell
$src = "C:\...\MasterSCADA\MPLC\linux-armv7hf"
$dst = "C:\Users\admin\Downloads\SA-02m-web-build\vendor\mplc4"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\install.sh"        $dst
Copy-Item "$src\mplc4.tar.gz"      $dst
Copy-Item "$src\nginx.tar.gz"      $dst
Copy-Item "$src\mplc_cyntron.so"   $dst  # плагин ЦИНТРОН
Copy-Item "$src\version.txt"       $dst
```

**Что делает `scripts/09-mplc.sh`:**

1. Запускает vendor `install.sh --use-systemd --http-port=8082 --enable-log`.
   Порт `8082` выбран специально — SA-02m nginx уже на `9999`, порт `80`
   оставлен свободным для сторонних UI на промышленных стендах. Изменить:
   `SA02M_MPLC_HTTP_PORT=8081 ./install.sh`.
2. Копирует `mplc_cyntron.so` в `/opt/mplc4/` (плагин ЦИНТРОН для драйверов).
3. Enable/start `mplc4.service`.

**Порты MPLC:** `8082/TCP` (nginx UI), `31550/TCP` (mplc_monitor), `30750/TCP`
(fcgi backend).

**Лицензирование MPLC.** Vendor поставляет runtime уже с runtime-ключом
(NetKey поддерживается, но по умолчанию выключен). Дополнительные ключи —
через MasterSCADA IDE (Windows) → `Настройки` → `Ключи защиты`.

## Ручная загрузка на боевое устройство

Если vendor-payload не запечён в rootfs, но нужно поставить CODESYS/MPLC
на уже прошитое устройство:

```powershell
# 1. Скопировать payload на устройство:
$hk = "SHA256:STw9vh3ohLieuJLXlsa/feL2UEHi/o4juXymYFKyuuA"
& "C:\Program Files\PuTTY\plink.exe" -batch -ssh root@192.168.1.136 -pw cyntron -hostkey $hk `
    "mkdir -p /opt/vendor-installers/codesys /opt/vendor-installers/mplc4"
& "C:\Program Files\PuTTY\pscp.exe" -batch -pw cyntron -hostkey $hk -r `
    ".\vendor\codesys\*" "root@192.168.1.136:/opt/vendor-installers/codesys/"
& "C:\Program Files\PuTTY\pscp.exe" -batch -pw cyntron -hostkey $hk -r `
    ".\vendor\mplc4\*" "root@192.168.1.136:/opt/vendor-installers/mplc4/"

# 2. Запустить установку на устройстве (только шаги CODESYS+MPLC):
& "C:\Program Files\PuTTY\plink.exe" -batch -ssh root@192.168.1.136 -pw cyntron -hostkey $hk `
    "cd /opt/sa02m-web-build && bash scripts/08-codesys.sh && bash scripts/09-mplc.sh"
```

## Проверка

```bash
# Сервисы
systemctl status codesyscontrol mplc4 --no-pager -l | head -30

# Порты
ss -tlnp | grep -E ':(11740|4840|8082|31550|30750)\b'

# Web-панель СА-02м → Управление → Службы → CODESYS / MPLC4 (кнопки start/stop работают
# из коробки: SERVICE_DEFS уже содержат оба, см. etc/sa02m-web-service-ctl.sh).

# CODESYS лицензионный режим:
grep -i 'demo\|license' /var/opt/codesys/codesyscontrol.log | tail
```

## Отключение отдельных шагов

```bash
SA02M_SKIP_CODESYS=1 ./install.sh   # без CODESYS
SA02M_SKIP_MPLC=1    ./install.sh   # без MPLC
SA02M_SKIP_CODESYS=1 SA02M_SKIP_MPLC=1 ./install.sh  # без обоих
```

## Файлы репозитория, участвующие в интеграции

| Файл | Назначение |
|---|---|
| `scripts/08-codesys.sh` | Установка CODESYS Control (.deb + hold + enable). |
| `scripts/09-mplc.sh` | Установка MPLC 4D + плагин mplc_cyntron.so. |
| `install.sh` | Опциональные вызовы `08-codesys.sh` / `09-mplc.sh`. |
| `tools/debian-rootfs/create-sa02m-rootfs.sh` | Копирует `vendor/*` в rootfs `/opt/vendor-installers/`. |
| `etc/sa02m-apt-hold-codesys.sh` | Существующий hold-скрипт для CODESYS. |
| `etc/sa02m-web-service-ctl.sh` | Управление CODESYS/MPLC из веб-панели (уже готово). |
| `www/network_config/static/js/app.js` | UI-строки CODESYS/MPLC4 (уже готово). |
| `docs/codesys-rt/README.md` | Полный аудит CODESYS RT + PREEMPT_RT tuning. |
