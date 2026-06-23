# Bug Log

Документация найденных и устранённых ошибок.
Формат: дата/время, ветка, файл, тип, описание, причина, исправление.

---

## [2026-06-23 13:53] branch: 1.0.3.34

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `etc/sa02m-web-service-ctl.sh`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** В «Сведения → Службы» Docker показывал аптайм (~15 м), но badge «Неактивен»; в «Управление → Службы» не было Docker.
**Причина:** `fast_service_state` для docker.service вызывал `svc_is_active`, который ищет процесс `pgrep -x docker`; демон — `dockerd`. Управление: unit не был в `SERVICE_DEFS`.
**Исправление:** В `status.cgi` — ветка `docker|docker.service` (проверка `dockerd`, `/run/docker.sock`); аптайм по `dockerd`. В `sa02m-web-service-ctl.sh` — `docker|Docker|docker.service`; UI label в `app.js`.

## [2026-06-23 12:29] branch: 1.0.3.33

**Файл(ы):** `tools/buildroot/prepare-rt-docker-kernel.sh`, FAT boot `zImage`
**Тип:** Краш / Логическая ошибка
**Описание:** RT zImage (6277688 B) не загружался — устройство не поднималось по сети после деплоя 2026-06-23.
**Причина:** `apply_kconfig_snippets` + `olddefconfig` сбрасывали `CONFIG_ARCH_SUNXI` и `CONFIG_MMC_SUNXI` — ядро собиралось как generic ARM multipatform без sunxi/eMMC/ethernet. В modules.builtin RT не было `sunxi-mmc.ko`, `dwmac-sun8i.ko`.
**Исправление:** В `prepare-rt-docker-kernel.sh` добавлены `apply_sa02m_boot_kconfig` (sunxi/mmc/ethernet/LED/RTC) и `verify_sa02m_boot_kconfig`; повторное применение после `olddefconfig`. Пересборка: zImage 6704664 B с `PREEMPT_RT`, `ARCH_SUNXI`, `MMC_SUNXI`. Деплой на FAT; откат SMP сохранён в `zImage.bak-smp-*`. Symlink `/lib/modules/6.1.0-rc6-rt4-rt4` → `6.1.0-rc6-rt4` (двойной суффикс из LOCALVERSION, исправлен в скрипте).

## [2026-06-23 13:30] branch: 1.0.3.33

**Файл(ы):** `tools/buildroot/prepare-rt-docker-kernel.sh`, `/etc/docker/daemon.json`
**Тип:** Некорректное поведение
**Описание:** Docker не стартовал на RT-ядре: NAT/addrtype/conntrack/BPF/raw table отсутствовали; `olddefconfig` сбрасывал sunxi и netfilter.
**Причина:** Kconfig snippets применялись до/после `olddefconfig` без повторного merge; `yes | make` + `pipefail` обрывал скрипт; не хватало NF_NAT, xt_conntrack, BPF, IP_NF_RAW.
**Исправление:** `apply_sa02m_boot_kconfig`, `apply_docker_netfilter_kconfig`, verify-хуки; iptables-legacy + `daemon.json` (ipv6 off); пересборка RT zImage с полным netfilter/BPF. `docker run --rm hello-world` OK.

## [2026-06-23 11:59] branch: 1.0.3.33

**Файл(ы):** FAT boot `/dev/mmcblk2p1` (zImage), `tools/buildroot/prepare-rt-docker-kernel.sh`
**Тип:** Краш / Некорректное поведение
**Описание:** После деплоя RT zImage (6.1.0-rc6-rt4, 6277688 B) устройство не поднялось по сети; SSH timeout, ping ~50%.
**Причина:** RT-образ на FAT не загрузился стабильно (возможны несовместимость defconfig/DTB с SA-02m или сеть end0/end1 до полного boot).
**Исправление:** Откат через U-Boot USB mass storage (диск F:): `zImage` восстановлен из `zImage.bak-smp-20260623` (6298696 B, MD5 совпадает с `device_boot`). `boot.scr` и `sun8i-a40i-sk.dtb` без изменений.

## [2026-06-22 11:55] branch: 1.0.3.32

