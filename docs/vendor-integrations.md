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
| MPLC 4D | `SA02M_MPLC_DIR=/path/dir` | `/opt/vendor-installers/mplc4/` | `$REPO/MPLC4/cyntron/` |

Приоритет 3 используется автоматически в `tools/debian-rootfs/create-sa02m-rootfs.sh`:
если каталог `$REPO/vendor/codesys/` или `$REPO/MPLC4/cyntron/` есть на build-host,
его содержимое копируется в rootfs → `/opt/vendor-installers/{codesys,mplc4}/`.
MPLC переехал с `vendor/mplc4/` на `MPLC4/cyntron/` — единый источник staging
(см. `MPLC4/README.md`). Плагин `mplc_cyntron.so` **не** входит в vendor-дроп: он
отслеживается в git по пути `firmware/mplc4/mplc_cyntron.so` и подставляется в
`/opt/vendor-installers/mplc4/` отдельным шагом сборки rootfs. После первой
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

**Порты:** `11740/TCP` (Gateway), `1217/UDP` (Discovery), `4840/TCP` —
**собственный** OPC UA-сервер CODESYS (владеет портом 4840). Наш northbound-шлюз
`sa02m-mqtt-opcua` работает на `4841/TCP` — распределение портов закреплено в
`docs/contracts/kernel-conditional-services.md`.

### MasterSCADA MPLC 4D Runtime (armhf)

Исходник: `\\build-host\...\MasterSCADA\MPLC\linux-armv7hf\` (vendor-дроп, 5 файлов).
Копируем каталог целиком в `MPLC4/cyntron/` (единый источник staging, был
`vendor/mplc4/`; версия payload — `MPLC4/README.md` / `version.txt`):

```powershell
$src = "C:\...\MasterSCADA\MPLC\linux-armv7hf"
$dst = "C:\Users\admin\Downloads\SA-02m-web-build\MPLC4\cyntron"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\install.sh"        $dst
Copy-Item "$src\mplc4.tar.gz"      $dst
Copy-Item "$src\nginx.tar.gz"      $dst
Copy-Item "$src\admin.tar.gz"      $dst
Copy-Item "$src\version.txt"       $dst
```

Плагин `mplc_cyntron.so` **не** входит в vendor-дроп `MPLC4/cyntron/` — он
отслеживается в git по пути `firmware/mplc4/mplc_cyntron.so` и подставляется в
`/opt/vendor-installers/mplc4/` автоматически при сборке rootfs (см. E12).

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

# Web-панель СА-02м → Управление → Службы → CODESYS / MPLC4 (кнопки start/stop/
# install/uninstall работают из коробки; см. раздел ниже и etc/sa02m-web-service-ctl.sh).

# CODESYS лицензионный режим:
grep -i 'demo\|license' /var/opt/codesys/codesyscontrol.log | tail
```

## Установка/удаление служб из веб-панели

CODESYS, MPLC4 и Node-RED устанавливаются и удаляются кнопками в
**Управление → Службы** (v1.0.5.19+). Логика — прямо в
`etc/sa02m-web-service-ctl.sh` (verb'ы `install`/`uninstall`), а НЕ вызовом
`scripts/07/08/09` (их нет на устройстве, они зависят от `scripts/lib.sh`).
Скрипты 07/08/09 остаются каноничным install-time путём; при правке общего
шага синхронизируйте оба места.

**Источники (должны присутствовать на устройстве):**

| Служба | Что нужно под `/opt/vendor-installers/<служба>/` |
|---|---|
| CODESYS | `codesyscontrol_*_armhf.deb` + `sa02m.conf` (systemd drop-in) |
| MPLC4 | `install.sh`, `mplc4.tar.gz`, `nginx.tar.gz`, `mplc_cyntron.so` |
| Node-RED (оффлайн) | Node armhf tarball + собранное дерево `node-red@3` + `nodered.service` (+`settings.js`) |

Сборка rootfs (`create-sa02m-rootfs.sh`) раскладывает `sa02m.conf` и
`mplc_cyntron.so` в эти каталоги автоматически, если запечён `vendor/*`.
Оффлайн-пакет Node-RED готовится отдельно (собирается на armhf, ядро без
самообновления). При отсутствии файлов кнопка «Установить» возвращает
`staging_missing` (для Node-RED без интернета и без пакета — `no_internet`).

**«Удалить» = полная очистка:** остановка, `dpkg --purge` (для CODESYS —
`dpkg`, НЕ `apt`, чтобы не тянуть отсутствующий `codemeter`), удаление
данных/конфигов (`/var/opt/codesys`, `/opt/mplc4`, `~nodered/.node-red`).
Node-RED дополнительно удаляет пользователя `nodered` (`userdel -r`), но
**оставляет Node.js**. Каталоги `/opt/vendor-installers/` **сохраняются**.

**Развёртывание:** новые правила sudoers и логика ctl-скрипта приходят только с
полным `install.sh` (`scripts/03-webserver.sh`) — обновление только `www/` их
НЕ доставит.

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
| `scripts/07-nodered.sh` | Установка Node-RED (Node.js + nodered.service). |
| `tools/debian-rootfs/create-sa02m-rootfs.sh` | Копирует `vendor/*` в rootfs + drop-in CODESYS и `mplc_cyntron.so` в `/opt/vendor-installers/`. |
| `etc/sa02m-apt-hold-codesys.sh` | Существующий hold-скрипт для CODESYS. |
| `etc/sa02m-web-service-ctl.sh` | Управление CODESYS/MPLC/Node-RED из веб-панели: start/stop/install/uninstall. |
| `www/network_config/cgi-bin/services_ctrl.cgi` | CGI-эндпоинт служб (action: start/stop/install/uninstall). |
| `www/network_config/static/js/app/services.js` | UI кнопок и опрос статуса служб. |
| `www/network_config/static/js/app.js` | UI-строки CODESYS/MPLC4 (уже готово). |
| `docs/codesys-rt/README.md` | Полный аудит CODESYS RT + PREEMPT_RT tuning. |
