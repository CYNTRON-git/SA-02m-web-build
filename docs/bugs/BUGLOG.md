## [2026-06-02 11:49] branch: 1.0.3.22

**Файл(ы):** `www/network_config/static/js/app.js`, `/var/www/network_config/static/js/app.js` (192.168.1.113)
**Тип:** Некорректное поведение
**Описание:** При выбранном варианте SA-02m-2 (`sa02m-2eth`) заголовок `#device-title` оставался «СА-02м» вместо «СА-02м-2».
**Причина:** На устройстве был устаревший `app.js`: `applyVariantVisibility()` менял только `data-hide-for`, без обновления заголовка. В репозитории логика заголовка уже была; `variant.cgi` и `status.cgi` возвращали `sa02m-2eth` корректно.
**Исправление:** Развёрнут актуальный `app.js` на .113; `APP_VERSION` синхронизирован с `1.0.3.22` (cache-bust в `index.html`).

---

## [2026-06-02 11:47] branch: 1.0.3.22

**Файл(ы):** `/etc/sa02m_status_blocks.conf`, `etc/sa02m_status_blocks.conf`
**Тип:** Некорректное поведение
**Описание:** На устройстве 192.168.1.113 виджет «Службы» на вкладке «Общая информация» не показывал активность сервисов (nginx, mosquitto, MQTT-мост, MPLC4).
**Причина:** В `/etc/sa02m_status_blocks.conf` был `SA02M_STATUS_ENABLE_SERVICES=0`. `status.cgi` при отключённом блоке возвращает все поля служб как `"unknown"`; UI скрывает mosquitto/MQTT/MPLC и показывает только «…» для nginx/fcgiwrap.
**Исправление:** На устройстве: `sa02m-status-blocks-guard set services 1` + `confirm`. В репозитории: дефолт `SA02M_STATUS_ENABLE_SERVICES=1` в `etc/sa02m_status_blocks.conf` для новых установок.

---


**Файл(ы):** `etc/fix-eth.sh`
**Тип:** Логическая ошибка
**Описание:** После перезагрузки на SA-02m-2 (2 Ethernet) default route для DHCP-интерфейса end1 исчезал после первого запуска net-watchdog (~60 сек после boot).
**Причина:** fix-eth.sh содержал "early link bounce" (ip link set end1 down/up), который сбрасывал kernel-маршруты. После bounce dhclient переходил в RENEW/REBIND и не всегда переустанавливал default route. В репозиторной версии bounce уже был убран, но на устройстве оставалась старая версия скрипта.
**Исправление:** 1) Развёрнута актуальная версия fix-eth.sh (без bounce). 2) Добавлена проверка: если DHCP-интерфейс имеет IP но нет default route, восстанавливать маршрут из lease-файла `/var/lib/dhcp/dhclient.<iface>.leases`.

---

## [2026-06-02 11:33] branch: 1.0.3.22

**Файл(ы):** `/etc/init.d/start_nodered`, `/etc/fstab`, `systemd`
**Тип:** Некорректное поведение (failed services)
**Описание:** На новом устройстве 3 failed сервиса: `start_nodered` (init-скрипт с синтаксической ошибкой), `postfix` (не нужен), `smartd` (нет SMART-дисков). Дублирующиеся записи в `/etc/fstab` для `/dev/sda1`.
**Причина:** Node-RED был удалён, но init-скрипт `/etc/init.d/start_nodered` остался. postfix и smartd установились как зависимости. fstab имел две записи для USB (ntfs-3g и exfat).
**Исправление:** Удалён `/etc/init.d/start_nodered`, postfix и smartd замаскированы, fstab приведён в соответствие с донором (только exfat для USB).

---

## [2026-06-02 11:33] branch: 1.0.3.22

**Файл(ы):** `/etc/systemd/system/sa02m-userspace-watchdog.service`
**Тип:** Отсутствующий компонент
**Описание:** На новом устройстве отсутствовал service-файл `sa02m-userspace-watchdog.service`, сервис не запускался.
**Причина:** При установке сервис не был скопирован (установщик не включал его явно).
**Исправление:** Скопирован с донора, включён через `systemctl enable --now`.

---

## [2026-06-02 10:37] branch: 1.0.3.22

