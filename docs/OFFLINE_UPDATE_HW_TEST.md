# Офлайн-обновление: стендовые проверки (Tier C)

Пошаговая процедура проверки офлайн-пакета `.sa02m`, резервной копии и
config-only factory reset на стенде SA-02m. **Без** прошивки eMMC / rootfs.

Связанные уровни тестов (план §8): Tier A — unit на ПК; Tier B — VM/chroot;
Tier C — этот документ; Tier D — ручной power-cut только на жертвенном стенде.

---

## Доступы стенда

| | Значение |
|--|----------|
| IP | `192.168.1.136` |
| SSH | `root` / `cyntron` через `py -3 tools/ssh/sa02m_remote.py` |
| Веб | `http://192.168.1.136:9999`, `admin` / `cyntron` |

Единый env: [`tools/sa02m-device.env`](../tools/sa02m-device.env). Подробности SSH:
[`docs/AGENTS_SSH_AND_DEVICE_ACCESS.md`](AGENTS_SSH_AND_DEVICE_ACCESS.md).

**Запрещено** на стенде в рамках этой процедуры:

- `dd` / self-flash образа на работающую eMMC;
- factory-reset wipe без явного флага (см. ниже).

---

## 1. Unit-тесты на ПК (Tier A)

После появления пакетов `opt/sa02m-update` / `opt/sa02m-alice`:

```powershell
cd C:\Users\admin\Downloads\SA-02m-web-build
py -3 tools/update/run_unit_tests.py -v
```

Если каталогов тестов ещё нет — скрипт завершится с кодом 0 и сообщением
`Nothing to run` (bootstrap).

---

## 2. Автоматические HTTP-пробы (Tier C smoke)

Скрипт логинится в веб-UI, проверяет CGI обновления/бэкапа/сброса и **не**
запускает wipe по умолчанию.

```powershell
cd C:\Users\admin\Downloads\SA-02m-web-build
py -3 tools/update/hw_acceptance_update.py
```

Опции:

| Флаг | Назначение |
|--|--|
| `--base-url http://192.168.1.136:9999` | Базовый URL UI |
| `--user` / `--password` | Учётная запись веб (по умолчанию admin/cyntron) |
| `--skip-ssh` | Не вызывать `sa02m_remote.py` (нет CSRF с устройства) |
| `--factory-wipe` | **OPT-IN**, по умолчанию выкл. POST сброса настроек (разрушительно) |

Что проверяется:

1. SSH sanity (`hostname`, nginx/fcgiwrap) через `sa02m_remote.py`.
2. Login → cookie `session_token`.
3. `GET /cgi-bin/web_update_check.cgi` (авторизованный JSON).
4. `GET /cgi-bin/web_backup.cgi` — тело > 1 KiB, в архиве есть `backup-manifest.json`
   (если CGI ещё не задеплоен — SKIP).
5. `POST` крошечного невалидного `.sa02m` на `web_update_upload.cgi` — ожидается
   ошибка валидации, **не** HTTP 500.
6. `web_update_cancel.cgi` — POST без `X-SA02M-CSRF` не должен вернуть `ok:true`.
7. `GET web_factory_reset.cgi` — статус без старта wipe.
8. Wipe — только с `--factory-wipe`.

Полный signed apply валидного пакета **не** входит в этот smoke: нужен
`scripts/pack-offline-update.py` и ключ подписи (`private/sa02m-update-keys/`).

---

## 3. Ручной чеклист на стенде

Перед прогоном убедиться, что релиз N (bootstrap updater) уже на устройстве:
runner, trusted key, nginx locations, CSRF в `lib_web_auth.sh`.

### 3.1 Подготовка

```powershell
py -3 tools/ssh/sa02m_remote.py exec "cat /var/www/network_config/VERSION; systemctl is-active nginx fcgiwrap; ls -la /var/lib/sa02m-update 2>/dev/null | head"
py -3 tools/update/hw_acceptance_update.py
```

### 3.2 Резервная копия

1. Войти в UI → **Управление → Обновление / Резервная копия**.
2. Скачать архив через UI или `GET web_backup.cgi`.
3. Проверить: первый member — `backup-manifest.json`; предупреждение о секретах показано.
4. Сохранить файл offline (содержит пароли/ключи).

### 3.3 Невалидный пакет

1. Загрузить заведомо битый файл (или мусор с расширением `.sa02m`).
2. UI/CGI должны показать ошибку валидации (`E_TRAILER` / `E_SIG` / `E_TAR` …),
   без падения fcgiwrap (нет 500).

### 3.4 Валидный `.sa02m` (после packer + ключ)

1. Собрать пакет на ПК: `py -3 scripts/pack-offline-update.py …`
2. UI → **Из файла** → загрузка → inspect (version, signature_ok).
3. «Создать резервную копию и установить» → polling стадий
   (upload → inspect → backup → apply → verify → done).
4. После done: `VERSION` совпадает с пакетом; `nginx`/`fcgiwrap` active.
5. Параллельный apply → `E_LOCK`.

### 3.5 GitHub OTA (регрессия)

При наличии сети: **Проверить** / **Применить** интернет-OTA; убедиться, что
общий runner не сломан.

### 3.6 Factory reset (осторожно)

1. Пройти UI: предупреждения → обязательный backup → ввод `SA02M-RESET`.
2. Либо осознанно:  
   `py -3 tools/update/hw_acceptance_update.py --factory-wipe`
3. Проверить preserve: `machine-id`, SSH host keys, cloud secret, Alice cert,
   кеш MR firmware, Node-RED/CODESYS/MPLC не тронуты.
4. Пароль веб → `admin` / `cyntron`.

**Не** запускать wipe на полевом устройстве «на всякий случай».

### 3.7 Imaging-lock / watchdog

Во время длинного apply (≥ несколько минут) не должно быть reboot от
`sa02m-userspace-watchdog` / `net-watchdog` (есть `/run/sa02m-imaging.lock`,
HW watchdog runtime отключён на время операции).

---

## 4. SSH-восстановление бэкапа

Только по SSH (не через www-data CGI):

```bash
sa02m-restore-backup.sh --dry-run /path/backup.tar.gz
sa02m-restore-backup.sh --apply    /path/backup.tar.gz
```

После `--apply`: `nginx -t` + reload, без reboot.

---

## 5. Критерии приёмки (кратко)

См. полный список в плане (acceptance criteria). Минимум для стенда:

- [ ] `run_unit_tests.py` зелёный (когда пакеты есть)
- [ ] `hw_acceptance_update.py` без FAIL (SKIP допустим до деплоя CGI)
- [ ] Backup download с `backup-manifest.json`
- [ ] Невалидный `.sa02m` → validation error, не 500
- [ ] Cancel без CSRF отклонён
- [ ] GET factory reset не стартует wipe
- [ ] (опционально, lab) wipe с `--factory-wipe` / UI + preserve-блок

---

## 6. Как запускать (шпаргалка)

```powershell
cd C:\Users\admin\Downloads\SA-02m-web-build

# Tier A (ПК)
py -3 tools/update/run_unit_tests.py -v

# Tier C smoke (стенд .136), wipe выключен
py -3 tools/update/hw_acceptance_update.py

# Только HTTP, без SSH
py -3 tools/update/hw_acceptance_update.py --skip-ssh

# Разрушительный сброс настроек (lab only)
py -3 tools/update/hw_acceptance_update.py --factory-wipe
```
