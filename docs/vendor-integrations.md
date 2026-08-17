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
| Node-RED | `SA02M_NODERED_DIR=/path/dir` | `/opt/vendor-installers/nodered/` | `$REPO/vendor/nodered/` |

Приоритет 3 используется автоматически в `tools/debian-rootfs/create-sa02m-rootfs.sh`:
если каталог `$REPO/vendor/{codesys,nodered}/` или `$REPO/MPLC4/cyntron/` есть на
build-host, его содержимое копируется в rootfs → `/opt/vendor-installers/<служба>/`.
MPLC переехал с `vendor/mplc4/` на `MPLC4/cyntron/` — единый источник staging
(см. `MPLC4/README.md`). Плагин `mplc_cyntron.so` **не** входит в vendor-дроп: он
отслеживается в git по пути `firmware/mplc4/mplc_cyntron.so` и подставляется в
`/opt/vendor-installers/mplc4/` отдельным шагом сборки rootfs. После первой
прошивки `install.sh` (шаги `07`, `08` и `09`) их подхватывает.

Node-RED — единственный из трёх, чей payload мы **собираем сами**, а не получаем
от вендора: рецепт и форма — в разделе «Node-RED (оффлайн payload)» ниже.

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

### Node-RED (оффлайн payload)

Node-RED — единственный стек, который раньше требовал интернет на устройстве:
промышленный сегмент без выхода в сеть — норма, и шаг `07-nodered.sh` там
просто падал. Оффлайн-payload закрывает это: ставится **node-red 4.1.13**
(поддерживаемая линия; 3.x без обновлений безопасности с 2025-06-30).

**Состав payload'а** (`vendor/nodered/` на build-host →
`/opt/vendor-installers/nodered/` на устройстве):

| Файл | Что это | Обязателен |
|---|---|---|
| `node-red-4.1.13.tar.gz` | дерево node-red с **вложенными** зависимостями | да |
| `node-v22.*-linux-armv7l.tar.xz` | официальный tarball Node.js, sha256 сверен | нет¹ |
| `nodered.service` | копия `etc/systemd/system/nodered.service` | да |
| `settings.js` | копируется, только если у пользователя его ещё нет | нет |
| `BUILD-INFO.txt` | провенанс сборки (см. ниже) | нет² |

¹ распаковывается **только** если на плате нет Node.js или он ниже
`engines` порога node-red 4.1.13 (`>=18.5`). Плата с Node 20 сохраняет свой
Node, а пропуск распаковки пишется в лог — «тихого успеха, при котором ничего
не поставилось» больше нет. Смена мажора Node — отдельный шаг по
`docs/deployment.md`, а не побочный эффект кнопки.
² не читается установщиком, но это единственный дом измеренных цифр
(размер дерева, время сборки, суммы).

**Сборка — одна команда, на плате, от root:**

```bash
sudo bash scripts/dev/build-nodered-payload.sh --out /root/nodered-payload
```

Скрипт — единственный дом команд сборки. Он не трогает работающую установку
(сборка в отдельном каталоге, `npm ci` без `-g`), проверяет свободное место,
сверяет sha256 tarball'а Node с опубликованным `SHASUMS256.txt` и пишет
`BUILD-INFO.txt`.

**Почему `npm ci` из закоммиченного lock-файла.** Дерево описано в
`vendor-src/nodered/package.json` + `package-lock.json` (~190 KB текста в
репозитории, 274 пакета). `npm ci` ставит ровно то, что в lock-файле, —
пересборка через месяц даёт то же дерево, а не «что решит npm сегодня».
Честная граница: воспроизводится **дерево файлов**, но не байты архива
(mtime/gzip) и не доступность реестра — провенанс закрывается суммами в
`BUILD-INFO.txt`.

**Почему `--omit=optional`.** Единственная нативная зависимость node-red
4.1.13 — `@node-rs/bcrypt` (napi). Без неё дерево — **чистый JavaScript**, а
значит: не нужен компилятор и `build-essential`/`python3` на плате, и — главное
— **нет привязки к ABI мажора Node**, один и тот же payload работает и на
Node 20, и на Node 22. Плата за это: хеширование паролей `adminAuth` уходит на
чистый JS `bcryptjs` (медленнее на логине, функционально то же). Node-RED
разворачивается **без** `adminAuth`, поэтому сегодня цена нулевая. Это
рассуждение, а не измерение.

**Почему дерево вложенное, а не плоское.** `npm ci` поднимает зависимости
плоско; если запаковать как есть, в `/usr/lib/node_modules` прилетят ~235
соседних каталогов. Сборщик перекладывает их в `node-red/node_modules/` —
резолвер Node ищет вверх по дереву и находит их первыми, поведение то же, а
каталог верхнего уровня ровно один. Установщик это **проверяет**: архив с
другим числом верхнеуровневых записей не распаковывается (защита от обхода
путей и от «размазывания» чужих пакетов по глобальному каталогу модулей).

**Ориентир по размеру** (замер на dev-машине x86, не на плате): распакованное
дерево ~107 MB, 274 пакета. Авторитетные цифры — из `BUILD-INFO.txt` после
сборки на плате.