**Файл(ы):** `.tmp/debug_eth.py` (история git), `.gitignore`
**Тип:** Другое (утечка секрета / GitGuardian)
**Описание:** GitGuardian: в репозитории обнаружен `chpasswd` с парой `root:cyntron` (push 2026-06-02 ~06:23 UTC).
**Причина:** В коммите `4cf136e` в git попала папка `.tmp/` с отладочным скриптом `echo 'root:cyntron' | chpasswd`; удаление из индекса в `36883a0` не стирало историю.
**Исправление:** `git filter-repo --path .tmp --invert-paths` — каталог `.tmp/` вырезан из всей истории; локальный `debug_eth.py` переведён на переменные окружения без паролей в коде. Требуется `git push --force-with-lease` и смена пароля root на устройствах.

---

## [2026-06-02 11:36] branch: 1.0.3.22

**Файл(ы):** `etc/fix-eth.sh`
**Тип:** Логическая ошибка
**Описание:** Каждый раз после перезагрузки через ~60 секунд сеть падала на 3 секунды (Link is Down / Link is Up). Воспроизводилось на обоих устройствах (192.168.1.136 и 192.168.1.113).
**Причина:** В `recover_iface()` был блок "early link bounce" — при первом вызове с carrier=UP скрипт делал `ip link set $iface down; sleep 0.1; ip link set $iface up` (маркер `bounce_done`). Это вызывало PHY renegotiation (~3с down) каждый reboot. Блок запускался через 60с после boot (STARTUP_DELAY=60 в net-watchdog), уже ПОСЛЕ того как networking.service назначил IP. Gratuitous ARP уже решает задачу обновления ARP-кэша без disruption.
**Исправление:** Удалён bounce_marker блок из `etc/fix-eth.sh`. Добавлено автовосстановление default route для DHCP-интерфейсов из lease-файла.

---

## [2026-06-02 11:36] branch: 1.0.3.22

**Файл(ы):** `etc/sa02m-flasher.service`
**Тип:** Ошибка конфигурации
**Описание:** На новых устройствах SA-02m веб-интерфейс на порту 9999 возвращал `502 Bad Gateway` для всех `/api/flasher/*`. nginx error.log: `connect() to unix:/run/sa02m-flasher/flasher.sock failed (13: Permission denied)`.
**Причина:** Сервис `sa02m-flasher` пытается выполнить `os.chown(socket_path, -1, www-data_gid)` чтобы nginx (www-data) мог подключиться к сокету. Для этого процесс должен принадлежать группе `www-data`. Однако в unit-файле `SupplementaryGroups=dialout` — без `www-data`. На донор-устройстве (.136) работало случайно (пользователь добавлен вручную), на новом .113 — нет. Итог: сокет оставался с группой `sa02m-flasher`, nginx получал EPERM.
**Исправление:** `SupplementaryGroups=dialout` → `SupplementaryGroups=dialout www-data` в `etc/sa02m-flasher.service`. Применено на .113: daemon-reload + restart. HTTP 200 подтверждён.

---

## [2026-06-02 11:36] branch: 1.0.3.22

**Файл(ы):** `scripts/02-network.sh`, `/etc/network/interfaces.d/end1.conf`
**Тип:** Логическая ошибка / Ошибка конфигурации
**Описание:** На 192.168.1.113 (SA-02m-2eth) после перезагрузки отсутствовал default route — нет выхода в интернет.
**Причина:** DHCP-сервер шлёт RFC3442 (option 121, classless static routes), что по стандарту заставляет `dhclient-script` игнорировать option 3 (routers). Lease-файл содержал `option routers 192.168.1.1`, но маршрут не применялся. В `end1.conf` не было `post-up` для принудительного добавления маршрута.
**Исправление:** 1) В `end1.conf` на .113 добавлено `post-up ip route replace default via 192.168.1.1 dev end1 metric 100 || true`. 2) В шаблоне `scripts/02-network.sh` добавлен тот же post-up и dhclient exit hook `/etc/dhcp/dhclient-exit-hooks.d/end1-default-route`. Default route немедленно восстановлен.

---

## [2026-06-02 10:10] branch: 1.0.3.22

**Файл(ы):** `www/network_config/index.html`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение / UI
**Описание:** Блок «Аппаратный вариант» отображался глобально над всеми вкладками; raw-содержимое `sa02m_serial_map.conf` выводилось пользователю; заголовок «СА-02м» не менялся при переключении на вариант sa02m-2eth.
**Причина:** `hw-variant-section` был размещён в `<main>` вне вкладок; `showSerialMap()` выводил base64-декодированный файл конфига; `applyVariantVisibility()` не обновляла заголовок в шапке.
**Исправление:** Блок перенесён первым элементом во вкладку «Управление» (`tab-system`); удалён `serial-map-info` div и функция `showSerialMap`; в `applyVariantVisibility()` добавлено обновление `#device-title`; `loadVariant()` вызывается при DOMContentLoaded и обновляет заголовок.

