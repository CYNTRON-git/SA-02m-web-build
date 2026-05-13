# Расследование SSH и serial/RS-485 (ветка 1.0.3.3)

Документ для последующего анализа: гипотезы, правки в репозитории, попытки на плате, выводы и **что осталось сделать**.

## Исходная постановка

- На **отладочной** плате SSH стабилен; на **рабочей** — «отваливается».
- Версия изделия: **один Ethernet (1eth)**; тестовая плата может быть на **двух Ethernet (2eth)** с той же строкой модели SoM (`Cyntron A40i-2Eth` и т.п.).
- Вопрос: не влияет ли **RTC/init**, сеть, watchdog, перевод SSH в direct mode.

## Гипотезы (проверка по цепочке)

| Гипотеза | Статус | Комментарий |
|----------|--------|-------------|
| RTC / `hwclock` ломает SSH | **Маловероятно** | `apply.cgi` пишет время по явному действию пользователя; в цепочке SSH не участвует напрямую. |
| Сеть / `fix-eth` / net-watchdog рвёт сессию | **Частично** | Могут влиять на доступность порта; добавлены debug-логи в `fix-eth.sh`. Отдельно проверять при «отвале» именно линка vs зависания `sshd`. |
| Неверная карта UART (`ttyS0` как RS-485 на 2eth) | **Подтверждена как риск** | В README зафиксировано: на **2eth** `ttyS0` не должен быть COM1/RS-485-0. Установщик раньше всегда вешал `COM1` на `ttyS0`. |
| SSH socket activation vs `ssh.service` | **Требует мониторинга** | Скрипт `sa02m-ssh-direct.sh` переводит в direct mode; добавлены логи в `/var/log/sa02m_fix_ssh.log`. |
| Зависание **после** успешной аутентификации | **Подтверждено на рабочей** | `ssh -vvv`: `Authenticated ... using "publickey"` → далее `channel 0: send open` → ответа нет (таймаут). Это уже **не** сеть и не ключ до auth; кандидаты: PAM/session, shell при логине, конфиг `sshd`, перегрузка/блокировка на стороне сервера. |

## Правки в репозитории (код и конфиги)

### Профиль serial / RS-485 (1eth vs 2eth)

- **`scripts/lib.sh`**: функции `sa02m_board_model`, `sa02m_serial_profile_from_file`, `sa02m_serial_profile` (переменная окружения → `/etc/sa02m_serial_profile.conf` → fallback по наличию `eth1`), `sa02m_serial_targets`, `write_sa02m_serial_map_conf`.
- **`scripts/01-system.sh`**: установка/фиксация `/etc/sa02m_serial_profile.conf`, очистка старых `/dev/COM*`, `/dev/RS-485-*`, создание symlink и udev `99-sa02m-serial.rules`, запись `/etc/sa02m_serial_map.conf`.
- **`install.sh`**: опция `--serial-profile` → `SA02M_SERIAL_PROFILE`.
- **`etc/sa02m_serial_profile.conf`**: шаблон с комментариями (`sa02m-1eth` / `sa02m-2eth`).

**Важно для изделия 1eth на тестовом стенде с `eth1`:** профиль нужно **явно** задавать (`sa02m-1eth`), иначе по `eth1` ошибочно выберется `sa02m-2eth`.

### Веб / flasher

- **`www/network_config/cgi-bin/status.cgi`**: число портов RS-485 из `SA02M_SERIAL_COUNT` в `/etc/sa02m_serial_map.conf`.
- **`www/network_config/static/js/app.js`**: удаление лишних карточек RS-485 при смене числа портов.
- **`opt/sa02m-flasher/sa02m_flasher/config.py`**: загрузка карты портов из `/etc/sa02m_serial_map.conf`; исправлен default-arg для тестов.
- **`opt/sa02m-flasher/tests/test_config.py`**: юнит-тест карты для профиля 2eth.

### SSH и сеть (диагностика)

- **`etc/sa02m-ssh-direct.sh`**: лог состояния до/после переключения (модель платы, unit states, listeners).
- **`etc/fix-eth.sh`**: дополнительные строки `debug_iface_state` в ключевых точках.
- **`www/network_config/cgi-bin/ssh_debug.cgi`**: секции «board and serial map», прочее для снимка состояния.

### Прочие изменения в рабочем дереве (тот же коммит)

В коммит вошли также правки по другим трекам разработки (см. `git show --stat`), в т.ч.:

- `mplc_lease.py`, CGI (`apply`, `config`, `reboot`, `restart`), `lib_hw`, `index.html`, `README`, `99-lan-recovery`, systemd units для fix-eth, `02/03-network/webserver`, `update-www-only.sh`, конфиги `sa02m_hw.conf` / `storage` и др.

Их детальный разбор — по diff коммита; **данный документ** сфокусирован на линии SSH + serial.

## Попытки на устройстве (хронология)

1. **Деплой по SSH/SFTP/paramiko** на `192.168.1.136` с ключом `private/.ssh/sa02m_sa02` — файлы доставлены; отдельный прогон `paramiko` иногда давал `Timeout opening channel` при `exec_command` после успешного `connect`.
2. **Фиксация профиля `sa02m-1eth`** на плате при наличии `eth1` на стенде — записан `/etc/sa02m_serial_profile.conf`, перегенерированы `/etc/sa02m_serial_map.conf` и symlink в `/dev`.
3. **`curl …/reboot.cgi`** с ожиданием падения `:22` — порт **не падал** за отведённое окно; вывод: либо reboot не выполнился, либо окно наблюдения/маршрут не поймали реальный ребут.
4. **Серия `ssh … echo ok` (20 раз)** — все попытки **таймаут** на клиенте (Windows OpenSSH, timeout 8s).
5. **`ssh -vvv`** — KEX и **publickey auth успешны**; зависание после `Authenticated`, на открытии session channel.

## Выводы на текущий момент

1. **Карта UART и явный профиль 1eth** — необходимы для корректности COM/RS-485 и flasher на смешанных стендах; задокументировано в шаблоне и в `install.sh`.
2. **Симптом «SSH отваливается»** на наблюдавшейся рабочей плате соответствует скорее **зависанию/блокировке после аутентификации**, а не «порту 22 закрыт» в чистом виде: веб-диагностика может показывать listener, при этом интерактивная сессия не поднимается.
3. **RTC** как первопричина текущего зависания channel — **не подтверждена**; остаётся вторичным фактором (время, логи).
4. **Следующий слой анализа** — исключительно на стороне **Ubuntu/OpenSSH на плате**: `journalctl -u ssh`, `sshd -T`, PAM (`/etc/pam.d/sshd`), профили shell root (`/root/.profile`, `.bashrc`), лимиты `MaxStartups`, нестандартные `Match`/`ForceCommand`, а также проверка, не слушает ли `:22` нестандартный процесс (хотя `ss` в снимках указывал на `sshd`).

## Что сделано (кратко)

- Введён явный профиль serial **`sa02m-1eth` / `sa02m-2eth`** с файлом override и опцией установщика.
- Генерация **`/etc/sa02m_serial_map.conf`**, синхронизация **status.cgi**, **flasher config**, **RS-485 UI**.
- Расширена **диагностика** SSH и сетевого recovery.
- Зафиксированы **результаты тестов** на рабочей плате (post-auth hang).

## Что осталось (рекомендуемый backlog)

1. На проблемной плате: полный снимок **`journalctl -b -u ssh -u ssh.service --no-pager`**, **`sshd -T`**, сравнение с рабочей отладочной.
2. Проверить **PAM и shell** для `root` при non-interactive и interactive session.
3. Выяснить, почему **`reboot.cgi`** не дал наблюдаемого reboot (права `sudo`, `nohup`, реальный ли вызов `reboot`).
4. Повторить тест **после реального power-cycle** с логированием с первых секунд boot.
5. При необходимости: временно упростить `sshd_config` (отключить `UsePAM`, сменить subsystem и т.д.) — только на тестовой копии и с откатом.

## Дополнение: итог по SSH (последующая диагностика)

Сводка «в чём была проблема и как устранили» вынесена в отдельный документ:

- **`docs/SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md`** — задержки `UseDNS`/hosts, зависание после auth (MOTD, PAM systemd, I2C/RTC/CGI, фоновые сервисы), зависания всей системы из‑за потоковых SSH-команд (`journalctl -f`, `dmesg -w`, тяжёлый `systemctl`).

Таблицы гипотез выше сохраняются как история; приоритетную картину см. в новом файле.

---
*Ветка Git: `1.0.3.3`. Дата фиксации документа: 2026-05-11. Ссылка на итог SSH — `SA02M_SSH_ACCESS_PROBLEM_AND_FIX.md` (обновлялось вместе с веткой 1.0.3.5).*
