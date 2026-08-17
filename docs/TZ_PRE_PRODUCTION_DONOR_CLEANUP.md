# ТЗ: скрипт очистки мусора донора перед производственным образом SA-02m

**Статус:** выполнено (2026-08-07) — `cleanup-donor.sh` + apply на `.136`  
**Дата:** 2026-08-07  
**Контекст:** аудит `.136` — `/root` ≈ 786 MiB стендового мусора; текущий `tools/imaging/cleanup-donor.sh` удаляет только точное имя `/root/sa02m-deploy`, не `sa02m-deploy-*`.  
**Связанные файлы:** `tools/imaging/cleanup-donor.sh`, `tools/imaging/make-image.sh`, `tools/imaging/stream-after-cleanup.sh`, `docs/SA02M_IMAGING_GUIDE.md`, `docs/AGENTS_SSH_AND_DEVICE_ACCESS.md`

---

## 1. Цель

Сделать **безопасную, повторяемую** очистку донора перед снятием производственного образа eMMC так, чтобы:

1. В образ **не** попадали стендовые деплои, кеши, `.deb`, бэкапы разработки.
2. **Не** ломались продуктовые сервисы (nginx, MQTT, MPLC, CODESYS, flasher, веб, Alice conf/certs, сеть).
3. Скрипт встраивался в существующий пайплайн `make-image.sh` → cleanup → stream → PiShrink.
4. Был режим **dry-run** (показать, что будет удалено, без удаления) и отчёт «было/стало» по `df`/`du`.

**Не цель этого ТЗ:** zero-fill, reset machine-id/SSH host keys, PiShrink, запись образа — это уже `stream-after-cleanup.sh` / `make-image.sh`.

---

## 2. Проблема (факты со стенда 192.168.1.136)

| Путь / паттерн | ~размер | Почему мусор |
|---|---|---|
| `/root/sa02m-deploy-*`, `/root/sa02m-deploy` | 500+ MiB | Staging веб-деплоев (`www/`, `scripts/`, иногда почти весь репо, `deploy.tar.gz`) |
| `/root/deploy-*.tar`, `/root/deploy-*.tar.gz` | десятки MiB | Архивы деплоя после распаковки |
| `/root/.npm` | ~104 MiB | npm cache |
| `/root/*.deb` (CodeMeter, CODESYS, …) | ~63 MiB | Установщики; пакеты уже в dpkg |
| `/root/mplc_backup*`, `/root/mplc_update` | ~65 MiB | Стендовые бэкапы MPLC |
| `/root/www-backup-*.tgz`, `/root/flasher-backup-*.tgz`, `/root/preinstall-backup-*.tgz` | мало, но шум | Ручные бэкапы при разработке |
| `/root/zImage*.bak`, `/root/modules-*.bak*` | ~17 MiB | Бэкапы ядра при экспериментах |
| `/root/.cache`, history, Trash | мало | Стандартный мусор |

Рабочий продукт живёт в `/var/www/network_config`, `/opt/sa02m-*`, `/opt/mplc4`, `/opt/codesys`, `/etc/sa02m*`, `/var/lib/sa02m-*` — **их не трогать** (кроме явно оговорённого ниже).

---

## 3. Deliverable

### 3.1 Основной скрипт (обязательно)

**Путь:** расширить `tools/imaging/cleanup-donor.sh` **или** добавить `tools/imaging/cleanup-donor-junk.sh` и вызывать его из `cleanup-donor.sh` в фазе «мусор».

Предпочтение: **один вход** для `make-image.sh` — не ломать текущий контракт (`cleanup-donor.sh` фазы 1–4 без сброса SSH keys).

### 3.2 Опционально (желательно)

- Обёртка с хоста Windows/WSL: `tools/imaging/run-cleanup-donor.py` или вызов через `tools/ssh/sa02m_remote.py exec` / stdin script.
- Краткий раздел в `tools/imaging/README.md` + ссылка из `SA02M_IMAGING_GUIDE.md` (1 абзац + пример команд).
- Запись в `docs/bugs/BUGLOG.md`, если найден/закрыт пробел «`sa02m-deploy-*` не чистился».

### 3.3 Не создавать

- Отдельный UI / CGI очистки.
- Автозапуск cleanup по cron на полевых платах.
- Удаление продуктовых данных «на всякий случай».

---

## 4. Функциональные требования

### 4.1 Режимы запуска

| Режим | Флаг | Поведение |
|---|---|---|
| Dry-run | `--dry-run` (по умолчанию **рекомендуется** для первого прогона с хоста; на доноре в `make-image` — **apply**) | Только список путей и суммарный размер; exit 0 |
| Apply | `--apply` | Реальное удаление |
| Verbose | `--verbose` | Печать каждого удаляемого пути |
| Report-only sizes | `--report` | `du` до/после ключевых деревьев, без удаления (можно совмещать с dry-run) |