**Файл(ы):** `www/network_config/static/js/mqtt.js`, `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** В MQTT-вкладке блок «Системные» — «Время работы» (uptime_s) не менялся в live-ячейке; остальные diag-поля тоже обновлялись редко.
**Причина:** Мост опрашивал uptime только в `_poll_diag` (`poll_diag_s=60`); при backoff IO-опроса `poll_slow_if_due` тоже пропускался. UI показывал снимок без экстраполяции — между опросами секунды не тикали.
**Исправление:** Отдельный `_poll_uptime` каждые 5 с (`poll_uptime_s`); `poll_slow_if_due` выполняется даже в backoff; UI: якорь uptime + 1 с tick и экстраполяция в `updateLiveCell`; cache-bust `mqtt.js?v=1.0.3.32` (ветка, без суффиксов).

## [2026-06-22 11:55] branch: 1.0.3.32

**Файл(ы):** `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`, `opt/sa02m-flasher/sa02m_flasher/device_config.py`
**Тип:** Некорректное поведение
**Описание:** После power cycle в MQTT UI «Системные» и прошивальщик показывали `reset_reason=WWDG`, хотя модуль перезагружался отключением питания.
**Причина:** MR-02m кодирует причину в Input 65508 по `decode_reset_csr` (1=LPWR, 2=WWDG, …, 5=POR, 6=NRST — см. `MODBUS_VARIABLES.txt`). Мост и flasher использовали другую таблицу (похожую на порядок RCC-битов STM32): код **5** ошибочно отображался как WWDG вместо POR/PDR. На устройстве MQTT: `/devices/mr02m-COM4-6/controls/reset_reason WWDG` при фактическом коде 5. Прошивка и регистр 65508 корректны; stale cache не при чём (`poll_diag_s=60` обновляет значение).
**Исправление:** Таблица `MR_RESET_REASON_LABELS` / `_info_reset_reason` приведена к кодам MR-02m; декодирование reset reason — младший байт регистра (`& 0xFF`), т.к. старший может нести SPI fault.

---

## [2026-06-22 11:42] branch: 1.0.3.32

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** Виджеты «Дискретный выход», «USB-питание» и «индикация» (DO/Beeper/Alarm LED/USB) оставались «н/д» при `SA02M_STATUS_ENABLE_HARDWARE=0`.
**Причина:** Backend уже отдаёт TTL-кэш через `part=hardware` (`hw_poll_disabled=1`, `sa02m_hw_metrics_cache_refresh`), но frontend при `hardware=0` в blocks помечал часть загруженной и не вызывал `fetchBackgroundPart('hardware')` — `applyHardwareStatus` не получал JSON. Кэш на устройстве мог содержать устаревшие `-1` до первого запроса part=hardware.
**Исправление:** `shouldFetchBackgroundPart()`: GPIO-виджеты опрашиваются всегда; при `hardware=0` CGI по-прежнему использует кэш без тяжёлого I2C на каждый main. Cache-bust `app.js?v=1.0.3.32.4`.

---

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `.tmp_deploy_poll_queue.py`, `.tmp_deploy_status_cgi_fix.py`
**Тип:** Некорректное поведение
**Описание:** Дашборд «Сведения» — все виджеты «—», данные не обновлялись; nginx access log: HTTP 403 на все `GET /cgi-bin/status.cgi?part=*`.
**Причина:** `status.cgi` на устройстве имел права `644` (не исполняемый) после деплоя через `.tmp_deploy_poll_queue.py`, который делал `chmod 644` на все загруженные файлы, включая CGI. fcgiwrap возвращал 403 «Cannot get script name… is the script executable?»; JS получал HTTP 403, `noteStatusFailure()` накапливал ошибки.
**Исправление:** `chmod 755` на устройстве; git index `+x` для `status.cgi`; деплой-скрипт poll_queue — отдельно `chmod 755` для `*.cgi`; `.tmp_deploy_status_cgi_fix.py` с HTTP-проверкой всех part=*; cache-bust `app.js?v=1.0.3.32.3`. Проверка: все part=* → HTTP 200 через nginx.

---

## [2026-06-22 10:50] branch: 1.0.3.32

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** Алерт «Обновление основных виджетов приостановлено из-за ошибок ответа status.cgi» появлялся при рабочем CGI; после 5 сбоев отдельных part=* опрос не восстанавливался.
**Причина:** `noteStatusFailure()` накапливал счётчик `statusFailures.main` от любых background-частей, а `noteStatusSuccess(part)` сбрасывал только `statusFailures[part]` — `main` и алерт не очищались без успешного `fetchStatusMain()`. Отменённые (superseded) AbortError и очередь могли давать ложные таймауты при фиксированном budget.
**Исправление:** Порог паузы per-part (`statusFailures[part] >= 5`); успех любой background-части сбрасывает `statusFailures.main` и скрывает алерт; `isBenignStatusFetchError()` для stale/superseded abort; timeout += queue wait (cap 2×); cache-bust `app.js?v=1.0.3.32.2`. Диагностика на устройстве: все part=* через CGI OK (2–6 s), fcgiwrap/nginx active.

---

## [2026-06-22 10:43] branch: 1.0.3.32

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** Вкладка «Сведения» пустая — CPU, RAM, сеть, службы, RS-485 не заполнялись после введения глобальной очереди опроса status.cgi.
**Причина:** `fetchStatusBlocksConfig()` при ошибке/таймауте `part=blocks` выставлял `_statusBlocksConfig = { time: 0 }`; `isStatusBlockEnabled()` трактовал отсутствующие ключи как «выключено» → все background-части и rs485 пропускались. Инициализация опроса ждала blocks без таймаута — при зависании blocks polling не стартовал вообще.
**Исправление:** При сбое blocks config оставлять `null` (дефолт: всё кроме time); таймаут 4 s на fetch blocks; старт polling через 600 ms не дожидаясь blocks; явные 0/false в конфиге; cache-bust `app.js?v=1.0.3.32.1`.

---

**Файл(ы):** `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`, `www/network_config/static/js/mqtt.js`, `opt/sa02m-flasher/sa02m_flasher/module_profiles.py`, `/etc/sa02m-modbus-mqtt.yaml` (COM4 mr02m-COM4-6)
**Тип:** Некорректное поведение
**Описание:** MQTT-вкладка COM4 6AI6AO показывала неверные значения AI (~−14/−200/−55 °C), хотя окно настройки модуля — корректные (~25 °C); типы в YAML были legacy enum (1/2/28/6).
**Причина:** YAML содержал старые коды `ai_sensor_t` (0x01=NTC10k→новый 3, 0x02=Pt1000→11, 0x1C=Pt100 3w→22, 0x06=ТХА→41). Мост записывал их в holding reg0 и при опросе предпочитал YAML/legacy регистр вместо Modbus selection codes; неверный тип ломал N-leg mirroring (TXA/3-wire).
**Исправление:** Таблица миграции legacy→0..42 + `ai_sensor_schema: 2`; мост: `_resolve_ai_sensor_type()` (YAML над legacy в reg), перезапись legacy reg0; mqtt.js: миграция при loadConfig и при live legacy; YAML на устройстве: 3/11/22/22/41/41. Проверено: cache sensor_types 3/11/22/41, ai_1≈25.0 °C.

---

## [2026-06-22 10:35] branch: 1.0.3.32

**Файл(ы):** `www/network_config/static/js/flasher.js`, `opt/sa02m-flasher/sa02m_flasher/device_config.py`, `opt/sa02m-flasher/sa02m_flasher/service.py`, `opt/sa02m-flasher/tests/test_device_config.py`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** В окне настройки модуля RS-485 при смене типа AI (AI3 и др.) периодически «AI3: Окно настройки доступно только для устройств нашей линейки» или «линия занята другим процессом»; запись на устройство при этом часто успешна.
**Причина:** Параллельные `/device_config/*` (фоновый panel-poll + write holding + лишний full-refresh после apply) давали коллизии Modbus; при сбое live-сигнатуры snapshot после записи не использовал сигнатуру из scan-записи; fd опросчика мог освобождаться с задержкой после ports/release.
**Исправление:** Очередь device_config в flasher.js + edit-guard селекта AI; убран дублирующий refresh после apply; per-port lock в sa02m-flasher service; `_resolve_kind()` с fallback на scan device; повторная проверка port_occupants после 60 ms.

---

## [2026-06-22 10:35] branch: 1.0.3.32

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/cgi-bin/status.cgi`, `www/network_config/login.html`
**Тип:** Некорректное поведение
**Описание:** CPU периодически достигал 100% при открытом дашборде несмотря на rolling scheduler (6 s / 750 ms): 9–16 одновременных `status.cgi`, idle CPU падал до ~9%.
**Причина:** Независимые таймеры без глобальной сериализации — тяжёлые части (rs485 cold ~4 s, network ~0.7 s, services) перекрывались по времени выполнения; priority (4 s) и rs485 (8 s, phase 3300 ms) периодически совпадали с network/services; `login.html` prefetch — 4 отдельных CGI каждые 5 s; network без server cache; bootstrap каждые 1.2 s мог дублировать rs485.
**Исправление:** Глобальная очередь `scheduleStatusFetch` (max 1 CGI in-flight, min gap 350 ms, heavy gap 2200 ms); split LIGHT (6 s) vs HEAVY (12 s) с фазами; priority 6 s, rs485 12 s phase 10500 ms; bootstrap отложен 4 s, интервал 2.5 s, одна часть за тик; login — один `part=priority` / 8 s; backend cache: network TTL 8 s, rs485 8 s, services 45 s.

---

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** Высокая нагрузка CPU от веб-дашборда: тяжёлый `part=rs485` каждые 4 с с `no_cache=1`, блокирующий `sleep 0.1` в `cpu_usage()`, лишний опрос `part=time` при `SA02M_STATUS_ENABLE_TIME=0`.
**Причина:** RS-485 без server-side cache; routine fetch всегда bust кэша; `cpu_usage()` ждал 100 ms на каждый priority-запрос; frontend не знал об отключённых status blocks.
**Исправление:** `cache_print_or_build` для rs485 (TTL 4 s) + `no_cache=1` только при force; `cpu_usage()` — Option C (/tmp sample, delta без sleep при baseline <2 s, иначе sleep 50 ms); `part=blocks` + пропуск disabled частей; RS-485 poll 8 s, phase 3300 ms; routine rs485 без `no_cache=1`.

---

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** При возврате на вкладку дашборда и после обновления списка служб — лишний staggered burst `fetchStatusMain()` (до 8 CGI), накладывающийся на rolling timers и дававший кратковременные пики concurrency.
**Причина:** `visibilitychange` вызывал `fetchStatus()` → `fetchStatusMain()` + `fetchStatusRs485()`; `loadServicesControl` после refresh — полный `fetchStatus()` вместо точечного обновления services.
**Исправление:** На `visibilitychange` — только `fetchPriorityPart('priority')`; после refresh служб — `fetchBackgroundPart('services', …)` + priority; cache-bust `app.js?v=1.0.3.31-4`.

---

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** Загрузка CPU на дашборде циклически скачет 15%→97% при открытом веб-интерфейсе.
**Причина:** `fetchStatusMain()` каждые 6 с запускал 8 параллельных `status.cgi?part=*`; плюс priority (4 с) и rs485 (4 с) — до 10 одновременных bash/CGI; `cpu_usage()` в `status.cgi` измеряет CPU в окне 100 ms и фиксирует этот burst как ~97% aggregate.
**Исправление:** Rolling scheduler — у каждой background-части свой `setInterval` 6 s с фазовым сдвигом 750 ms; priority/rs485 со сдвигами 0/2100 ms; force-refresh через staggered burst; bootstrap опрашивает только незагруженные части по одной; cache-bust `app.js?v=1.0.3.31-3`.

---

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** На вкладке «Сведения» виджеты накопителей, системы и Ethernet долго пустые, затем все данные появляются одновременно.
**Причина:** Все фоновые виджеты запрашивались одним блокирующим `status.cgi?part=main`; `fetchStorageWidget` / `fetchNetworkWidget` / `fetchSystemWidget` и др. вызывали `fetchMainBundle()`, DOM обновлялся только после полного ответа (I2C, службы, сеть последовательно на сервере).
**Исправление:** Параллельный опрос `part=storage|time|uptime|network|load|system|services|hardware`; каждый виджет обновляется сразу по приходу своего JSON; координатор `fetchStatusMain()` вместо монолитного main; cache-bust `app.js?v=1.0.3.31-2`.

---

**Файл(ы):** `www/network_config/cgi-bin/mqtt_config.cgi`, `www/network_config/static/js/mqtt.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** При сохранении найденных MQTT-устройств — «Ошибка сохранения: неизвестная»; таблица устройств пустая, хотя в `/etc/sa02m-modbus-mqtt.yaml` уже есть mr02m-COM4-6/8.
**Причина:** После деплоя DI-счётчиков `mqtt_config.cgi` получил `chmod 644` (не исполняемый); fcgiwrap/nginx возвращали HTTP 403 «Cannot get script name… is the script executable?». `loadConfig()` молча не загружал YAML; `saveAndApply()` через `.catch(() => null)` показывал «неизвестная» без текста ошибки.
**Исправление:** `chmod 755` на CGI (install script уже 0755); git index `+x` для `mqtt_config.cgi`; `apiPost` возвращает `{ok:false,error:'HTTP N'}` при не-JSON/403; toast при сбое `loadConfig`; cache-bust mqtt.js `v=1.0.3.31-6`.

---

## [2026-06-19 14:12] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/lib_hw.sh`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** Кнопка «Сброс» USB-питания отключала VBUS и не включала его обратно; статус показывал «ВКЛ» при реально выключенном питании.
**Причина:** `gpioset` запускался через `sudo … &` — в pidfile попадал PID `sudo`, а не `gpioset`; при повторном включении старый `gpioset` не убивался (линия busy), state-файл обновлялся до «1» без реального удержания линии; UI оптимистично показывал ВКЛ через `pendingUsbPowerVal=1`.
**Исправление:** Поиск/остановка всех `gpioset` по chip/line; ожидание и верификация holder перед commit; pid/state через `sudo tee`; read из cmdline живого gpioset; лог и patch кэша при OFF/ON в reset async; UI держит «ВЫКЛ» на время сброса и опрашивает сервер после 10 с.

---

## [2026-06-19 15:28] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/static/js/i18n.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** В блоке «Прошивка выбранных устройств» таблица показывала все записи манифеста, включая несскачанные — пустые/placeholder строки с пометкой «не скачан».
**Причина:** `renderFirmware()` выводил все `state.firmware` из API без фильтра по `entry.downloaded`; авто-выбор мог выбирать несскачанную запись.
**Исправление:** `visibleFirmwareEntries()` — только downloaded; пустой список с подсказкой «Скачать»/«Выбрать»; «Скачать» — refresh+download, toast по числу файлов в кеше; `pickFirmwareToAutoSelect` только среди скачанных; cache-bust flasher.js `v=1.0.3.31-5`.

---

## [2026-06-19 14:41] branch: main

**Файл(ы):** `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`, `www/network_config/static/js/mqtt.js`, `www/network_config/cgi-bin/mqtt_config.cgi`
**Тип:** Некорректное поведение
**Описание:** В разделе MQTT у DI-каналов колонка счётчиков показывала «—» вместо значений с шины.
**Причина:** Цепочка: (1) мост ранее публиковал `di_N_count` только при `counter: true` в YAML и только в `_poll_diag` (60 с); конфиг устройств часто `channels: {}` — флаг counter не доходил до бэкенда; (2) UI скрывал колонку счётчика при `counter: false` и не обновлял live-ячейку при включении чекбокса; (3) `restart: true` из POST сохранялся в YAML.
**Исправление:** `_poll_di_counters()` в `_poll_do_di` (каждый poll_s), batch FC04 Input Reg 77+; UI всегда показывает live `di_N_count`, чекбокс «счётчик» по умолчанию включён, `syncMr02mDiCounterFlagsFromDom` перед сохранением; `mqtt_config.cgi` отбрасывает `restart` при записи YAML.

---

**Файл(ы):** `www/network_config/static/js/flasher.js`, `opt/sa02m-flasher/sa02m_flasher/firmware_repo.py`, `opt/sa02m-flasher/sa02m_flasher/service.py`
**Тип:** Некорректное поведение
**Описание:** После «Очистить» файлы прошивок удалялись из кеша, но таблица в UI не обновлялась — записи оставались как скачанные, выбор не сбрасывался.
**Причина:** UI полагался на отдельный GET `/firmware` после POST `/firmware/clear`; `applyFirmwareListChanges` не обрабатывал переход downloaded→false и удаление upload-записей; `isFirmwareEntryDownloaded()` для `local`/`upload` всегда возвращал true; API clear не возвращал актуальный список entries.
**Исправление:** POST `/firmware/clear` возвращает полный status с entries; `clearFirmwareCache()` применяет ответ сразу и перерисовывает список; добавлены `applyFirmwareStatusPayload`, обработка newlyUndownloaded/removed; `isFirmwareEntryDownloaded` читает только `entry.downloaded`; в `list_entries()` синхронизация downloaded с диском.

---

## [2026-06-19 14:04] branch: main

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/static/js/i18n.js`
**Тип:** Некорректное поведение
**Описание:** Описания прошивок в блоке «Устройства RS-485» оставались на русском при переключении языка на English.
**Причина:** `firmwareEntryDescription()` формировал жёстко зашитые RU-строки в `innerHTML`; при смене локали i18n обновлял только статический DOM, без перерисовки списка прошивок.
**Исправление:** Описания собираются через `t()`/`sa02mI18n.t` с ключами в DICT; добавлен `window.flasherRerenderFirmware`, вызываемый из `updateControl()` при смене языка.

---

## [2026-06-19 14:07] branch: main

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/firmware_repo.py`, `www/network_config/static/js/flasher.js`, `www/network_config/index.html`, `opt/sa02m-flasher/tests/test_firmware_repo.py`
**Тип:** Некорректное поведение
**Описание:** Повторная загрузка уже существующего .fw создавала дубликат `filename.2.fw`; обновлённая прошивка оставалась внизу списка.
**Причина:** `add_upload()` в цикле `while target.exists()` добавлял суффикс `.N` перед расширением; UI не поднимал запись при перезаписи того же `channel::file` (ключ уже был в списке).
**Исправление:** Перезапись файла с тем же именем + удаление старых `.2/.3` дубликатов; `applyFirmwareListChanges` отслеживает изменение sha256/size/version; `pruneFirmwareDisplayOrder` убирает устаревшие ключи; cache-bust flasher.js `v=1.0.3.31-4`.

---

## [2026-06-19 14:00] branch: main

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** В окне настройки модуля вкладки появлялись только после полного опроса Modbus; на вкладке «Сведения» плитки МК (питание, температура, uptime) не обновлялись автоматически.
**Причина:** `openConfigModal` не рендерил вкладки до ответа `/device_config/snapshot` (full); фоновый опрос `panel` читал `mr.mcu`, но `mergeMrMinimalIntoFull` не сливал поле `mcu` в кеш снимка.
**Исправление:** Stub-снимок из сигнатуры сканирования (`buildConfigSnapshotStubFromDevice` + `SIGNATURE_IO_HINTS`) — вкладки сразу; слияние `mcu` при panel/minimal merge; повторное планирование опроса при занятости порта.

---

## [2026-06-19 13:17] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/static/js/i18n.js`, `opt/sa02m-flasher/sa02m_flasher/firmware_repo.py`
**Тип:** Некорректное поведение
**Описание:** При нажатии «Скачать прошивки» без интернета показывались технические сообщения вроде «Манифест: HTTP 502» или длинные DNS-подсказки.
**Причина:** Frontend добавлял префикс «Манифест:» к сырому `err.message` / `res.error`; backend возвращал технические HTTP/DNS строки без единого пользовательского текста для offline.
**Исправление:** `_format_network_error` и UI маппят DNS/timeout/502–504 → «Нет доступа к интернету»; прочие ошибки — короткие RU-сообщения без префикса «Манифест»; добавлены строки i18n EN.

---

## [2026-06-19 13:07] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/flasher.js`
**Тип:** Логическая ошибка
**Описание:** Справочные пределы калибровки для 50П (12/25) и 50М (15/28) совпадали с Pt50 α385 (−200…300 °C), а не с α391/α428.
**Причина:** При добавлении кодов 50P/50M лимиты скопированы от Pt50 (коды 8/21), хотя 50П/50М используют `rtd_391`/`rtd_428` с R₀=50 Ω — диапазоны как у 100П/100М (`table_rtd_alpha.h`).
**Исправление:** 12/25 → −200…850 °C; 15/28 → −180…200 °C (десятые °C: −2000/8500 и −1800/2000).

---

## [2026-06-19 13:01] branch: 1.0.3.31

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/module_profiles.py`, `www/network_config/static/js/flasher.js`, `www/network_config/static/js/mqtt.js`, `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`
**Тип:** Некорректное поведение
**Описание:** Веб/MQTT/прошивальщик использовали старые коды типов AI (enum ai_sensor_t 0x00–0x26) вместо Modbus selection codes 0–42; отсутствовали 50П (12/25) и 50М (15/28).
**Причина:** MR-02m ≥1.0.9.1 изменил порядок кодов в регистре «тип датчика»; SA-02m-web-build не был синхронизирован.
**Исправление:** Единая таблица 0–42 по `MR-02m/README.md`; обновлены подписи, bucket/3-wire множества, масштабы MQTT-моста, лимиты калибровки; добавлены 50P/50M по аналогии с Pt100.

---

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/static/css/main.css`
**Тип:** Некорректное поведение
**Описание:** Карточки RS-485 (в т.ч. RS-485-3 / COM4) и числа TX/RX подсвечивались cyan при каждом обновлении опроса — отвлекало на активных линиях.
**Причина:** `renderRs485()` сравнивал TX/RX с `_prevRs`, добавлял класс `.act` на карточку (1.8 с) и на `<span class="rv act">`; CSS `.rs485-port.act` и `.rs485-row .rv.act` давали cyan border и текст.
**Исправление:** Удалены `_prevRs`, логика `actNow`, таймер `_actTimer` и CSS `.rs485-port.act` / `.rv.act`. TX/RX всегда `color: var(--text)`. Hover-подсветка границ (`.widget-rs485:hover .rs485-port:hover`) сохранена. Деплой app.js + main.css на 192.168.1.136.

---

## [2026-06-19 11:49] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`, `scripts/sa02m-rs485-stats.sh`
**Тип:** Некорректное поведение
**Описание:** Виджет RS-485 показывал TX 0 / RX 0 на всех портах (остался skeleton), хотя COM4 (RS-485-3/ttyS5) активно опрашивался Modbus-мостом.
**Причина:** После отключения JSON-кэша rs485 `status.cgi?part=rs485` выполнял 5×sudo driver + 5×sudo inuse (~6.4 с), а клиентский таймаут `STATUS_TIMEOUT_MS.rs485` был 4 с — fetch прерывался AbortError, `backgroundLoaded.rs485` не выставлялся, UI оставался на skeleton с нулями. API при прямом curl отдавал корректные tx/rx.
**Исправление:** Один sudo driver на запрос (`RS485_DRIVER_TEXT`), batch `inuse-batch` в helper; CGI ~4 с. Таймаут rs485 в app.js увеличен до 10 с. Деплой на 192.168.1.136; COM4: tx≈256746 rx≈1005158 (ядро ttyS5: tx≈256914 rx≈1005892).

---

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** Ethernet № 1 показывал только IP `192.168.1.136` без префикса «Статический» / «Static» или «DHCP:».
**Причина:** В `iface_mode()` одна строка `local iface=$1 conf=".../${iface}.conf"` — в bash `${iface}` в той же `local` ещё пуст, путь становился `/etc/network/interfaces.d/.conf`, функция возвращала `unknown`; `formatEthIpWidget` без префикса отдавал голый IP.
**Исправление:** Разделены объявления `local iface` и `local conf`; в JS при IP и mode≠dhcp — fallback на static. Деплой на 192.168.1.136.

## [2026-06-19 11:46] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** Виджет «Диск eMMC» всегда показывал «R 0 Б / W 0 Б», хотя eMMC активно использовался.
**Причина:** `root_disk_device()` брал устройство из `df /` (`/dev/root` → basename `root`); файла `/sys/block/root/stat` нет. Реальный корень — `/dev/mmcblk2p2` (findmnt), статистика в `/sys/block/mmcblk2/stat`.
**Исправление:** `root_disk_device()` через findmnt + readlink -f + снятие суффикса раздела (mmcblk2p2 → mmcblk2); для строки I/O — `fmtTrafficBytes` как у Ethernet RX/TX (накопленные байты с загрузки). Деплой на 192.168.1.136.

## [2026-06-19 11:40] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** TX/RX в виджетах Ethernet и RS-485 «замирали» (например RS-485-3: TX 192.9 K / RX 752.6 K), хотя ядро и Modbus-опрос продолжали наращивать счётчики.
**Причина:** `part=rs485` отдавался из файлового кэша (TTL 4 с); при таймауте `flock` (0.25 с) возвращался stale `rs485.json` без пересборки. Клиент опрашивал RS-485 раз в 8 с и пропускал цикл при `backgroundBusy.rs485`. Ethernet: `fmtBytes` округлял до 0.1 МБ — малый прирост между опросами (6 с) не был виден.
**Исправление:** RS-485 без JSON-кэша (`build_rs485_json` напрямую); при неудачной блокировке кэша — пересборка без flock; `no_cache=1` для rs485; интервал опроса RS-485 4 с + очередь повтора; `fmtTrafficBytes` (2 знака МБ) для end0/end1 RX/TX. Деплой на 192.168.1.136.

## [2026-06-19 11:31] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** Виджет «Время работы» показывал «0д 18ч 20м» при нулевых днях.
**Причина:** `applyUptimeStatus` отдавал приоритет `uptime_str` из status.cgi, где всегда включались дни (`${UPTIME_D}д ...`), хотя `fmtUptime()` уже скрывает нулевые дни.
**Исправление:** Виджет форматирует аптайм через `fmtUptime(d.uptime_sec ?? d.uptime_s)`.

## [2026-06-19 11:29] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`, `www/network_config/static/js/i18n.js`, `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`
**Тип:** Некорректное поведение
**Описание:** RS-485-3 (COM4/ttyS5) показывал «Ош FE=0 PE=0 OE=14» и жёлтый индикатор при нормальном опросе MQTT (TX/RX растут).
**Причина:** OE — накопительный счётчик UART overrun в `/proc/tty/driver/serial` с загрузки; 14 событий за ~700K RX байт, после baseline не растёт (stale). UI показывал lifetime fe/pe/oe как активные ошибки. Конфликта портов нет (только modbus_mqtt_bridge на ttyS5).
**Исправление:** status.cgi считает fe_d/pe_d/oe_d между опросами (baseline при первом sample); UI/dot — только по delta, строка ошибок без нулевых FE/PE; modbus bridge: exclusive open + flush RX/TX при открытии порта.

## [2026-06-19 11:29] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/lib_hw.sh`, `www/network_config/cgi-bin/status.cgi`, `www/network_config/cgi-bin/hw_set.cgi`
**Тип:** Некорректное поведение
**Описание:** Блок «Дискретный выход, USB-питание и индикация» показывал «н/д» для DO/Beeper/Alarm LED/USB при валидном `/etc/sa02m_hw.conf`.
**Причина:** `SA02M_STATUS_ENABLE_HARDWARE=0` отключает I2C в `gather_hardware_metrics()` — все `hw_*` принудительно `-1`; UI отображает `-1` как «н/д». Конфиг корректен, прямой `sa02m_hw_collect_metrics` на устройстве возвращает реальные 0/1.
**Исправление:** TTL-кэш `/tmp/sa02m_status_cache/hw_metrics.snapshot` (15 с, flock): при `hw_poll_disabled` — `sa02m_hw_metrics_cache_refresh()` вместо заглушек `-1`; при включённом блоке — save после collect; `hw_set.cgi` патчит кэш сразу после записи.

## [2026-06-19 11:28] branch: 1.0.3.31

**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `scripts/sa02m-rs485-stats.sh`, `scripts/03-webserver.sh`, `scripts/update-www-only.sh`
**Тип:** Некорректное поведение
**Описание:** Виджет RS-485 для COM4 (RS-485-3 / ttyS5) показывал красный индикатор «нет ответов» и TX/RX=0, хотя sa02m-modbus-mqtt опрашивал /dev/COM4.
**Причина:** www-data не может читать каталог `/proc/tty/driver/` — glob `driver/*` давал пустой `SERIAL_DRIVER_FILES`, статистика tx/rx не собиралась; `open=1` (fuser через sudo-helper) работал, поэтому UI показывал «опрос активен, нет ответов» (красная точка). Дополнительно `-r` для `/proc/tty/driver/serial` ложноположителен при недоступном каталоге.
**Исправление:** Жёстко задан `/proc/tty/driver/serial` в `SERIAL_DRIVER_FILES`; чтение через `sa02m-rs485-stats.sh` (sudo); fallback на sudo при пустом driver_text; sudoers для www-data; деплой status.cgi + helper, сброс кэша rs485.json.

## [2026-06-19 11:23] branch: 1.0.3.31

**Файл(ы):** `www/network_config/index.html`, `www/network_config/static/css/main.css`
**Тип:** Некорректное поведение
**Описание:** Кнопка «Выход» в шапке не подсвечивалась при наведении курсора, в отличие от переключателей языка и темы.
**Причина:** Кнопка использовала общие классы `.btn .btn-sm` без cyan-border hover, как у `.topbar-lang-btn`.
**Исправление:** Класс заменён на `topbar-lang-btn` с `id="logout-btn"`; добавлен `.topbar-user #logout-btn { margin-left: 4px; }`; hover наследуется от `.topbar-lang-btn:hover` (обе темы через CSS-переменные).

## [2026-06-19 11:23] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/cgi-bin/status.cgi`, `www/network_config/cgi-bin/lib_hw.sh`
**Тип:** Некорректное поведение
**Описание:** После частых F5 блок «Дискретный выход, USB-питание и индикация» показывал «Каналы не заданы — отредактируйте /etc/sa02m_hw.conf», хотя `/etc/sa02m_hw.conf` на устройстве настроен (i2c_expander, DO/Beeper/LED/USB gpiod).
**Причина:** `SA02M_STATUS_ENABLE_HARDWARE=0` в `/etc/sa02m_status_blocks.conf` — `gather_hardware_metrics()` принудительно выставлял `HW_CFG=0`; UI трактовал `!hw_configured` как отсутствие конфига. При refresh-storm первый успешный `part=main` закреплял ложную ошибку; abort fetch не сбрасывал hint при bfcache.
**Исправление:** `sa02m_hw_detect_channel_pins()` — проверка конфига без I2C; при отключённом status-block — `hw_poll_disabled:1` и корректный `hw_configured` по файлу; UI показывает «каналы не заданы» только при `hw_configured===0 && hw_poll_disabled!==1`, игнорирует неполный payload, сбрасывает hint при init, retry main после abort.

## [2026-06-19 11:20] branch: 1.0.3.31

**Файл(ы):** `www/network_config/index.html`, `www/network_config/static/css/main.css`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** На вкладке «Сведения» виджеты дашборда были чуть ниже до загрузки данных и «подпрыгивали» после ответа status.cgi (остаточный layout jitter после dd7c1a8b).
**Причина:** Пустые строки «Система» (cpu-model/armbian/kernel), пустой disk-io, заниженный min-height «Служб», RX/TX «—» вместо «0 Б», отсутствие skeleton-строк служб и слота rs485-err, появление swap-block через display:none, пустые bar-meta.
**Исправление:** Плейсхолдеры и widget-sub-reserved в HTML/JS; renderServicesSkeleton + min-height по badge; rs485ErrSlotHtml; swap-block-reserved до priority; min-height eth-ip/traffic/uptime/storage bar-meta; initDashboardPlaceholders().

## [2026-06-19 11:14] branch: 1.0.3.31

**Файл(ы):** `etc/sa02m_status_blocks.conf`, `scripts/sa02m-rs485-stats.sh`, `scripts/03-webserver.sh`, `scripts/update-www-only.sh`, `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`, `www/network_config/static/js/i18n.js`
**Тип:** Некорректное поведение
**Описание:** Виджет «Интерфейсы RS-485 — активность» не показывал трафик на COM4 при активном опросе MQTT-моста; порты отображались в обратном порядке (RS-485-0 справа); в подписи не было номера COM.
**Причина:** (1) `SA02M_STATUS_ENABLE_RS485=0`. (2) `local num=$1 dev="/dev/RS-485-${num}"` — bash не подставляет `$1` в ту же `local`. (3) `www-data` не листает `/proc/tty/driver/` (glob пустой) и не читает `serial` без sudo — TX/RX=0; `fuser` без sudo не видит MQTT. (4) Skeleton 1-based при API 0-based.
**Исправление:** `SA02M_STATUS_ENABLE_RS485=1`; явный путь `/proc/tty/driver/serial` + `sa02m-rs485-stats.sh` через sudo; раздельное `dev=`; подписи `RS-485-N (COMN+1)`; 0-based sort; dot по TX/RX.

## [2026-06-19 11:14] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** При частом обновлении страницы «Общая информация» резко росла нагрузка на CPU устройства, данные дашборда долго не появлялись.
**Причина:** Каждая перезагрузка создавала новые `setInterval` (priority/main/rs485 + bootstrap) без очистки; in-flight запросы к `status.cgi` не отменялись при уходе со страницы; параллельно летели перекрывающиеся fetch к одной части; bootstrap-таймер не сохранялся и не снимался.
**Исправление:** Единый координатор опроса (`initStatusPolling` / `teardownStatusPolling`): очистка интервалов и таймаутов на `pagehide`/`beforeunload`, AbortController с отменой предыдущего запроса части, поколение poll-gen против stale-ответов, клиентский rate-limit (`STATUS_MIN_GAP_MS`), перезапуск после BFCache (`pageshow` persisted); баннер `#dashboard-poll-alert` при серии таймаутов `part=main`.

## [2026-06-19 11:10] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/static/css/main.css`
**Тип:** Некорректное поведение
**Описание:** В блоках «Интерфейсы RS-485 — активность» при загрузке под именем порта (RS-485-N) отображался лишний символ «—», из‑за чего карточка меняла высоту после прихода данных.
**Причина:** `rs485SkeletonCardHtml()` подставлял «—» в `.rs485-dev` и в TX/RX до ответа `part=rs485`.
**Исправление:** `.rs485-dev` в skeleton — невидимый `\u00a0` с классом `rs485-dev-reserved` и `min-height`; TX/RX — «0» как в загруженном состоянии.

## [2026-06-19 11:08] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/static/js/i18n.js`, `www/network_config/static/js/mqtt.js`
**Тип:** Некорректное поведение
**Описание:** При переключении языка на English подписи вида «Процессов: X / Y» и другие динамические метки дашборда оставались на русском до следующего опроса status.cgi.
**Причина:** `refreshMainStatusI18n()` после DOM-walk заново записывал русские строки без `uiT()`; часть виджетов (load avg, kernel, GPIO, RS-485, MQTT broker) не имела refresh-колбэков на lang switch.
**Исправление:** Все смешанные метки переведены через `uiT()`; добавлены `refreshPriorityStatusI18n`, `refreshRs485I18n`, `mqttRefreshI18n`; i18n `updateControl()` вызывает их при смене языка.

## [2026-06-19 11:05] branch: 1.0.3.31

**Файл(ы):** `www/network_config/index.html`, `www/network_config/static/css/main.css`, `www/network_config/static/js/app.js`
**Тип:** Некорректное поведение
**Описание:** Виджеты дашборда «Общая информация» (Ethernet № 1/2, RS-485, Службы) были ниже при загрузке и вырастали после прихода данных.
**Причина:** Pill «Линк» скрывался через `display:none`/`hidden` (коллапс заголовка); `#rs485-grid` и `#svc-dynamic-list` пустые до ответа API; строки load avg без min-height.
**Исправление:** Pill Ethernet — `visibility:hidden` + плейсхолдер «Нет линка» и `min-height` заголовка; skeleton-карточки RS-485 по числу COM до `part=rs485`; `min-height` списка служб; зарезервированные строки proc-info/cpu-freq.

## [2026-06-19 10:49] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/mqtt.js`
**Тип:** Некорректное поведение
**Описание:** В таблице «Устройства на шине» у МР-02m 6АИ 6АО отображалось «Каналов 21/12»; у 4ДО 6ДИ — «0/10» при включённых каналах по умолчанию.
**Причина:** `countChannelsEnabled()` суммировала записи YAML, включая 9 системных (`sys`: uptime, serial, …), плюс физические каналы; при пустом `channels` физические каналы не считались, хотя UI по умолчанию считает их включёнными.
**Исправление:** Подсчёт только по профилю модуля (DO/DI/AO/AI из `MR02M_TYPES`); `sys` исключены; отсутствующая запись канала трактуется как enabled (как `getOrCreateChannel`).

## [2026-06-19 10:48] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/mqtt.js`
**Тип:** Некорректное поведение
**Описание:** После «Сохранить и применить» на модуле 6AI 6AO все AI-каналы кратко показывали тип «0 — отключён», затем после опроса появлялись сохранённые типы.
**Причина:** `saveAndApply()` вызывал `aiTypeClearPendingForDevice()` — сбрасывал ожидаемые типы; во время перезапуска моста live-кэш отдавал нули, а `mr02mAiEffectiveSensorType()` предпочитал live над YAML.
**Исправление:** Паттерн MR-02m-flasher: `aiTypeApplyPendingFromDeviceConfig()` выставляет pending для всех AI из сохранённого конфига; немедленный `refreshAiTypeSelects()`; reconcile снимает pending при совпадении с шиной; cfgType приоритетнее stale live=0.

## [2026-06-19 10:43] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/mqtt.js`
**Тип:** Некорректное поведение
**Описание:** При смене типа датчика AI на модуле 6AI 6AO в MQTT-вкладке селект сбрасывался обратно на «0 — отключено» во время выбора.
**Причина:** Live-poll (`prefetchDeviceLive` каждые 1.5 с) вызывал `refreshAiTypeSelects()`, который брал тип из `_liveSensorTypes` (Modbus на шине = 0) и перезаписывал DOM до сохранения конфига; блокировки редактирования не было.
**Исправление:** Паттерн MR-02m-flasher: `_aiTypeEditGuard` (focus/blur + 450 ms), `_aiTypePending` (ожидание подтверждения с шины), пропуск refresh для редактируемых селектов; reconcile pending после save/poll.

## [2026-06-19 10:38] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/mqtt.js`
**Тип:** Некорректное поведение
**Описание:** В MQTT-вкладке все AI-каналы МР-02m 6АИ 6АО показывали тип «2 — Pt1000», хотя после прошивки каналы отключены (тип 0).
**Причина:** `mr02mAiEffectiveSensorType()` подставлял fallback `2`, если `sensor_type` отсутствовал в YAML; конфиг устройства содержал только `enabled: true` без типов; MQTT meta на шине — `value` (код 0).
**Исправление:** Fallback изменён на `0`; типы читаются из `sensor_types` live-кэша моста; селекты обновляются после prefetch.

## [2026-06-19 10:38] branch: 1.0.3.31

**Файл(ы):** `www/network_config/static/js/mqtt.js`, `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`, `opt/sa02m-modbus-mqtt/mqtt_live_snapshot.py`
**Тип:** Некорректное поведение
**Описание:** Блок «Системные» (uptime, serial, mcu_temp и т.д.) не обновлял live-значения при раскрытии аккордеона устройства.
**Причина:** Гонка: `prefetchDeviceLive()` завершался до `ensureAccordionBody()` — `refreshLiveCellsForDevice()` выходил, т.к. `_accordionBuilt` ещё не содержал device id; DOM строился уже после prefetch без повторного refresh.
**Исправление:** Сначала построение DOM, затем prefetch; мост пишет `sensor_types` в live-кэш для UI.

## [2026-06-19 10:00] branch: 1.0.3.30

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/jobs.py`, `opt/sa02m-flasher/sa02m_flasher/runner.py`
**Тип:** Логическая ошибка / падение
**Описание:** Стандартное RS-485 сканирование падает с `TypeError: progress_cb() got an unexpected keyword argument 'address'`.
**Причина:** `runner.py` после расширения progress-событий передаёт `address`, `baudrate`, `step`, `step_total` в `ctx["progress"]`, а `progress_cb` в `jobs.py` принимал только `(value, message)`; на устройстве был развёрнут обновлённый runner без jobs.py.
**Исправление:** `progress_cb` принимает `**extra` и пробрасывает все не-None поля в SSE `data`; деплой jobs.py вместе с runner/scanner.

## [2026-06-19 09:49] branch: 1.0.3.30

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/static/css/main.css`, `www/network_config/index.html`, `www/network_config/static/js/i18n.js`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `opt/sa02m-flasher/sa02m_flasher/jobs.py`
**Тип:** Некорректное поведение
**Описание:** При стандартном сканировании (диапазон адресов × несколько скоростей) в статус-баре не было видно текущего адреса и baud — только «Опрос адреса N» без скорости или неочевидный счётчик шагов.
**Причина:** `sc_progress` в runner игнорировал конфиг линии (4-й аргумент) и не передавал baud в SSE; UI показывал только текст сообщения справа от полосы без отдельного блока «адрес + скорость».
**Исправление:** Progress-события включают `address`, `baudrate`, `step`, `step_total`; сообщение «Адрес {addr}, {baud}»; слева от полосы — detail-текст, справа — `step/total` или %; режим арбитража (`fast`) без изменений.

## [2026-06-19 09:47] branch: 1.0.3.30

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/static/css/main.css`, `www/network_config/static/js/i18n.js`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `opt/sa02m-flasher/sa02m_flasher/scanner.py`
**Тип:** Некорректное поведение
**Описание:** При быстром сканировании (арбитраж WB) в заголовке «Журнал операции» показывался прогресс-бар с неинформативным процентом вместо текущей скорости линии.
**Причина:** UI всегда отображал progress bar; демон не отправлял события progress с номером скорости на фазе арбитража.
**Исправление:** В режиме `fast` скрывается полоса прогресса, показывается текст «Поиск на {baud}»; scanner/runner шлют progress с этим сообщением при переключении скорости; добавлены i18n-ключи и CSS-класс `flasher-progress--arbitration`.

## [2026-06-18 18:05] branch: 1.0.3.29

**Файл(ы):** `www/network_config/cgi-bin/lib_rtc.sh`, `apply.cgi`, `etc/sa02m-rtc-sync.sh`, `etc/sa02m-pre-start.sh`, `scripts/01-system.sh`
**Тип:** Некорректное поведение
**Описание:** Запись системного времени в DS3231 и загрузка времени при старте не работали без `/dev/rtc1` (только чтение для веб-UI было исправлено ранее).
**Причина:** `hwclock --systohc` и pre-start ожидали char-device rtc1; на ядре без `rtc-ds3231` запись и shutdown-sync пропускались.
**Исправление:** `sync_rtc_from_system()` / запись BCD по I2C; apply.cgi и sa02m-rtc-sync используют общую библиотеку; pre-start читает DS3231 через I2C; install копирует lib в `/usr/local/lib/sa02m-lib-rtc.sh`.

## [2026-06-18 18:02] branch: 1.0.3.29

**Файл(ы):** `www/network_config/static/css/main.css`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** В блоке «Прошивка выбранных устройств» кнопка «Выбрать .fw» не помещалась рядом с «Скачать прошивки» на узких экранах.
**Причина:** `.flasher-fw-top-actions` использовал жёсткую сетку `1fr 1fr`, сжимая обе кнопки до 50% ширины колонки (~170px).
**Исправление:** `flex-wrap` + `flex: 1 1 12rem` — при ширине контейнера < ~392px «Выбрать .fw» переносится под «Скачать прошивки». Cache-bust main.css `v=1.0.3.29-8`.

## [2026-06-18 17:59] branch: 1.0.3.29

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** Таблица найденных устройств RS-485 не сортировалась по Modbus-адресу по возрастанию после завершения сканирования.
**Причина:** После скана вызывалась `sortDevicesBySerial()` — сортировка по серийному номеру, адрес только как tie-breaker; во время повторного рендера сортировка не применялась.
**Исправление:** `sortDevicesByAddress()` с числовым сравнением адреса; вызывается в `renderDevices()` когда скан не активен. Cache-bust flasher.js.

## [2026-06-18 17:47] branch: 1.0.3.29

**Файл(ы):** `www/network_config/cgi-bin/lib_rtc.sh`, `config.cgi`, `status.cgi`, `www/network_config/static/js/flasher.js`, `www/network_config/static/js/i18n.js`
**Тип:** Некорректное поведение
**Описание:** «Время с RTC» в веб-UI показывало «—» при рабочем системном времени.
**Причина:** DS3231 на i2c-1@0x68 присутствует, но модуль `rtc-ds3231` отсутствует в ядре 6.1.0-rc6 → `/dev/rtc1` не создаётся; `read_rtc_datetime()` искала только rtc1/hwclock и возвращала пустую строку. SSH: только `rtc0` (sun6i), `hwclock --rtc=/dev/rtc1` — «Cannot access»; `i2cdump -y 1 0x68` — валидное время DS3231.
**Исправление:** Общий `lib_rtc.sh`: sysfs/hwclock для внешних rtc*, затем чтение DS3231/PCF8563 через `sudo -n i2cget`. Подписи прошивок в flasher.js; i18n kB/описания. Задеплоено на 192.168.1.136 — `rtc_datetime` в config/status CGI заполнен.

## [2026-06-18 17:10] branch: 1.0.3.29

**Файл(ы):** `www/network_config/static/js/i18n.js`
**Тип:** Некорректное поведение
**Описание:** При нажатии кнопки смены языка веб-интерфейс зависал.
**Причина:** Полный обход DOM при `setLang()` генерировал тысячи мутаций; `MutationObserver` перезаписывал оригиналы текстов/атрибутов и повторно обрабатывал изменения в цикле.
**Исправление:** Observer отключается на время `apply()`; оригиналы не перезаписываются в callback; атрибуты/текст обновляются только при реальном изменении; переключатель языка блокируется на один кадр через `requestAnimationFrame`.

## [2026-06-18 14:52] branch: 1.0.3.28 - Dashboard: HW над RS-485, hover, без «Состояние:»

**Файл(ы):** www/network_config/index.html, www/network_config/static/css/main.css, www/network_config/static/js/app.js
**Тип:** Некорректное поведение
**Описание:** Виджеты HW и RS-485 занимали всю ширину в разном порядке; подписи «Состояние:» дублировали индикаторы; виджеты дашборда не выделялись при наведении.
**Причина:** Разметка dash-full и фиксированный порядок блоков; отсутствие отдельного стиля hover для #tab-dashboard .widget.
**Исправление:** HW блок выше RS-485; класс dash-span-top4 (4 колонки); удалены hw-status-label; синяя рамка/тень при :hover. Cache-bust CSS/JS.

## [2026-06-18 14:52] branch: 1.0.3.28 - RS-485 Dashboard: 4 цвета точки, без «○ свободен»

**Файл(ы):** www/network_config/static/js/app.js, www/network_config/static/css/main.css
**Тип:** Некорректное поведение
**Описание:** Текст «○ свободен» / «● активен» загромождал карточки; не различались «опрос без ответов» и «ошибки линии».
**Причина:** Бинарная логика dot + статусная строка вместо семантики poll/errors/responses.
**Исправление:** Классы idle / on / warn / 
oresponse (+ 
opoll при absent); подсказки в 	itle; строки open/closed убраны.

## [2026-06-18 14:52] branch: 1.0.3.28 - Gateway UI: COM панели, nav toggle, таблица портов

**Файл(ы):** www/network_config/static/js/gateway.js, www/network_config/static/css/main.css, www/network_config/index.html
**Тип:** Некорректное поведение
**Описание:** Неравная ширина панелей COM, лишние hints режимов, нельзя свернуть подменю шлюза повторным кликом; таблица портов без единого стиля/ширины.
**Причина:** Inline-стили и ield-hint под каждым режимом; подменю только открывалось; таблица без gw-ports-table.
**Исправление:** gw-device-stack, удалены gw-mode-hint, gatewayNavClick для collapse; стили gw-ports-table, zebra; убран COM5-текст для 4-портового профиля.

## [2026-06-18 14:19] branch: 1.0.3.28 — hourly spam `network_or_git_failed` в event log

**Файл(ы):** `etc/sa02m-web-update-check.sh`, `www/network_config/cgi-bin/web_update_check.cgi`, `etc/systemd/sa02m-web-update-check.service`
**Тип:** Некорректное поведение
**Описание:** Журнал событий (`/var/log/sa02m_install.log`) каждый час получал строку `sa02m-web-update-check: network_or_git_failed`, даже без действий пользователя.
**Причина:** `sa02m-web-update-check.timer` (OnCalendar=hourly) запускал тот же скрипт, что и кнопка «Проверить обновления»; при недоступности GitHub/git скрипт всегда писал ошибку в install.log.
**Исправление:** Флаг `--manual` / `SA02M_WEB_UPDATE_CHECK_MANUAL=1` — запись в install.log только при ручной проверке (CGI `?force=1` передаёт `--manual`). Автоматический timer по-прежнему обновляет `check.json`, но молчит при offline. В unit добавлены `StandardOutput=null` / `StandardError=null`. Задеплоено на 192.168.1.136.

## [2026-06-18 14:12] branch: 1.0.3.28 — multi-select пакетная прошивка в веб-UI

**Файл(ы):** `www/network_config/static/js/flasher.js`, `www/network_config/index.html`, `opt/sa02m-flasher/sa02m_flasher/module_profiles.py`, `opt/sa02m-flasher/sa02m_flasher/service.py`, `opt/sa02m-flasher/tests/test_flash_route.py`
**Тип:** Некорректное поведение
**Описание:** В веб-прошивальщике можно было выбрать только одно устройство; повторный клик заменял выбор, пакетная прошивка нескольких MR/MP или WB по очереди была недоступна.
**Причина:** Single-select через `__selected` на строке таблицы; `startFlash` отправлял один target в `/flash_batch`.
**Исправление:** Multi-select (`selectedDeviceIndices` Set), клик переключает выделение; «Прошить (N)» шлёт все targets последовательно через существующий `run_flash_batch_job` ([1/N] прогресс). `validateMultiFlashSelection()` и backend `validate_batch_flash_targets()` блокируют смешение MR/MP и WB. Задеплоено на 192.168.1.136.

## [2026-06-18 13:55] branch: 1.0.3.28 — «Подготовка порта» ~3.7 с при свободном COM

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/mplc_lease.py`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `www/network_config/static/js/flasher.js`, `opt/sa02m-flasher/tests/test_mplc_lease.py`
**Тип:** Некорректное поведение
**Описание:** Перед каждым сканированием UI показывал «Подготовка порта» ~3–4 с, даже когда fuser на `/dev/COM4` пуст и опросчики порт не держали.
**Причина:** `port_lease()` всегда вызывал `systemctl stop` для `mplc4` и `sa02m-modbus-mqtt` по глобальному `is-active`, независимо от фактической занятости порта (~3.7 с: stop/start mqtt + pkill mplc).
**Исправление:** Быстрый путь: `is_port_poll_free()` (один fuser) → пропуск stop/restart; при занятом порте — прежняя логика. UI: «Сканирование» вместо «Подготовка порта» по кешу `/ports`. Замер после фикса: prep→scan ~0.34 с (было ~3.67 с). Задеплоено на 192.168.1.136.

## [2026-06-18 13:48] branch: 1.0.3.28 — веб показывал устаревшую версию 1.0.3.22

**Файл(ы):** `www/network_config/static/js/app.js`, `www/network_config/index.html`, `www/network_config/VERSION`, `scripts/sync-app-version.py`
**Тип:** Некорректное поведение
**Описание:** В шапке «Сервер автоматизации» отображалась v1.0.3.22 при ветке 1.0.3.28.
**Причина:** `APP_VERSION` и cache-bust `app.js?v=` в index.html не обновлялись при bump ветки.
**Исправление:** Версия вынесена в `www/network_config/VERSION`; добавлен `scripts/sync-app-version.py` (читает git-ветку, обновляет VERSION, app.js, index.html). Синхронизировано на 1.0.3.28, задеплоено на 192.168.1.136.

## [2026-06-18 13:40] branch: 1.0.3.28 — авто-маршрут прошивки по сигнатуре

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/module_profiles.py`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `www/network_config/index.html`, `www/network_config/static/js/flasher.js`, `opt/sa02m-flasher/tests/test_flash_route.py`
**Тип:** Некорректное поведение
**Описание:** Для прошивки сторонних модулей (Wiren Board) требовалась отладочная галочка «Разрешить устройство вне списка сигнатур MR/MP-02m»; без неё MR .fw на WB-устройство блокировался, а правильный WB-путь не выбирался автоматически.
**Причина:** Whitelist MR/MP с обходом через `force_unlisted_signature`; `is_wb_firmware` определялся только по расширению файла без проверки согласованности с сигнатурой устройства.
**Исправление:** Удалена галочка из UI/API. Добавлены `device_flash_route()` и `validate_firmware_device_route()`: MR/MP → .fw / 115200 N1 / fast Modbus; не наши (WB) → .wbfw / 19200 N2 / WB algorithm. Явные ошибки при несовпадении типа прошивки и сигнатуры. Задеплоено на 192.168.1.136.

## [2026-06-18 13:28] branch: 1.0.3.28 — SSE обрыв при прошивке: перезапуск демона

**Файл(ы):** `www/network_config/static/js/flasher.js`
**Тип:** Некорректное поведение
**Описание:** UI показывал «Потеряно SSE-соединение» ~40 с после старта прошивки MR-02m_1.0.9.0.fw; итог прошивки в UI неизвестен.
**Причина:** `systemctl restart sa02m-flasher` в 11:50:30 MSK (~43 с после старта flash_batch) оборвал SSE и уничтожил in-memory job (404 на reconnect). Прошивка на устройстве остановилась на блоке 197/628. nginx `proxy_read_timeout 3600s` и heartbeat в service.py не при чём.
**Исправление:** В `openStream` при `onerror` — опрос `GET /jobs/<id>` каждые 2 с; при 404 — явное сообщение о перезапуске демона и совет сканировать; обновлён toast при ошибке прошивки. Задеплоено на 192.168.1.136.

## [2026-06-18 13:21] branch: 1.0.3.28 — Прошивка падала на скачивании .fw: DNS/UX

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/firmware_repo.py`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `www/network_config/static/js/flasher.js`, `www/network_config/index.html`, `scripts/01-system.sh`
**Тип:** Некорректное поведение
**Описание:** UI показывал «есть 1.0.9.0» после обновления манифеста, но прошивка падала до Modbus с `Temporary failure in name resolution` при попытке скачать `MR-02m_1.0.9.0.fw`. Пользователь воспринимал это как «интернет есть».
**Причина:** (1) Манифest index.json (~4 KB) мог обновиться ранее, а файл .fw (~150 KB) не был в кешe; подсказка «есть X» бралась из `latestStableVersion` манifesta, не из `downloaded`. (2) При ICS (WiFi→Ethernet с ПК) шлюз 192.168.1.5 доступен, но внешние DNS (8.8.8.8) с шлюза не проходят — `getent/curl` к cyntron.ru падают; на устройстве не был прописан DNS через IP шлюза.
**Исправление:** `_format_network_error` + `ensure_local_path` — понятные RU-сообщения; кнопка «Скачать прошивки» (`refresh download=true` + keep_current); блокировка «Прошить» для «не скачан»; подсказка «есть X (не скачан)»; `01-system.sh` — nameserver шлюза в resolvconf head. На устройстве: resolvconf head `nameserver 192.168.1.5` (если ICS не отдаёт DNS — загрузка .fw вручную или MR-02m_1.0.8.26.fw из кеша).

## [2026-06-18 13:24] branch: 1.0.3.28 — signature-based UART profiles для reg 129

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/module_profiles.py`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `www/network_config/static/js/flasher.js`, `opt/sa02m-flasher/tests/test_module_line_profiles.py`, `opt/sa02m-flasher/tests/test_runner_app_line.py`
**Тип:** Логическая ошибка
**Описание:** Вход в bootloader (reg 129) использовал baud/stop из скана (19200 N2 на шлюзе) вместо профиля по сигнатуре модуля; MR-02m требует 115200 N1.
**Причина:** Жёсткий fallback 19200/2 и/или слепое доверие scan baud без классификации MP/MR vs WB vs CE/DTV.
**Исправление:** `Rs485LineProfile` + `application_line_profile()` по сигнатуре (MR/MP/CE/DTV → 115200 N1; WB/.wbfw → scan или 19200 N2). Runner и flasher.js передают `app_line_*` в flash_batch; WB .wbfw path без изменений.

## [2026-06-18 13:15] branch: 1.0.3.28

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/firmware_repo.py`, `opt/sa02m-flasher/sa02m_flasher/service.py`, `www/network_config/static/js/flasher.js`
**Тип:** Некорректное поведение
**Описание:** Кнопка «Проверить» только обновляла манифест без скачивания; `_consolidate_repository` оставлял лишь максимальную версию на (channel, kind), удаляя текущие образы модулей с линии.
**Причина:** `refresh(download=False)` на кнопке; политика кеша не учитывала версии, реально работающие на RS-485.
**Исправление:** «Проверить» вызывает `refresh(download=true)` с `keep_current` (max app/bl с результатов сканирования); скачиваются current+latest stable app/bl; из кеша удаляются остальные .fw/.bin/.elf.

## [2026-06-18 13:16] branch: 1.0.3.28 — MR-02m: reg 129 всё ещё на 19200 N2 при прошивке .fw

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/runner.py`, `www/network_config/static/js/flasher.js`
**Тип:** Логическая ошибка
**Описание:** При прошивке MR-02m_1.0.9.0.fw лог показывал «Перевод адр.4 в bootloader (app baud 19200 N2)» — reg 129 не доходила до приложения, fast Modbus по серийному 0xFD 0x46 таймаутил.
**Причина:** Частичный fix 1.0.3.27 менял только fallback при отсутствии baud в target, но (1) старый runner на устройстве всё ещё имел hardcode 19200/2; (2) даже с fix — если в target попадал baudrate/stopbits из скана (19200 N2), они переопределяли MR-default; UI не передавал line-параметры, а на старом коде это давало 19200 N2.
**Исправление:** `_application_line_params`: для MR .fw всегда 115200 N1 (игнор scan baud); для WB — scan или 19200 N2. UI передаёт baudrate/parity/stopbits из скана (для WB). `_firmware_signature_for_log` — sig в логе из manifest/скана, если в .fw NONE.

## [2026-06-08 21:03] branch: 1.0.3.27 — fix-eth.sh не восстанавливал default route для static-интерфейса после cold-boot

**Файл(ы):** `etc/fix-eth.sh`
**Тип:** Логическая ошибка
**Описание:** При cold-boot PHY-линк end0 поднялся через ~5 мин после запуска `ifup@end0.service`. К моменту прихода carrier IP уже был назначен (ifup выполнился), но gateway route не добавился, т.к. `ip route add default` в `ifup` провалился при `linkdown`. `fix-eth.sh` видел `has_ip=true` и переходил к проверке connectivity, но восстановление default route было реализовано только для DHCP-интерфейсов. В итоге устройство работало без default gateway до ручного вмешательства или перезапуска.
**Причина:** Блок восстановления default route содержал условие `if [ "$iface_type" = "dhcp" ]`, static-интерфейс с `gateway` в конфиге игнорировался.
**Исправление:** Условие переработано: для любого типа интерфейса при отсутствии `ip route default dev $iface` — восстанавливать маршрут: для dhcp из lease-файла, для static из `gateway` в `interfaces.d/*.conf`.

## [2026-06-08 21:21] branch: 1.0.3.27 — Неверный baud rate при входе в bootloader MR-02m

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/runner.py`
**Тип:** Логическая ошибка
**Описание:** `_enter_bootloader_from_application_line` использовала baud rate 19200 baud, 2 стоп-бита по умолчанию (значения для Wiren Board), тогда как MR-02m работает на 115200 N1. Команда reg 129 не доходила до устройства, оно оставалось в режиме приложения.
**Причина:** `baud = int(device.get("baudrate") or 0) or 19200` и `stopbits = int(device.get("stopbits") or 2) or 2` — жёсткий fallback 19200/2 без учёта типа прошивки.
**Исправление:** Добавлены `_default_baud`/`_default_stop` зависящие от `is_wb_firmware`: для MR-firmware = 115200 N1, для WB = 19200 N2.

## [2026-06-08 21:21] branch: 1.0.3.27 — Неверное кодирование блоков данных при прошивке bootloader из .fw

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/flash_protocol.py`
**Тип:** Логическая ошибка
**Описание:** При прошивке бутлоадера из `.fw`-файла данные кодировались в big-endian (`payload_block_to_registers`), тогда как `.fw` содержит байт-свопированный payload. В staging Flash записывались байт-свопированные данные → вектор SP не соответствовал диапазону → команда `commit` (0x1006) возвращала исключение 4 (Server Device Failure). CRC совпадал (т.к. вычислялся над byte-swapped данными), но Flash содержимое было некорректным.
**Причина:** Отсутствовал учёт того, что `.fw` payload — это `raw_binary` после byte-swap каждой 16-битной пары. Нужно применить обратный swap при кодировании в Modbus-регистры, чтобы в staging попали оригинальные LE-байты.
**Исправление:** Добавлена функция `payload_bytes_to_registers_le`; `send_data_block_bootloader` и `send_data_block_bootloader_by_serial` получили параметр `app_from_fw=True`. При `.fw` формате: header (первые 32 байта) передаётся как есть через `send_info_block_wb` (содержит CRC32 над raw binary), данные кодируются через `payload_bytes_to_registers_le` → `data_bl` = raw binary → `running_crc` = CRC32(raw binary) = expected_crc ✓ → Flash = raw binary ✓ → commit проходит ✓.

## [2026-06-08 19:46] branch: 1.0.3.27 — Прошивка модулей расширения по адресу приложения вместо 247

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/runner.py`
**Тип:** Логическая ошибка
**Описание:** После входа в bootloader (reg 129) сервис пытался зондировать и прошивать модуль по адресу приложения (напр. 8), тогда как bootloader всегда отвечает по адресу 247 (BOOTLOADER_DEFAULT_ADDR). Прошивка не начиналась: все Modbus-запросы на адрес 8 уходили в таймаут.
**Причина:** `addr_probe` и `boot_addr_for_address_path` инициализировались из `device.get("address")` (адрес приложения), а не из `fp.BOOTLOADER_DEFAULT_ADDR`.
**Исправление:** `addr_probe = fp.BOOTLOADER_DEFAULT_ADDR`; `boot_addr_for_address_path = fp.BOOTLOADER_DEFAULT_ADDR` в `_run_bootloader_flash_session` и `_flash_one_device`.

## [2026-06-08 19:46] branch: 1.0.3.27 — Таймаут при ожидании готовности bootloader после info-блока

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/flash_protocol.py`
**Тип:** Логическая ошибка
**Описание:** После отправки info-блока bootloader начинает блокирующее стирание Flash (~2 с), в течение которого не отвечает на Modbus. Функция `_wait_bootloader_ready_for_data_impl` считала «не поддерживается» только Modbus exception 02, а таймаут не распознавала, поэтому fallback на фиксированную паузу 2.8 с не срабатывал — 15 с поллинга и ошибка.
**Причина:** `state_unsupported = _modbus_inner_exception_code(err_state) == 2` — условие не учитывало таймаут ответа.
**Исправление:** Добавлена функция `_fp_is_timeout_err`; условие расширено: `state_unsupported = (exc_code == 2) or _fp_is_timeout_err(err_state)`.

## [2026-06-05 16:13] branch: 1.0.3.25 — LED с задержкой 30 с: path unit не работает на sysfs

**Файл(ы):** `etc/sa02m-eth0-led-poll.sh`, `etc/systemd/sa02m-eth0-led-poll.service`, `scripts/02-network.sh`
**Тип:** Логическая ошибка
**Описание:** LED гас и загорался с задержкой ~30 с.
**Причина:** `systemd path unit` (PathChanged) не работает на sysfs — inotify не получает события от `/sys/class/net/end0/carrier`. Реакция шла только через `net-watchdog` (интервал 30 с).
**Исправление:** Новый `sa02m-eth0-led-poll.service` — цикл `sleep 1` + поллинг carrier. LED реагирует в течение 1–2 с. Path unit отключён.

---

## [2026-06-05 16:10] branch: 1.0.3.25 — LED eth0_link не гаснет: fix-eth не вызывал скрипт при carrier=0

**Файл(ы):** `etc/sa02m-eth0-led.sh`, `etc/fix-eth.sh`, `etc/net-watchdog.sh`, `etc/systemd/sa02m-eth0-led.path`, `etc/systemd/sa02m-eth0-led.service`, `scripts/02-network.sh`
**Тип:** Логическая ошибка
**Описание:** После правки udev LED всё равно не гас при отключении кабеля. В логе fix-eth carrier=0 фиксировался, но LED оставался включённым.
**Причина:** `fix-eth.sh` при `carrier=0` выходил без вызова `sa02m-eth0-led.sh`. udev `change` на link-down ненадёжен. Скрипт LED проверял только carrier без operstate.
**Исправление:** Вызов LED-скрипта в ветке no-carrier fix-eth и в net-watchdog; systemd path unit на carrier/operstate end0 для мгновенной реакции; установка скрипта и path unit через 02-network.sh.

---

## [2026-06-05 15:57] branch: 1.0.3.25 — LED eth0_link не гаснет при отключении кабеля

**Файл(ы):** `etc/99-lan-recovery.rules`
**Тип:** Логическая ошибка
**Описание:** При вытаскивании сетевого кабеля LED eth0_link оставался включённым.
**Причина:** udev-правило `ATTR{carrier}=="0"` никогда не совпадает: при link-down ядро возвращает `EINVAL` при чтении `/sys/class/net/end0/carrier` вместо строки `"0"`, поэтому скрипт `sa02m-eth0-led.sh` не вызывался при отключении кабеля.
**Исправление:** Убраны оба условия `ATTR{carrier}=="1/0"` из LED-правила. Теперь скрипт вызывается на любое `change`-событие end0 и сам читает carrier с fallback 0 при ошибке.

---

## [2026-06-03 16:53] branch: 1.0.3.24 — LED eth0_link не горит при link-up на end0

**Файл(ы):** `etc/sa02m-eth0-led.sh`, `etc/sa02m-pre-start.sh`, `etc/99-lan-recovery.rules`
**Тип:** Логическая ошибка
**Описание:** При подключённом кабеле end0 (carrier=1) индикатор линка eth0 не загорается. trigger оставался `[none]`, brightness=0.
**Причина:** `eth0_link` — platform gpio-led без поддержки netdev trigger (в sysfs нет `device_name`/`link`). Скрипты писали в несуществующие атрибуты (Permission denied / No such file), яркость не устанавливалась.
**Исправление:** `sa02m-eth0-led.sh` и `eth0_led_sync_carrier()` в pre-start: `trigger=none`, brightness=1/0 по `carrier` end0. udev: правило и при carrier=0. Задеплоено на устройство.

---

## [2026-06-03 14:05] branch: 1.0.3.24 — деплой sa02m-end1-coldboot.service на SA-02m

**Файл(ы):** `etc/systemd/sa02m-end1-coldboot.service`, `usr/local/sbin/sa02m-end1-coldboot.sh`
**Тип:** Диагностика + деплой сервиса
**Описание:** CONFIG_ICPLUS_PHY уже встроен в ядро как builtin, PHY привязан к драйверу ICPlus IP101G штатно. Проблема была только в отсутствии задеплоенного сервиса sa02m-end1-coldboot. Сервис создан в репо, задеплоен на устройство, включён через systemctl enable. После warm reboot сервис отработал корректно (T+30с, ~40с выполнение), однако carrier остался 0 — ожидаемо, так как проблема специфична для cold-boot (power cycle). Требуется проверка power cycle.
**Причина:** Сервис sa02m-end1-coldboot.service не был задеплоен на устройство.
**Исправление:** Созданы `etc/systemd/sa02m-end1-coldboot.service` и `usr/local/sbin/sa02m-end1-coldboot.sh` в репо; задеплоены на устройство через pscp; активированы (`systemctl enable`). Для финальной проверки требуется полный power cycle (выключить питание, подождать 10с, включить).

---

## [2026-06-03 13:57] branch: 1.0.3.24 — icplus.ko сборка и анализ PHY на SA-02m

**Файл(ы):** `/lib/modules/6.1.0-rc6/kernel/drivers/net/phy/icplus.ko`
**Тип:** Диагностика + деплой модуля
**Описание:** Собран `icplus.ko` для ядра `6.1.0-rc6` из исходника `linux-6.1-rc6-sk.tar.bz2` с тулчейном ARM GNU Toolchain 10.3-2021.07 (VirtualBox VM `A40-i`). vermagic: `6.1.0-rc6 SMP mod_unload ARMv7 p2v8` соответствует устройству. Модуль задеплоен на устройство. `modprobe icplus` завершился ошибкой: `Error: Driver 'ICPlus IP175C' is already registered` — драйверы ICPlus уже скомпилированы в ядро (=y, подтверждено через `/proc/kallsyms`: `ip101a_read_page`, `ip101g_read_page`, `ip101a_g_config_intr_pin` и др.). PHY (ID 0x02430c54 = IP101A по SK-дефинициям) привязан к драйверу `ICPlus IP101G`. Интерфейс `end1` показывает `NO-CARRIER` — холодный старт autoneg не проходит; сервис `sa02m-end1-coldboot` не установлен.
**Причина:** CONFIG_ICPLUS_PHY=y в ядре запрещает загрузку одноимённого модуля. Холодный старт PHY требует `ethtool -r end1` через ~30с после загрузки.
**Исправление:** icplus.ko задеплоен (готов при пересборке ядра с =m). Для холодного старта — применить `fix-end1-internet.sh` (устанавливает `sa02m-end1-coldboot.service`).

---

## [2026-06-03 13:34] branch: 1.0.3.24 — fix: CONFIG_ICPLUS_PHY=y, kernel rebuilt and deployed

**Файл(ы):** `arch/arm/boot/zImage` (linux-6.1-rc6-sk, WSL cross-build)
**Тип:** Исправление конфигурации ядра
**Описание:** Ядро 6.1.0-rc6 пересобрано с `CONFIG_ICPLUS_PHY=y`. После деплоя на устройство dmesg подтвердил: `dwmac-sun8i end1: PHY [stmmac-1:00] driver [ICPlus IP101G] (irq=POLL)` — IP101G теперь инициализируется штатным драйвером при холодной загрузке.
**Причина:** `CONFIG_ICPLUS_PHY` (Kconfig-символ для IC Plus PHY family: IP101A/G, IP175C/D, IP1001) не был включён в `sunxi_sk_defconfig`.
**Исправление:** WSL: `arm-linux-gnueabihf-gcc` + исходник `linux-6.1-rc6-sk.tar.bz2` из buildroot → `make sunxi_sk_defconfig` → `./scripts/config --enable ICPLUS_PHY` → `make olddefconfig` → `make -j20 zImage`. Деплой: `pscp` → `/tmp/zImage-new` → `cp /tmp/zImage-new /mnt/zImage` (mmcblk2p1 FAT). Перезагрузка подтвердила работу.

---

## [2026-06-03 12:45] branch: 1.0.3.24 — root cause: IP101A PHY driver missing from kernel

**Файл(ы):** `tools/imaging/out/patch_dtb_all.py`, `tools/imaging/out/sun8i-a40i-nano2e-none-sk.dts`, `tools/imaging/out/sun8i-a40i-sk-fixed.dtb`
**Тип:** Логическая ошибка / конфигурация ядра
**Описание:** end1 (GMAC, IP101A PHY) не поднимает линк при холодной загрузке. Диагностика показала: ядро (6.1.0-rc6) собрано без `CONFIG_IP101_PHY` — в системе доступны только Generic PHY и Generic Clause 45 PHY. PHY идентифицирован: IC+ IP101A rev 4, ID 0x02430c54 (подтверждено `mii-tool -v`). ANLPAR=0x0000 — физического партнёра на линии нет (порт коммутатора не активен или кабель не подключён на другом конце); это также объясняет отказ всех программных обходных решений.
**Причина:** 1) `CONFIG_IP101_PHY` не скомпилирован в ядро → Generic PHY вместо icplus-драйвера с IP101A-специфической инициализацией. 2) Отсутствует физический партнёр на порту end1 в момент тестирования.
**Исправление:** DTB: добавлен `compatible = "icplus,ip101a", "ethernet-phy-ieee802.3-c22"` в узел `ethernet-phy@0` GMAC-mdio (будет активировать icplus-драйвер как только `CONFIG_IP101_PHY=y` появится в ядре; сейчас — fallback на Generic PHY без изменений). `reset-deassert-us = 500ms` зафиксирован в `patch_dtb_all.py` для воспроизводимости. Сервис `sa02m-end1-coldboot` полностью удалён с устройства и из репозитория (не нужен при правильном драйвере). **Следующий шаг:** пересборка ядра с `CONFIG_IP101_PHY=y` для полного устранения root cause.

---

## [2026-06-03 12:45] branch: 1.0.3.24

**Файл(ы):** `etc/fix-end1-internet.sh`
**Тип:** Новая функциональность — сервисный скрипт развёртывания
**Описание:** Добавлен самодостаточный скрипт `fix-end1-internet.sh` для запуска на устройстве. Применяет все накопленные фиксы для end1 (GMAC): патч DTB (GMAC okay + dc1sw always-on + syscon), threadirqs в boot.scr, end1.conf (allow-hotplug + DHCP + metric 100 + post-up route), end0.conf metric 200, dhclient RFC3442 exit hook, актуальный fix-eth.sh, sa02m-end1-coldboot, удаление sa02m-phy-coldboot, udev i2c-2 unbind. Идемпотентен, сообщает что изменено, определяет необходимость перезагрузки.
**Причина:** Отсутствовал единый инструмент применения всех фиксов на работающем устройстве без полной переустановки.
**Исправление:** `etc/fix-end1-internet.sh` — самодостаточный bash-скрипт с embedded heredocs всех файлов; запускать: `bash fix-end1-internet.sh` от root на устройстве.

---

## [2026-06-03 12:10] branch: 1.0.3.24

**Файл(ы):** `etc/fix-eth.sh`, `etc/sa02m-phy-coldboot.sh` (удалён), `etc/systemd/sa02m-phy-coldboot.service` (удалён), `etc/sa02m-end1-coldboot.sh` (новый), `etc/systemd/sa02m-end1-coldboot.service` (новый), `scripts/02-network.sh`
**Тип:** Регрессия — предыдущее исправление (`sa02m-phy-coldboot` + unbind/rebind в `fix-eth.sh`) уничтожало работающий линк
**Описание:** На рабочей плате после второго power-cycle end1 LED зажёгся, pings работали — но потом LED погас. Причина: `sa02m-phy-coldboot.service` запускал unbind/rebind каждые ~34с безусловно (не проверял carrier внутри цикла после unbind); параллельно `fix-eth.sh` с MAX_LINK_CYCLES=20 делал свои unbind/rebind-циклы. Оба механизма разрушали уже работающий линк.
**Причина:** Unbind/rebind через sysfs не является надёжным способом аппаратного сброса IP101A на данной платформе. После 6+ неудачных unbind/rebind PHY переходил в необратимое состояние (без power-cycle не восстанавливался). `sa02m-phy-coldboot.service` не имел guard на carrier=1 между итерациями — только в начале цикла; за время unbind+rebind+sleep(30) линк мог появиться и сразу быть уничтожен следующей итерацией.
**Исправление:** (1) `sa02m-phy-coldboot.service` и `.sh` удалены полностью. (2) `fix-eth.sh` reverted: unbind/rebind убран, восстановлен soft link cycle (`ip link down/up + ethtool -r`), MAX_LINK_CYCLES возвращён к 5. (3) Добавлен `sa02m-end1-coldboot.service` (oneshot) — запускается 1 раз через 30с после boot, проверяет carrier, при отсутствии делает ОДИН вызов `ethtool -r`, ждёт 15с, логирует результат. Никаких циклов, никакого unbind/rebind. (4) `patch_dtb_all.py`: убрано изменение reset-deassert-us (возврат к 200ms в образах).

---

## [2026-06-03 11:52] branch: 1.0.3.24

**Файл(ы):** `etc/fix-eth.sh`, `etc/sa02m-phy-coldboot.sh`, `etc/systemd/sa02m-phy-coldboot.service`, `tools/imaging/out/patch_dtb_all.py`, DTB `sun8i-a40i-sk.dtb`
**Тип:** Некорректное поведение — end1 не линкуется при первом холодном старте на рабочей плате
**Описание:** На рабочей плате (с PCA9536) end1 поднимался только со второго power-cycle reboot. На тестовой плате также нестабильно. Предыдущий fix (MAX_LINK_CYCLES=5, reset-deassert-us=200ms) оказался недостаточным: link cycle через `ip link set down/up` НЕ вызывает аппаратный GPIO-сброс PHY (только `phy_probe()` при bind/unbind драйвера). После 5 циклов (~3 мин) watchdog прекращал попытки, тогда как switch мог ещё не завершить загрузку.
**Причина:** (1) `ip link set down/up` перезапускает `phylink` state machine, но не вызывает `phy_device_reset()` — PHY остаётся в изначально «неудавшемся» состоянии cold-boot. (2) MAX_LINK_CYCLES=5 → watchdog сдавался раньше, чем switch-партнёр завершал boot (~3–5 мин). (3) reset-deassert-us=200ms — граница для стабилизации осциллятора IP101A на данной плате.
**Исправление:** (1) `fix-eth.sh`: link cycle переведён на PHY driver unbind/rebind (`/sys/bus/mdio_bus/drivers/Generic PHY/unbind|bind`) — вызывает `phy_probe()` → `phy_device_reset()` → GPIO-сброс с правильными таймингами. MAX_LINK_CYCLES увеличен 5→20 (≈10 мин). Fallback на soft-cycle если unbind недоступен. (2) Новый `sa02m-phy-coldboot.service` (Type=oneshot, Before=net-watchdog.service): ранний аппаратный reset PHY при cold-boot с retry 12×30с=6 мин — запускается до net-watchdog, при warm-boot сразу выходит. (3) DTB: `reset-deassert-us` 200ms→500ms для обоих PHY (GMAC и EMAC). `patch_dtb_all.py` обновлён для применения в будущих образах.
**Проверка:** На тестовой плате end1 получил IP 192.168.1.114 с первого boot, Link is Up через 15.8 с. `link_cycle_count` не создан — link cycles не понадобились. `sa02m-phy-coldboot` нашёл carrier уже поднятым и вышел. Производственная плата (с I2C/RTC) ожидает тест.

---

## [2026-06-03 10:15] branch: 1.0.3.24

**Файл(ы):** `etc/fix-eth.sh`, DTB `sun8i-a40i-sk.dtb`
**Тип:** Некорректное поведение — end1 линк не поднимается с первого холодного старта
**Описание:** После фикса `dc1sw regulator-always-on` (DTB) accidental power-cycle PHY при boot был устранён. Вместе с ним устранился и side-effect: раньше при cold boot PHY терял питание (~30 с), потом его восстанавливал `sa02m-end1-link.service` (~74 с) и PHY автоматически сбрасывался. После фикса при cold boot IP101A PHY инициализируется только один раз (~11 с) и если за это время autoneg не завершился — линк не поднимается вообще. Второй reboot помогал (PHY сохранял состояние через power-on VCCIO).
**Причина:** (1) `fix-eth.sh` делал link cycle однократно (маркер-файл `link_cycled`), после чего повторные попытки блокировались — при cold boot IP101A может требовать нескольких renegotiate-циклов. (2) В DTB `reset-deassert-us` = 100 мс — для IP101A при cold start осциллятор может стабилизироваться > 100 мс.
**Исправление:** (1) `fix-eth.sh`: маркер `link_cycled` (boolean) заменён на `link_cycle_count` (счётчик), добавлен `MAX_LINK_CYCLES=5` — link cycle будет повторяться до 5 раз (≈2.5 мин) пока не появится carrier. `mii-tool -r` добавлен как основной способ рестарта autoneg через MDIO. Задержки увеличены: 0.5 с → 1 с между down/up, 2 с → 3 с ожидание. (2) DTB: `reset-deassert-us` увеличен 100 мс → 200 мс (осцилляторный запас IP101A при cold start). **Проверка:** после применения DTB-патча на тестовой плате — end1 получил IP 192.168.1.114 с первого cold boot без link cycle (link_cycle_count пустой). vcc-gmac-phy:disabling отсутствует. Проблема решена.

---

## [2026-06-03 09:40] branch: main

**Файл(ы):** `tools/imaging/out/patch_dc1sw_v2.py`, `etc/udev/rules.d/50-sa02m-i2c2-unbind.rules`
**Тип:** Некорректное поведение / неполный патч DTB + CRLF в logrotate конфиге
**Описание:** При ревью выявлено: (1) `dc1sw` (vcc-gmac-phy) в задеплоенном DTB НЕ имел `regulator-always-on` — предыдущий патч не был применён корректно. `dmesg` показывал `vcc-gmac-phy: disabling` на ~30 с после загрузки → `sa02m-end1-link.service` вынужденно перезапускал PHY на ~74 с. (2) `/etc/logrotate.d/sa02m-flasher` имел CRLF-окончания → `logrotate` падал на каждом boot. (3) Platform udev-правило для раннего unbind `1c2b800.i2c` было только в репо, не задеплоено на устройство.
**Причина:** (1) `patch_dc1sw_v2.py` содержал ошибку Python (capture_output+stderr conflict). Предыдущий патч-скрипт нашёл другой узел `regulator-always-on` в DTS но не `dc1sw`. (2) Файл logrotate был создан на Windows (CRLF). (3) Deployment gap.
**Исправление:** (1) Исправлен `patch_dc1sw_v2.py`, применён на устройстве — `dc1sw { regulator-always-on; }` подтверждён через `dtc -O dts`. Результат: `vcc-gmac-phy: disabling` исчезло, end1 инициализируется за 11 с (было 74 с), `sa02m-end1-link` больше не запускается. (2) `sed -i 's/\r//'` исправил CRLF — logrotate работает, 0 failed services. (3) Platform udev-правило задеплоено через `pscp`.

---

## [2026-06-03 09:00] branch: main

**Файл(ы):** `etc/udev/rules.d/50-sa02m-i2c2-unbind.rules`, `etc/sa02m-i2c2-unbind.sh`, `tools/imaging/out/patch_dtb_all.py`
**Тип:** Логическая ошибка / линк end0/end1 на рабочей плате
**Описание:** На рабочей плате с PCA9536 снова не поднимался линк (та же картина, что раньше): GMAC инициализировался одновременно с IRQ storm на `i2c-2` (`1c2b800.i2c`).
**Причина:** udev unbind срабатывал только при появлении адаптера `i2c-2` — уже после начала bus-recovery IRQ. Промежуточно в DTB ошибочно оставили `i2c@1c2b800 status = "disabled"` (ломало PCA9536); эталонный DTB на FAT/share должен быть с `status = "okay"` и ранним unbind.
**Исправление:** Правило udev дополнено: unbind на `SUBSYSTEM=="platform", KERNEL=="1c2b800.i2c"` (раньше, чем `i2c-2`). `sa02m-i2c2-unbind.sh` учитывает `SUBSYSTEM=platform`. Эталонный DTB: GMAC `okay`, `syscon=0x02`, `dc1sw` + `regulator-always-on`, `i2c@1c2b800` = `okay` (md5 `d521407b...`). Полный набор из BUGLOG: `threadirqs`, `allow-hotplug end1`, restore-dtb.

---

## [2026-06-02 17:30] branch: 1.0.3.23

**Файл(ы):** `/mnt/fat/boot.scr`, `/usr/local/share/sa02m/boot.scr`, `/usr/local/sbin/sa02m-restore-dtb.sh`
**Тип:** Зависание системы / IRQ storm
**Описание:** Устройство полностью зависало (SSH + serial недоступны) при загрузке на рабочей плате с RTC и PCA9536 (расширитель I/O на I2C3 / `1c2b800.i2c`). Проблема возникла после включения GMAC (`end1`).
**Причина:** PCA9536 на рабочей плате удерживает SDA низким при старте. Драйвер `mv64xxx_i2c` для шины `i2c-2` (`1c2b800.i2c`, TWI3, PI0/PI1) при такой ситуации входил в бесконечный IRQ storm (непрерывные прерывания). Без GMAC нагрузки хватало ресурсов CPU для выполнения `udev unbind` + `sa02m-pre-start.sh` до заморозки. С включённым GMAC (дополнительная нагрузка: dwmac-sun8i probe + MDIO/PHY init) совокупный IRQ storm успевал заморозить систему до того как userspace-механизм мог вмешаться.
**Исправление:** Добавлен `threadirqs` в `bootargs` в `/mnt/fat/boot.scr` через `mkimage`. `threadirqs` переводит все аппаратные IRQ в kernel threads — планировщик может вытеснять поток I2C IRQ и давать CPU другим задачам (udev unbind, sa02m-pre-start). Система перестала зависать. Canonical `boot.scr` сохранён в `/usr/local/share/sa02m/boot.scr`. Скрипт `sa02m-restore-dtb.sh` расширен для защиты и `boot.scr` вместе с DTB.

---

## [2026-06-02 17:00] branch: 1.0.3.23

**Файл(ы):** `/etc/network/interfaces.d/end1.conf`
**Тип:** Логическая ошибка / зависание загрузки
**Описание:** После включения GMAC (`end1`) устройство зависало при загрузке на рабочей плате с RTC и I2C-устройствами: SSH недоступен ~5 минут, иногда дольше.
**Причина:** `end1.conf` содержал `auto end1` вместо `allow-hotplug end1`. При `auto` `networking.service` ждёт завершения `ifup end1` (DHCP) синхронно — до 300 секунд, если DHCP-сервер не ответил на порту end1 рабочей платы. I2C-шина (`i2c-2`, `1c2b800.i2c`, TWI3, PI0/PI1) с PCA9536 не является причиной: пины I2C3 (PI0/PI1) не пересекаются с GMAC (PA0-PA15); IRQ storm уже обрабатывается существующим udev-правилом `50-sa02m-i2c2-unbind.rules` + `sa02m-pre-start.sh`.
**Исправление:** `auto end1` → `allow-hotplug end1` в `/etc/network/interfaces.d/end1.conf`. При `allow-hotplug` `ifup` запускается в фоне когда появляется link, не блокируя `networking.service` и SSH. Время загрузки до SSH снизилось с >300с до <40с.

---

## [2026-06-02 16:00] branch: main+1

**Файл(ы):** `etc/sa02m-restore-dtb.sh`, `etc/systemd/sa02m-restore-dtb.service`, `etc/apt/99-sa02m-dtb-protect`, `tools/imaging/out/sun8i-a40i-sk-fixed.dtb`
**Тип:** Некорректное поведение / Регрессия после обновления
**Описание:** После `apt upgrade` второй Ethernet `end1` (GMAC, `allwinner,sun8i-r40-gmac`) переставал появляться в системе. DHCP на `end1` не работал.
**Причина:** FAT-раздел (`/dev/mmcblk2p1`) содержит `sun8i-a40i-sk.dtb`, загружаемый U-Boot напрямую. В версии DTB от мая 2026 г. узел `ethernet@1c50000` (GMAC) содержал `status = "disabled"` вместо `"okay"`. Ядро не инициализировало `dwmac-sun8i`, интерфейс `end1` не создавался. Диагностические признаки: `vcc-gmac-phy: disabling` в dmesg; отсутствие `end1` в `ip link`; `/proc/device-tree/soc/ethernet@1c50000/status = disabled`.
**Исправление:** Восстановлен оригинальный DTB (`sun8i-a40i-sk.dtb`, GMAC `status = "okay"`) на FAT-разделе. Создан `end1.conf` с DHCP. Для предотвращения повторения: эталонная копия DTB размещена в `/usr/local/share/sa02m/sun8i-a40i-sk.dtb`; скрипт `/usr/local/sbin/sa02m-restore-dtb.sh` проверяет и восстанавливает DTB по md5; systemd-сервис `sa02m-restore-dtb.service` запускает его при каждой загрузке (`Before=networking.service`); apt-хук `/etc/apt/apt.conf.d/99-sa02m-dtb-protect` вызывает скрипт после любого `apt install/upgrade`.

---

## [2026-06-02 13:05] branch: 1.0.3.22

**Файл(ы):** `www/network_config/static/js/mqtt.js`, `www/network_config/cgi-bin/mqtt_status.cgi`, `etc/sa02m-mqtt-external-info.py`
**Тип:** Некорректное поведение
**Описание:** Вкладка MQTT: «● Нет данных» у брокера/моста, на дашборде службы MQTT активны.
**Причина:** `/usr/local/sbin/sa02m-mqtt-external-info.py` с CRLF в shebang (`python3\r`) — sudo от www-data падал; `apiGet` не проверял HTTP/ошибку JSON; при сбое UI оставался начальный «Нет данных».
**Исправление:** `sed` LF на устройстве; fallback `systemctl is-active` в `mqtt_status.cgi`; хост из bash; устойчивый `refreshBrokerStatus` и `apiGet` в mqtt.js v1.2.1.

---

## [2026-06-02 12:57] branch: 1.0.3.22

**Файл(ы):** `opt/sa02m-flasher/sa02m_flasher/firmware_repo.py`, `opt/sa02m-flasher/sa02m_flasher/runner.py`, `opt/sa02m-flasher/tests/test_firmware_repo.py`
**Тип:** Некорректное поведение
**Описание:** В «Прошивка выбранных устройств» отображались неподдерживаемые и устаревшие образы (например `MR-02m_full_*.bin`).
**Причина:** Репозиторий показывал все файлы из манифеста/кеша без фильтра под Modbus-прошивальщик и без удаления старых версий.
**Исправление:** `is_flasher_supported_file` (отсев `*_full_*`, .elf, oversized .bin); в каждой паре channel+kind остаётся только max version; старые/неподдерживаемые файлы удаляются из `/var/lib/sa02m-flasher/firmware/`; проверка в `runner._load_firmware_for_flash`.

---

## [2026-06-02 12:57] branch: 1.0.3.22

**Файл(ы):** `www/network_config/index.html`, `www/network_config/static/js/app.js`, `www/network_config/static/css/main.css`
**Тип:** Некорректное поведение / доработка UI
**Описание:** Ethernet 0/1 (end0/end1) в дашборде и вкладке «Сеть»; RX/TX в одной строке; лишняя подсказка hw.conf; широкие блоки настроек.
**Причина:** Устаревшие подписи и вёрстка; `end0-traf` одной строкой; подсказка «Аппаратные каналы настроены» при нормальной конфигурации.
**Исправление:** Переименование в Ethernet № 1/№ 2; RX и TX отдельными строками; настройки в `.widget`; скрытие hw-hint при OK; вкладка «Сеть» — `width: 50%` для пары виджетов; кнопки «Применить».

---

## [2026-06-02 14:00] branch: 1.0.3.22

**Файл(ы):** `www/network_config/static/js/mqtt.js`, `www/network_config/cgi-bin/mqtt_status.cgi`, `etc/sa02m-mqtt-external-info.py`, `etc/sudoers.d/sa02m-mqtt`
**Тип:** Некорректное поведение
**Описание:** В «Подключение MQTT с ПК» при клике на пароль — «Пароль не задан», хотя `MQTT_PASS` есть в `/etc/sa02m_mqtt.env`.
**Причина:** Пароль не попадал в JSON (сбой/ограничение sudo для www-data); UI не перезапрашивал credentials при клике; маска `******` показывалась и при пустом пароле.
**Исправление:** Fallback чтения env через `sudo cat`; парсинг `MQTT_PASS`/`MQTT_PASSWORD`; sudoers `SA02M_MQTT_ENV`; в UI — повторный запрос при клике, «—» если пароль недоступен, копирование после успешной загрузки.

---

## [2026-06-02 12:30] branch: 1.0.3.22

**Файл(ы):** `tools/imaging/make-image.sh`, `tools/imaging/stream-after-cleanup.sh`
**Тип:** Некорректное поведение
**Описание:** Донор 192.168.1.136 перезагружался через ~1–2 мин после старта zero-fill при `make-image.sh` (unexpected reboot, снятие образа прерывалось).
**Причина:** `RuntimeWatchdogSec=10s` (systemd HW watchdog) + нагрузка zero-fill на eMMC блокировали систему дольше таймаута; userspace watchdog с imaging lock не успевал помочь.
**Исправление:** Перед stream: stop+mask `sa02m-userspace-watchdog`, `sa02m-failure-monitor`, `net-watchdog`, `sa02m-watchdog-feed`; `systemctl set-property --runtime Manager RuntimeWatchdogSec=0`. То же в начале `stream-after-cleanup.sh`. Убран `systemctl restart sa02m-userspace-watchdog` перед снятием.

---

## [2026-06-02 12:03] branch: 1.0.3.22

**Файл(ы):** `etc/sa02m-armbian-branding.sh`, `/etc/update-motd.d/10-armbian-header`, `/etc/armbian-release`, `/etc/armbian-image-release` (192.168.1.113, 192.168.1.136)
**Тип:** Некорректное поведение
**Описание:** В MOTD при SSH-логине отображалось `Support: DIY (community maintained)` вместо поддержки CYNTRON.
**Причина:** `10-armbian-header` выводит `HARDWARE_STATUS` по `BOARD_TYPE=csc`; скрипт брендинга менял только BOARD/VENDOR в release-файлах, не строку Support.
**Исправление:** В `sa02m-armbian-branding.sh` — патч MOTD для SA-02m/CYNTRON → `cyntron.ru` (зелёный), `VENDORSUPPORT=https://cyntron.ru` в release-файлах; развёрнуто на .113 и .136.

---

## [2026-06-02 12:02] branch: 1.0.3.22

**Файл(ы):** `/etc/armbian-release`, `/etc/armbian-image-release` (192.168.1.113), `etc/sa02m-armbian-branding.sh`, `scripts/01-system.sh`
**Тип:** Некорректное поведение
**Описание:** MOTD и login banner показывали «Banana Pi M2 Ultra» вместо «CYNTRON SA-02m».
**Причина:** `/etc/armbian-release` сохранял upstream `BOARD=bananapim2ultra` и `BOARD_NAME="Banana Pi M2 Ultra"`; MOTD (`10-armbian-header`) подхватывает `BOARD_NAME` из этого файла после `armbian-image-release`.
**Исправление:** На .113: `BOARD=SA-02m`, `BOARD_NAME="CYNTRON SA-02m"`, `VENDOR=CYNTRON` в обоих release-файлах. В репозитории: idempotent `etc/sa02m-armbian-branding.sh`, вызов из `scripts/01-system.sh`.

---


**Файл(ы):** `www/network_config/cgi-bin/status.cgi`, `www/network_config/static/js/app.js`, `www/network_config/index.html`
**Тип:** Некорректное поведение
**Описание:** В виджете «Система» не отображалась версия Armbian (только плата, CPU, ядро).
**Причина:** Поле `armbian_version` не собиралось в `gather_system_metrics()` и не выводилось в UI (`index.html` / `applySystemStatus`). Отключение `SA02M_STATUS_ENABLE_HARDWARE=0` на .113 не связано — блок hardware управляет виджетом дискретных выходов, не «Система». `SA02M_STATUS_ENABLE_SYSTEM=1` на обоих устройствах.
**Исправление:** Чтение `ARMBIAN_PRETTY_NAME` из `/etc/os-release` (fallback: `VERSION` из `/etc/armbian-release`), поле в JSON `part=system`/`main`; строка `#armbian-info` в виджете. Развёрнуто на .113 и .136; на .113 дополнительно `sa02m-status-blocks-guard set hardware 1` + `confirm`; сброшен кэш status.cgi.

---

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