---

## [2026-06-02 10:02] branch: 1.0.3.22

**Файл(ы):** `www/network_config/index.html`, устройство `192.168.1.113:/etc/sa02m_serial_map.conf`
**Тип:** Некорректное поведение / Неверная конфигурация
**Описание:** Блок «Тип устройства» (hw-variant-section) располагался внизу вкладки «Управление». На устройстве 192.168.1.113 был установлен профиль `sa02m-1eth` (5 портов, ttyS0 включён) вместо корректного `sa02m-2eth` (4 порта: ttyS3/S4/S5/S7).
**Причина:** Блок был размещён в нижней части tab-system. На 192.168.1.113 профиль не был обновлён при смене аппаратного варианта.
**Исправление:** Блок hw-variant-section перенесён в самый верх `<main>` (вне вкладок), удалён пояснительный текст. На 192.168.1.113 пересозданы `/etc/sa02m_serial_map.conf`, udev-правила и симлинки COM1-4/RS-485-0..3 → ttyS3/S4/S5/S7.

---

## [2026-06-02 09:58] branch: 1.0.3.22

**Файл(ы):** `scripts/02-network.sh`
**Тип:** Логическая ошибка
**Описание:** На SA-02m-2 (2-eth) интерфейс end0 (статический) имел дефолтный metric 0, а end1 DHCP — metric 100. При отсутствии кабеля на end0 (NO-CARRIER) Linux всё равно выбирал маршрут через end0 как дефолтный (metric 0 < 100), и интернет через end1 не работал.
**Причина:** В `end0.conf` отсутствовал явный `metric`, поэтому Linux назначал metric 0 по умолчанию — ниже metric 100 у end1 DHCP.
**Исправление:** В `02-network.sh` при создании `end0.conf` на варианте `sa02m-2eth` добавляется `metric 200`, чтобы end1 DHCP (metric 100) всегда выигрывал дефолтный маршрут.

---

## [2026-06-02 09:58] branch: 1.0.3.22

**Файл(ы):** `scripts/lib.sh`
**Тип:** Логическая ошибка
**Описание:** `sa02m_hw_variant()` и `sa02m_serial_profile()` определяли вариант устройства по числу физических Ethernet-интерфейсов (`/sys/class/net/end*/device`). На A40i физически всегда два MAC-контроллера, поэтому автодетект всегда возвращал `sa02m-2eth` независимо от реального варианта.
**Причина:** Autodetect по числу ETH-интерфейсов неприменим для платформы Cyntron A40i, где оба Ethernet-контроллера присутствуют физически даже в однопортовом варианте устройства.
**Исправление:** Удалён autodetect из `sa02m_hw_variant()` — дефолт `sa02m-1eth`; `sa02m_serial_profile()` теперь делегирует в `sa02m_hw_variant()` вместо отдельной проверки `/sys/class/net/end1`. Вариант задаётся явно через переменную среды или `/etc/sa02m_hw_variant.conf`.

---

## [2026-06-02 09:47] branch: 1.0.3.22

**Файл(ы):** `scripts/lib.sh`, `install.sh`, `scripts/02-network.sh`, `scripts/01-system.sh`, `etc/sa02m_hw_variant.conf`
**Тип:** Новая функциональность
**Описание:** Установщик и образ не поддерживали два аппаратных варианта (SA-02m 1-eth и SA-02m-2 2-eth) — IP/шлюз/профиль COM были захардкожены под 1-eth.
**Причина:** Отсутствовал механизм определения варианта и различные дефолты для IP/GW/end1.
**Исправление:** Добавлены `sa02m_hw_variant()`, `sa02m_default_ip()`, `sa02m_default_gw()` в `lib.sh`; `--variant` флаг в `install.sh`; auto end1 DHCP в `02-network.sh`; запись `/etc/sa02m_hw_variant.conf` в `01-system.sh`; шаблон конфига `etc/sa02m_hw_variant.conf`.

---

## [2026-06-02 09:39] branch: 1.0.3.22