Без `--apply` скрипт **не должен** ничего удалять (fail-safe).  
Исключение для обратной совместимости с `make-image.sh`: если скрипт вызывается как сейчас без флагов из make-image — сохранить текущее поведение **apply** **или** явно передать `--apply` из `make-image.sh` (предпочтительно второе, с правкой make-image).

### 4.2 Что удалять (ALLOW-DELETE)

Реализовать через явные glob/списки (не `rm -rf /root/*`).

**A. Staging деплоев и архивы**

```
/root/sa02m-deploy
/root/sa02m-deploy.tar.gz
/root/sa02m-deploy-*
/root/sa02m-install-*
/root/sa02m-web-build          # если это копия репо, не runtime
/root/deploy-*.tar
/root/deploy-*.tar.gz
/root/deploy.tar.gz
```

**B. Кеши разработчика**

```
/root/.npm
/root/.cache
/root/.local/share/Trash
/root/.bash_history
/root/.viminfo
/root/.lesshst
```

Аналогично для `/home/*/` (cache, bash_history, Trash) — как сейчас.

**C. Оставшиеся установщики**

```
/root/*.deb
/root/*.rpm                    # на всякий случай
```

**D. Стендовые бэкапы / артефакты экспериментов**

```
/root/backup
/root/mplc_cyntron_build
/root/mplc_backup*
/root/mplc_update
/root/www-backup-*.tgz
/root/www-backup-*.tar.gz
/root/flasher-backup-*.tgz
/root/preinstall-backup-*.tgz
/root/f10-backup
/root/opt-bridge-backup-*
/root/cursor_build.swap
/root/u-boot-sunxi-with-spl.bin
/root/zImage*.bak*
/root/modules-*.bak*
/root/40-usb_modeswitch.rules
/root/-d /root/nul /root/NUL
"/root/ystemd-analyze critical-chain"   # уже было
```

**E. Логи / apt / tmp** (уже есть в фазе 4 — сохранить)

- `apt-get clean`, lists purge, journal vacuum, truncate/delete rotated logs, `/tmp`, `/var/tmp`.
- Дополнительно рассмотреть: `/var/log.hdd/*` (на `.136` syslog.1 ~64 MiB) — **truncate или rotate**, не удалять каталог целиком если на него завязан rsyslog; безопасный минимум: truncate больших `*.1`, `*.gz`.

**F. Update staging (осторожно)**

Разрешить очистку **только** эфемерного:

```
/var/lib/sa02m-update/staging/*
/var/lib/sa02m-update/incoming/*
/var/lib/sa02m-update/runner/*
/tmp/sa02m-*
```

**Запрещено** удалять без отдельного явного флага `--purge-update-state`:

```
/var/lib/sa02m-update/state/deployed_*
/var/lib/sa02m-update/trusted path N/A
/etc/sa02m-update/trusted-keys/*
```

По умолчанию rollback-архивы в `/var/lib/sa02m-update/rollback/` — **не трогать** (или удалять только с `--purge-rollback` и только файлы старше N дней; default: не трогать).

### 4.3 Что НЕ удалять (DENY — жёсткий запрет)

Скрипт обязан **отказать** (пропуск + warning), если путь матчится deny-листу, даже если попал в glob:

| Путь / префикс | Почему |
|---|---|
| `/var/www/network_config` | продуктовый веб |
| `/opt/mplc4` (кроме явных backup-копий вне opt, уже в /root) | runtime MPLC |
| `/opt/codesys` | runtime |
| `/opt/sa02m-*` | продуктовые агенты (код маленький, нужен) |
| `/etc/sa02m*` , `/etc/sa02m_*` | конфиги |
| `/var/lib/sa02m-alice` | mTLS certs (policy: preserve on factory reset) |
| `/var/lib/sa02m-flasher/firmware` | кеш прошивок MR (нужен offline) |
| `/etc/network`, `/etc/nginx`, `/etc/mosquitto*` | сеть/брокер |
| `/home/klogic` проектные рабочие файлы **кроме** `.cache`/history | не сносить home целиком |
| SSH keys донора | **не** трогать в этом скрипте (делает stream-after-cleanup) |
| `/boot`, ядро, dtb (кроме явных `*.bak` в `/root`) | |

Реализация: функция `is_denied(path)`; перед `rm` проверка canonical path.

### 4.4 Тулчейн (фаза 3 — сохранить)

Оставить purge build-essential/gcc/headers как в текущем `cleanup-donor.sh`.  
Не трогать: `nodejs` (Node-RED), `docker.io` — **без** отдельного флага `--purge-docker` (default off). Docker images на стенде почти пустые; пакет может быть нужен профилю.