**Оффлайн имеет приоритет над сетью.** И `scripts/07-nodered.sh`, и кнопка
«Установить» сначала ищут payload и только потом идут в сеть: пин, который
проигрывает тому, что сегодня отдаёт реестр, — не пин. Онлайн-путь запрашивает
тот же пин (`--node22 --nodered-version=4.1.13`), но остаётся best-effort:
NodeSource для armhf отдаёт свой 22.x, не обязательно тот же, что в payload'е.
Детерминированный путь — payload.

**Установка НЕ перезаписывает установленный Node-RED через мажор.** Если на
плате уже стоит 3.x, кнопка «Установить» вернёт `major_upgrade_refused` и
ничего не тронет: переход 3→4 мигрирует потоки и перешифровывает учётные
данные необратимо, поэтому он идёт по процедуре с бэкапом
(`docs/deployment.md` → «Доставка vendor-payload Node-RED и обновление
стека»), а не одной кнопкой.
Причина и что делать — в `/var/log/sa02m_install.log`.

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

**Node-RED — по отдельной процедуре.** Payload крупный (десятки MB), а на
плате с уже установленным Node-RED смена мажора необратимо мигрирует потоки,
поэтому доставка и установка идут по runbook'у с бэкапом и проверенным
откатом: `docs/deployment.md` → «Доставка vendor-payload Node-RED и обновление
стека». Импровизировать здесь нельзя (PROTOCOL.md инвариант 4).

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
шага синхронизируйте оба места. Для Node-RED это расхождение теперь ловится
механически — quality-строка `nodered-pin-consistency` валит сборку, если
версия/флаг Node разъехались между 07, ctl-скриптом, рецептом сборки,
lock-файлом и этим документом. Оффлайн-путь 07 не дублирует логику распаковки,
а вызывает тот же ctl-скрипт (дом один).

**Источники (должны присутствовать на устройстве):**

| Служба | Что нужно под `/opt/vendor-installers/<служба>/` |
|---|---|
| CODESYS | `codesyscontrol_*_armhf.deb` + `sa02m.conf` (systemd drop-in) |
| MPLC4 | `install.sh`, `mplc4.tar.gz`, `nginx.tar.gz`, `mplc_cyntron.so` |
| Node-RED (оффлайн) | `node-red-4.1.13.tar.gz` + `nodered.service` (обязательны) + Node armhf tarball, `settings.js`, `BUILD-INFO.txt` — см. раздел выше |

Сборка rootfs (`create-sa02m-rootfs.sh`) раскладывает `sa02m.conf`,
`mplc_cyntron.so` и `nodered.service` в эти каталоги автоматически, если
запечён соответствующий `vendor/*`. Известные подкаталоги перечислены явно
(`codesys mplc4 nodered`); любой другой каталог в `vendor/` в образ не попадает
и о нём пишется `WARN` — раньше он исчезал молча.

При отсутствии файлов кнопка «Установить» возвращает `staging_missing`; без
пакета и без интернета — `no_internet`; при попытке перезаписать установленный
Node-RED через мажор — `major_upgrade_refused`.

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
SA02M_SKIP_NODERED=1 ./install.sh   # без Node-RED
SA02M_SKIP_CODESYS=1 SA02M_SKIP_MPLC=1 ./install.sh  # без обоих
```

## Файлы репозитория, участвующие в интеграции

| Файл | Назначение |
|---|---|
| `scripts/08-codesys.sh` | Установка CODESYS Control (.deb + hold + enable). |
| `scripts/09-mplc.sh` | Установка MPLC 4D + плагин mplc_cyntron.so. |
| `install.sh` | Опциональные вызовы `08-codesys.sh` / `09-mplc.sh`. |
| `scripts/07-nodered.sh` | Установка Node-RED: payload → иначе онлайн → иначе WARN + выход 0. |
| `scripts/dev/build-nodered-payload.sh` | Сборка оффлайн-payload Node-RED на плате (единственный дом команд сборки). |
| `vendor-src/nodered/package{,-lock}.json` | Манифест дерева node-red; `npm ci` ставит ровно его. |
| `etc/systemd/system/nodered.service` | Unit Node-RED (репозиторный артефакт; payload несёт его копию). |
| `.ai-dev/quality/checks/nodered-pin-consistency.sh` | Гейт: пин версии и флаг Node не разъезжаются между домами. |
| `tools/debian-rootfs/create-sa02m-rootfs.sh` | Копирует `vendor/{codesys,mplc4,nodered}` в rootfs + drop-in CODESYS, `mplc_cyntron.so` и `nodered.service` в `/opt/vendor-installers/`. |
| `etc/sa02m-apt-hold-codesys.sh` | Существующий hold-скрипт для CODESYS. |
| `etc/sa02m-web-service-ctl.sh` | Управление CODESYS/MPLC/Node-RED из веб-панели: start/stop/install/uninstall. |
| `www/network_config/cgi-bin/services_ctrl.cgi` | CGI-эндпоинт служб (action: start/stop/install/uninstall). |
| `www/network_config/static/js/app/services.js` | UI кнопок и опрос статуса служб. |
| `www/network_config/static/js/app.js` | UI-строки CODESYS/MPLC4 (уже готово). |
| `docs/codesys-rt/README.md` | Полный аудит CODESYS RT + PREEMPT_RT tuning. |