**Файл(ы):** `scripts/01-system.sh`, `/etc/udev/rules.d/99-com-aliases.rules`
**Тип:** Конфликт конфигурации udev
**Описание:** На устройствах присутствовал файл `99-com-aliases.rules` с маппингом COM-портов, который дублировал или конфликтовал с `99-sa02m-serial.rules`, генерируемым установщиком по профилю. На 192.168.1.136 файл содержал 5-портовый маппинг (ttyS0=COM1, ttyS3=COM2...), создавая дублирующие симлинки. При смене профиля (например с 1-eth на 2-eth) старый `99-com-aliases.rules` оставался и создавал конфликт: ttyS0=COM1 в нём против отсутствия ttyS0 в `99-sa02m-serial.rules` 2-eth профиля.
**Причина:** Установщик `01-system.sh` генерировал `99-sa02m-serial.rules` для нужного профиля, но не удалял устаревший `99-com-aliases.rules`, оставшийся от предыдущих установок или образа.
**Исправление:** Добавлен `rm -f /etc/udev/rules.d/99-com-aliases.rules` в `scripts/01-system.sh` после генерации `99-sa02m-serial.rules`. Файл удалён с обоих устройств (192.168.1.136, 192.168.1.113), выполнен `udevadm control --reload-rules`.

---

## [2026-06-02 09:36] branch: 1.0.3.22

**Файл(ы):** `etc/99-com-aliases.rules`
**Тип:** Некорректное поведение
**Описание:** При настройке нового устройства SA-02m (2-eth) симлинки COM2→ttyS3 (вместо ttyS4), COM5→ttyS7 (лишний), RS-485-1→ttyS3 (вместо ttyS4), RS-485-4→ttyS7 (лишний).
**Причина:** `99-com-aliases.rules` содержал маппинг 1-eth профиля (ttyS0=COM1, ttyS3=COM2...), который конфликтовал с `99-sa02m-serial.rules` 2-eth профиля (ttyS3=COM1, ttyS4=COM2...). udev обрабатывал оба файла, symlink назначался последним правилом что порождало некорректные ссылки.
**Исправление:** Обновлён `99-com-aliases.rules` под профиль sa02m-2eth: ttyS3=COM1/RS-485-0, ttyS4=COM2/RS-485-1, ttyS5=COM3/RS-485-2, ttyS7=COM4/RS-485-3. Пересоздано через udevadm trigger.

---

## [2026-06-02 09:33] branch: 1.0.3.22

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `apply.cgi`, `config.cgi`, `ssh_debug.cgi`, `www/network_config/index.html`, `www/network_config/static/js/app.js`, `etc/inet-failover.sh`, `etc/sa02m-mqtt-external-info.py`, `opt/sa02m-cloud-agent/sa02m-cloud-agent.py`, `etc/sa02m-net-autolink.sh`
**Тип:** Некорректное поведение
**Описание:** После миграции интерфейсов `eth0`→`end0` / `eth1`→`end1` ряд файлов в репозитории продолжал использовать старые имена интерфейсов, что привело бы к неработающему web-UI, cloud agent и failover-скриптам на мигрированных устройствах.
**Причина:** Частичная миграция из предыдущей задачи — обновлены были не все файлы репозитория; web CGI, app.js, cloud agent и inet-failover оставались с `eth0`/`eth1`.
**Исправление:** Полный поиск по репозиторию и замена всех функциональных вхождений `eth0`→`end0`, `eth1`→`end1`. Задеплоено на донора (192.168.1.136) с перезагрузкой (подтверждено: `end0` 192.168.1.136) и на целевое устройство (192.168.1.113, `end0`/`end1` уже были).

---

## [2026-06-02 09:14] branch: 1.0.3.22

**Файл(ы):** `install.sh`, `scripts/02-network.sh`, `scripts/01-system.sh`, `scripts/lib.sh`, `etc/99-lan-recovery.rules`, `etc/net-watchdog.sh`, `etc/inet-failover.sh`, `etc/fix-eth.sh`, `etc/fix-eth.service`, `etc/sa02m_network.conf`, `etc/sysctl.d/60-sa02m-net.conf`, `etc/sa02m-eth0-led.sh`, `etc/sa02m-pre-start.sh`, `etc/sa02m-userspace-watchdog.sh`, `etc/sa02m-grat-arp.py`, `etc/cron.d/sa02m-arp`, `etc/sa02m-mqtt-external-info.py`, `etc/sa02m-net-autolink.sh`, `etc/systemd/sa02m-net-autolink.service`, `README.md`
**Тип:** Рефакторинг / Некорректное поведение
**Описание:** После переноса образа SA-02m на другое устройство с другими MAC-адресами сеть не поднималась: link-файлы `/etc/systemd/network/10-eth0.link` и `11-eth1.link` переименовывали интерфейсы по старым MAC → имена не назначались → конфиг ifupdown не применялся.
**Причина:** MAC-based переименование через link-файлы жёстко привязывало интерфейсы к конкретному устройству. Имена `eth0`/`eth1` были захардкожены по всей конфигурации.
**Исправление:** Переход на стабильные предсказуемые имена `end0`/`end1` (по аппаратному пути, без MAC): удалены link-файлы с устройств, все конфиги переименованы (`eth0.conf`→`end0.conf`, `eth1.conf`→`end1.conf`), все скрипты обновлены на `end0`/`end1`. Сервис `sa02m-net-autolink` задепрекейтен и замаскирован.

