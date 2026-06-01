# Bug Log

Документация найденных и устранённых ошибок по проекту SA-02m.
Формат: дата/время, ветка, файл, тип, описание, причина, исправление.
Новые записи добавляются **сверху**.

---

## [2026-06-01 10:20] branch: main

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
