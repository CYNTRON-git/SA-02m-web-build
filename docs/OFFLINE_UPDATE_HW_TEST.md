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

#### 3.5.1 Воспроизведение полевого инцидента 1.0.6.52 (OTA с локального репозитория)

Полный рецепт RED (старый код, откат исправного обновления после перезагрузки)
→ GREEN (1.0.6.52) на стенде без зависимости от интернета. Гарантии, которые
он подтверждает, — `docs/contracts/web-update.md` «Жизненный цикл apply».

**Рабочая станция.** `git clone --bare <repo> C:\bench\SA-02m-web-build.git`;
ветку под тестом положить как `main` в этот bare-репозиторий (`git push
C:\bench\SA-02m-web-build.git <sha>:refs/heads/main --force` — это стендовый
bare-репозиторий, не origin; `docs/decisions/no-force-push-version-branches.md`
про origin); отдать его `git daemon --export-all --base-path=C:\bench
--enable=upload-pack --listen=<ws-ip> --port=9418 --reuseaddr` (TCP 9418 открыть
в брандмауэре). Dumb HTTP не годится: хелпер клонирует `--depth=1`.

**Плата (каждая запись — с ведома Оператора).** Единственный шов URL хелпера —
окружение процесса (`SA02M_WEB_BUILD_REPO_URL` / `…_ALLOWLIST` в
`etc/sa02m-web-update-apply.sh`; `/etc/sa02m_web_build.conf` несёт только
ветку), поэтому:

| # | Запись | Откат |
|---|---|---|
| W1 | `/etc/systemd/system/fcgiwrap.service.d/zz-bench-ota.conf`: `[Service]` `Environment=SA02M_WEB_BUILD_REPO_URL=git://<ws-ip>/SA-02m-web-build.git` `Environment=SA02M_WEB_BUILD_REPO_ALLOWLIST=git://<ws-ip>/SA-02m-web-build.git` + `daemon-reload` + `restart fcgiwrap` | `rm` + `daemon-reload` + `restart fcgiwrap` |
| W2 | `/etc/sudoers.d/zz-bench-ota-env` (0440, `visudo -c`): `Defaults:www-data env_keep += "SA02M_WEB_BUILD_REPO_URL SA02M_WEB_BUILD_REPO_ALLOWLIST"` | `rm` |
| W3 | Даунгрейд до 1.0.6.49: `git archive --format=tar.gz -o SA-02m-full-1.0.6.49.tar.gz b46895b` → `/tmp` платы → `bash /tmp/sa02m-upd/scripts/offline-full-update.sh --force` (≈10 мин) | GREEN-прогон оставляет плату на новой версии |

`check.json` при живом интернете не трогать (часовая проверка видит GitHub
main новее 1.0.6.49 — охранник Apply пропускает, хелпер клонирует уже со
стенда); без интернета — записать вручную (`checked_at` сейчас UTC,
`deployed_version` 1.0.6.49, `remote_version` новее, `update_available` true).

**RED (стендовый `main` = 6ba943d, плата 1.0.6.49).** «Проверить» →
«Применить». Ожидается: deploy ≈10–15 мин; затем `pgrep -f '/sa02m-update/runner/'`
пуст, `transaction.json` `stage=verifying progress_pct=85`, `/run/sa02m-imaging.lock`
есть, `busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1
org.freedesktop.systemd1.Manager RuntimeWatchdogUSec` = `t 0`, `sa02m-flasher` /
`net-watchdog` неактивны, `update.log` кончается `health: restarting fcgiwrap...`,
панель «Проверка сервисов…» 85 %, `GET web_update_apply.cgi` → `status:"running"`.
«Перезагрузка» (web) → измерить секунды до ответа `/login.html` (ожидается ≥180),
`update.log` показывает recover → таймауты fcgiwrap/nginx → `unit not active:
nginx` → откат, `VERSION` = 1.0.6.49. Измерено 2026-09-23: 311 с, и recover
был убит своим `TimeoutStartSec=300` посреди отката (`Result=timeout`) —
плата осталась на `stage=rolling_back` с `/run/sa02m-imaging.lock` и
`RuntimeWatchdogUSec=0`; перед GREEN-прогоном её чинит
`scripts/sa02m-update-remedy.sh` (`docs/deployment.md`).

**GREEN (стендовый `main` = ветка 1.0.6.52, плата снова 1.0.6.49).** «Проверить»
→ «Применить». Это доставляющее обновление — весь apply идёт под раннером
1.0.6.49 (он `exec`'ит копию себя), поэтому ожидается ТО ЖЕ замирание:
`update.log` кончается `health: restarting fcgiwrap...`, панель «Проверка
сервисов…» 85 %; через 120 с новый CGI (уже на диске) отдаёт `stale:true,
error_code:E_RUNNER_LOST`, старый бандл пишет в журнал событий «Обновление
прервано на этапе …: перезагрузите плату». «Перезагрузка» (web) → страница
входа в обычное время (< 90 с), `journalctl -u sa02m-update-recover -b`: `post-boot
verification`, `sa02m-update-verify.service scheduled`; `journalctl -u
sa02m-update-verify -b` после nginx: `verify: post-boot verification`, для
`sa02m-devices-api` на стенде — строка про Condition; `DONE: update verified
after boot`; `transaction.json` `stage=done`; watchdog `t 15000000` (fallback
политики — hold брал код 1.0.6.49, поля нет); imaging-lock снят; `sa02m-flasher`
и `net-watchdog` active; VERSION 1.0.6.52. Вариант без перезагрузки —
`scripts/sa02m-update-remedy.sh` вместо «Перезагрузки»: ожидается `runner
supports reclaim`, в `update.log` — `recover: stage=verifying … context=runtime`,
`health: restarting fcgiwrap...` и `restarted after apply: …` (наборы
перезапусков, до которых старый раннер не дошёл), `sa02m-update-verify.service
scheduled`, затем журнал verify как выше; это первый прогон ветки `reclaim` на
плате. Сам выход из cgroup
(`runner cgroup: …`, `re-launching as transient unit`, `systemctl status
'sa02m-update-apply-*'` = running сквозь `restarting fcgiwrap...`,
`restarted after apply: …`, `DONE: update applied successfully` без
перезагрузки) доказывается только следующим OTA — например, стендовый `main`
на `1.0.6.52` + один коммит с поднятой версией. Затем W6: `transaction.json` вручную на `stage=verifying`,
`files_done=files_total`, `target_version=<новая>`, `updated_at` старый;
`date -Iseconds > /run/sa02m-imaging.lock`; `GET web_update_apply.cgi` →
`stale:true, error_code:E_RUNNER_LOST`; «Перезагрузка» → страница входа в
обычное время (< 90 с), `journalctl -u sa02m-update-verify -b` после nginx,
`transaction.json` `stage=done`, версия не откатилась, watchdog 15000000. Один
раз повторить W6 с переименованным `sa02m-update-verify.service`, чтобы
проверить fallback `systemd-run -p After=` на systemd 255; файл вернуть.
Очистка: W1, W2 снять.

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