---

## [2026-06-02 09:14] branch: 1.0.3.21

**Файл(ы):** `etc/sa02m-net-autolink.sh`, `etc/systemd/sa02m-net-autolink.service`, `scripts/01-system.sh`
**Тип:** Новая функциональность / Устранение проблемы переноса образа
**Описание:** При переносе образа SA-02m на другое устройство `/etc/systemd/network/10-eth0.link` и `11-eth1.link` содержали MAC-адреса донора — интерфейсы не переименовывались в `eth0`/`eth1`, сеть не работала без ручного вмешательства.
**Причина:** link-файлы hardcode MAC-адрес конкретного устройства; при клонировании образа MAC меняется, но файлы остаются без изменений.
**Исправление:** Добавлен сервис `sa02m-net-autolink` (`Before=systemd-networkd.service`, `DefaultDependencies=no`), который при каждой загрузке сравнивает MACs физических интерфейсов с записанными в link-файлах и обновляет их при расхождении. Для Cyntron A40i-2Eth использует детерминированное сопоставление по MAC-префиксу (`02:53:` → `eth0`, `12:53:` → `eth1`). Скрипт идемпотентен. Установлен через `scripts/01-system.sh`.

---

## [2026-06-01 16:30] branch: main

**Файл(ы):** `tools/imaging/flash-receiver.sh`, `etc/storage-mount.sh`, `tools/imaging/make-image.sh`
**Тип:** Логическая ошибка (три связанных бага)
**Описание:** Образы не устанавливались корректно на новые устройства ни через USB-флешку, ни через ImageUSB.

**Причина 1 — flash-receiver.sh (критическая):**
`IMG_DIR` захардкожен как `/mnt`, но USB-накопитель на SA-02m монтируется в `/media/usb` (через udev + storage-mount.sh). Скрипт не находил образ и падал с `FATAL: образ не найден`.

**Причина 2 — storage-mount.sh (критическая):**
После монтирования USB нет автозапуска `autorun.sh`. Скрипт лежит на флешке, но его никто не вызывает — ни udev, ни storage-mount.sh.

**Причина 3 — make-image.sh (критическая):**
После применения watchdog-фикса (`apply_watchdog_fix.py`) на живом доноре unit-файлы watchdog сервисов заменяются на noop (`ExecStart=/bin/true`). При следующем снятии образа эти noop-файлы попадают в образ. На новом устройстве watchdog (net-watchdog, userspace-watchdog, failure-monitor) не работает — только притворяется enabled.

**Исправление:**
- `flash-receiver.sh`: `IMG_DIR` = `$(dirname $(readlink -f $0))` — автоопределение по директории скрипта
- `storage-mount.sh`: после успешного монтирования USB — проверка наличия `autorun.sh` и запуск в фоне
- `make-image.sh`: патч watchdog unit-файлов через loop-mount после PiShrink (восстановление из репо + RuntimeWatchdogSec через drop-in)

---

## [2026-06-01 10:25] branch: main

**Файл(ы):** `etc/sa02m-web-auth-lib.sh`, `etc/sa02m-repair-web-env.sh`, `etc/sa02m-commit-web-env.sh`, `www/network_config/cgi-bin/login.cgi`, `www/network_config/cgi-bin/web_creds.cgi`, `www/network_config/cgi-bin/lib_web_auth.sh`, `scripts/03-webserver.sh`
**Тип:** Логическая ошибка
**Описание:** `/etc/sa02m_web.env` мог содержать буквальную строку `$(printf …)` вместо пароля; при `source` файла выполнялась подстановка команд.
**Причина:** Запись пароля без кавычек и чтение через `. "$AUTH"` — любое `$(…)` в значении интерпретировалось shell; в файл мог попасть shell-код (например из автоматизации).
**Исправление:** Библиотека `web_auth_*`: quoted-формат `SA02M_WEB_PASS='…'`, безопасное чтение без eval, запрет shell-символов в новом пароле, `sa02m-repair-web-env` для нормализации повреждённых файлов при install/update.

