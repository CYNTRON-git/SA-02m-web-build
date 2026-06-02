# Bug Log

Документация найденных и устранённых ошибок по проекту SA-02m.
Формат: дата/время, ветка, файл, тип, описание, причина, исправление.
Новые записи добавляются **сверху**.

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