### 4.5 Отчёт

В конце (stderr или `/tmp/sa02m-cleanup-report.txt`):

```
BEFORE: root_used=… /root=… 
DELETED: N paths, ~X MiB (estimated)
AFTER:  root_used=… /root=…
DENY_SKIPPED: …
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK (dry-run или apply) |
| 1 | usage / not root |
| 2 | частичный сбой rm (продолжить где возможно, но ненулевой exit) |
| 3 | обнаружен запрещённый путь в конфиге списков (ошибка ТЗ/конфига) |

---

## 5. Нефункциональные требования

1. **Bash**, `set -euo pipefail`, `LC_ALL=C`, запуск **только root**.
2. Идемпотентность: повторный `--apply` безопасен.
3. Не использовать интерактивные `apt`/`rm` prompts.
4. Не зависеть от git / сети / GitHub.
5. Совместимость с Armbian/Debian на armhf SA-02m.
6. Время на типичном доноре: &lt; 2 мин (без огромного apt purge аномалий).
7. Логирование всех действий с timestamp.
8. CRLF: при install с Windows — `sed -i 's/\r$//'` как у остальных скриптов.

---

## 6. Интеграция

1. `make-image.sh` продолжает вызывать cleanup; передать `--apply` если введён fail-safe default.
2. Документировать ручной прогон:

```bash
# на доноре
bash tools/imaging/cleanup-donor.sh --dry-run
bash tools/imaging/cleanup-donor.sh --apply

# с Windows-хоста (предпочтительно)
py -3 tools/ssh/sa02m_remote.py exec 'bash -s -- --dry-run' < tools/imaging/cleanup-donor.sh
# или upload + exec
```

3. **Не** включать cleanup в обычный `install.sh` / `update-www-only.sh` (только imaging / явный вызов).

---

## 7. Критерии приёмки

### 7.1 На ПК (без платы)

- `shellcheck` при наличии; иначе ручной review.
- `--dry-run` на синтаксически валидном fixture (опциональный mini-fixture в `.tmp/` или `tools/imaging/tests/`) показывает ожидаемые пути.

### 7.2 На доноре (стенд, если доступен)

1. `--dry-run`: в списке есть `sa02m-deploy-*`, `.npm`, `*.deb`; **нет** `/var/www/network_config`, `/opt/mplc4`.
2. `--apply`: `/root` заметно уменьшается (ориентир: с ~786 MiB до ≪ 100 MiB на текущем `.136`, если не появятся новые артефакты).
3. После apply: `systemctl is-active nginx fcgiwrap` (и критичные sa02m-*) остаются active / не сломаны.
4. `df -h /` — used снизился.
5. Повторный `--apply` — OK, без ошибок.

### 7.3 Регресс пайплайна

- `make-image.sh` с cleanup не падает на фазе 1–4 из‑за новых флагов.
- SSH host keys **не** удаляются этим скриптом.

---

## 8. Запреты безопасности

- **Запрещено** self-flash / `dd` на eMMC из этого скрипта.
- **Запрещено** `rm -rf /root` целиком.
- **Запрещено** трогать `/etc/ssh`, `machine-id` здесь.
- **Запрещено** удалять calibration / cloud enrollment / Alice certs.
- PowerShell на хосте: не передавать `$`/`|` голым ssh — использовать `sa02m_remote.py`.

---

## 9. Предлагаемый план работ агента

1. Прочитать `cleanup-donor.sh`, `make-image.sh` (вызов cleanup), imaging README.
2. Спроектировать списки ALLOW/DENY + флаги.
3. Реализовать dry-run/apply/report; сохранить фазы toolchain + apt/logs.
4. Обновить вызов из `make-image.sh` при необходимости.
5. Короткая док-правка (README + 1 абзац в imaging guide).
6. При возможности — dry-run + apply на `.136` через `sa02m_remote.py`; зафиксировать before/after в отчёте агента.
7. BUGLOG, если закрывается пробел по `sa02m-deploy-*`.

---

## 10. Definition of Done

- [ ] Скрипт удаляет все классы мусора из §4.2 A–E (и F по умолчанию для staging update).
- [ ] Deny-лист §4.3 соблюдён.
- [ ] `--dry-run` / `--apply` работают; make-image интегрирован.
- [ ] Документация обновлена.
- [ ] (Стенд) `/root` очищен от deploy-мусора; сервисы живы.
- [ ] Краткий отчёт агента: список изменений файлов + before/after размеры.

---

## 11. Вне scope (не делать в этой задаче)

- Чистка `armbian-firmware` / удаление Docker пакетов.
- Factory reset / wipe пользовательских конфигов.
- Пересборка образа и заливка на приёмники.
- Commit/push (только если пользователь отдельно попросит).