---

**Файл(ы):** `scripts/03-webserver.sh`, `scripts/update-www-only.sh`, `etc/nginx/network_config.conf`, `www/network_config/cgi-bin/web_update_apply.cgi`, `scripts/01-system.sh`
**Тип:** Некорректное поведение
**Описание:** Обновление веб-UI из GitHub через вкладку «Управление» на чистой установке не работало: кнопка «Применить» не запускала apply; ручная «Проверить обновления» давала 504; при сбое apply UI показывал устаревший status «done»; dhclient на USB-модеме падал из-за CRLF в exit-hook.
**Причина:** `sa02m-web-update-apply` не устанавливался и не был в sudoers (только check); nginx fastcgi_read_timeout 20s для force-check; `web_update_apply.cgi` читал старый `update_status` при неудачном sudo; `sa02m-modem-metric` без strip CRLF после install с Windows-репозитория.
**Исправление:** Установка apply-скрипта и NOPASSWD в `03-webserver.sh`/`update-www-only.sh`; отдельный location nginx для `web_update_check.cgi` с таймаутом 60s; при неудачном старте apply — status error; `sed -i 's/\r$//'` для dhclient exit-hook в `01-system.sh`.

---

**Файл(ы):** `scripts/03-webserver.sh`, `README.md`
**Тип:** Логическая ошибка
**Описание:** После установки на чистый Ubuntu/Debian все CGI-запросы возвращали 502 Bad Gateway. Веб-интерфейс отображал сырой HTML страницы ошибки nginx вместо данных (манифест прошивок, журнал и т.д.).
**Причина:** На чистом Debian/Ubuntu `apt install fcgiwrap` автоматически запускает stock `fcgiwrap.socket`, который создаёт сокет по пути `/run/fcgiwrap.socket`. `03-webserver.sh` детектировал этот сокет и патчил nginx.conf (`unix:/run/fcgiwrap/fcgiwrap.socket` → `unix:/run/fcgiwrap.socket`). Затем устанавливался наш кастомный `fcgiwrap.service` (сокет `/run/fcgiwrap/fcgiwrap.socket`), stock socket отключался. В итоге nginx смотрел на `/run/fcgiwrap.socket` которого больше нет → 502.
**Исправление:** Убрана ACTIVE_FCGI-детекция сокета из `03-webserver.sh` (она подходит только для legacy, но всегда перебивается нашим сервисом). Добавлены явные `stop/disable/mask` для `fcgiwrap.socket` и `fcgiwrap@.socket`, удаление осиротевших файлов сокетов. Добавлена верификация сокета после старта. README обновлён: убран совет делать `apt install fcgiwrap` перед `install.sh`, добавлен раздел диагностики и исправления 502.

---

## [2026-05-30 15:32] branch: 1.0.3.20

**Файл(ы):** `opt/sa02m-serial-gateway/serial_gateway.py`
**Тип:** Некорректное поведение
**Описание:** Шлюз RS-485 (Modbus TCP / RTU over TCP) на COM4 принимал TCP-соединения, но все запросы завершались Modbus exception 0x0A/0x0B; `bytes_rx=0`, счётчик ошибок рос.
**Причина:** `SerialWorker.exchange()` читал RTU по таймауту тишины без паузы после TX (переключение RS-485), без отсечения эха запроса в RX и без определения длины Modbus-кадра — в отличие от `sa02m-flasher` и `modbus_mqtt_bridge`.
**Исправление:** Добавлены `_modbus_read_frame_len`, `_strip_rtu_echo`, `_rtu_char_time_s`; `exchange()` переписан по той же логике, что в `send_receive()` flasher/bridge (post-send delay, echo strip, early exit по длине кадра, inter-frame gap).

## [2026-05-29 16:31] branch: 1.0.3.19

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/service.py`, `opt/sa02m-flasher/sa02m_flasher/config.py`, `opt/sa02m-flasher/sa02m_flasher/mplc_lease.py`, `etc/sa02m_flasher.conf`, `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/flasher.js`, `www/network_config/static/js/mqtt.js`
**Тип:** Инициализация журнала
**Описание:** Первичная точка отсчёта — зафиксированы файлы с текущими изменениями (modified) на момент введения BUGLOG.
**Причина:** —
**Исправление:** Журнал создан. Все последующие баги и исправления будут документироваться в этом файле согласно правилу `bug_log_workflow.mdc`.
