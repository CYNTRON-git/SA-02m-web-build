# Контракт: `web_update_apply.cgi` — запуск интернет-обновления веб и его жизненный цикл

Домашний адрес контракта эндпоинта, который запускает root-хелпер
`sa02m-web-update-apply` (OTA с GitHub), и — с 1.0.6.52 — жизненного цикла
apply, который этот запуск открывает (раннер, recover при загрузке, статус для
панели, кнопка «Перезагрузка»). Здесь — ветка «POST без `confirm_version`»
(кнопка «Применить» в «Обновление веб») и ответ GET; офлайн-транзакция
(`confirm_version` + `sa02m-update.service`) описана в
`docs/OFFLINE_UPDATE_PACKAGE_V1.md`. Машинная грамматика (поля, коды) — на
английском (`PROTOCOL.md` invariant 5); пояснения — на русском.

## Порядок проверок (жёсткий)

1. Cookie `session_token` — невалидная сессия → `{"error":"unauthorized","ok":false}`,
   выход до любой работы.
2. Заголовок `X-SA02M-CSRF` — `web_csrf_validate` (политика
   `docs/decisions/selective-csrf-policy.md`); отсутствует/неверен → `E_CSRF`.
   Проверка `web-update-csrf-contract` пинит эту строку.
3. **Охранник «есть ли что применять»** — читает `check.json`
   (`/var/lib/sa02m-web-build/check.json`, пишет `sa02m-web-update-check`,
   таймер `hourly`) и **отказывает во всём, чего не может доказать** (с 1.0.6.39,
   аудит C10; до этого любая ошибка чтения РАЗРЕШАЛА запуск root-обновления).
4. Только после этого — `sudo -n /usr/local/sbin/sa02m-web-update-apply`.
5. Сам хелпер (`etc/sa02m-web-update-apply.sh`) **до клонирования** отказывает,
   если `transaction.json` стоит на `applying|verifying|committing|rolling_back`
   И раннер жив (тест живости — ниже): `update_status=error`, строка «обновление
   уже выполняется» в журнале, выход 1 (с 1.0.6.52; до этого второе «Применить»
   перезаписывало живую транзакцию своей). Мёртвый раннер не защищается —
   повторный запуск и есть один из путей восстановления.

## Ответы охранника (шаг 3)

Транспорт всегда HTTP 200 (идиома CGI-слоя, `web-code-rigor.md`); различать —
по телу.

| Состояние `check.json` | Ответ | Запуск |
|---|---|---|
| Свежий (`checked_at` не старше `WEB_UPD_CHECK_MAX_AGE_S` = 86400 с и не из будущего дальше 1 ч), `deployed_version` < `remote_version` | нет ответа охранника — переход к шагу 4 | да |
| Свежий, `deployed_version` ≥ `remote_version` — или версий нет, но `update_available` = `false` | `{"ok":false,"status":"error","error":"no_update","error_code":"E_NO_UPDATE","log":"Обновлений нет"}` | нет |
| Файла нет · JSON не разбирается · `checked_at` отсутствует/старше окна/из будущего · версии неразборчивы и `update_available` не `false` · нет `python3` | `{"ok":false,"status":"error","error":"check_stale","error_code":"E_CHECK_STALE","log":"Сведения об обновлении устарели — нажмите «Проверить»"}` | нет |

Инварианты:

- **Fail-closed.** Единственный путь к шагу 4 — прочитанный, свежий и
  разобранный `check.json`, в котором обновление действительно новее. Любая
  невозможность это доказать — отказ, а не запуск.
- **Свежесть.** Окно 24 ч = 24 периода таймера: мёртвый таймер или ушедшие
  часы не превращают вчерашний ответ в «сейчас». Кнопка «Проверить» в UI
  (`POST web_update_check.cgi` с заголовком `X-SA02M-CSRF`, с 1.0.6.49; GET
  `?force=1` проверку не запускает — только отдаёт кэш) пересобирает файл;
  после неё Apply снова доступен, если есть что применять. Гейты:
  `cgi-csrf-policy` (статически) и `cgi-csrf-behaviour` (реальный CGI в
  песочнице).
- Оба кода — **новые ошибки на мутирующем эндпоинте**, фронтенд обрабатывает
  их явно (`app/status.js` `webUpdApplyRefusal`): своя строка статуса, кнопка
  «Применить» остаётся выключенной; в общий путь «Ошибка обновления. См.
  Журнал событий» они не попадают.
- Путь к `check.json` переопределяется только окружением
  `SA02M_WEB_BUILD_STATEDIR` (то же имя, что у `etc/sa02m-update-runner.sh`) —
  для харнесса; nginx/fcgiwrap его не задают. Каталог транзакции — тем же
  правилом через `SA02M_UPDATE_STATEDIR` (имя раннера).

## GET — состояние (с 1.0.6.52: мёртвый раннер виден)

`GET /cgi-bin/web_update_apply.cgi` отдаёт поля транзакции (`stage`,
`progress_pct`, `files_done`/`files_total`, `error_code`, `error_message`, …),
`status`/`legacy.status` для старых бандлов и **два новых поля, всегда
присутствующих**:

| Поле | Значение |
|---|---|
| `runner_alive` | `true`/`false` — жив ли процесс/юнит раннера; `null`, когда транзакции нет |
| `stale` | `true`, когда одновременно: `stage` ∈ множеству «идёт» (`uploaded … rolling_back`), `runner_alive` = `false` и `updated_at` старше `WEB_UPD_STALE_AFTER_S` = 120 с (или не разбирается). Иначе `false` |

**Тест живости** (одно место — `cgi-bin/lib_web_update.sh`
`web_upd_runner_alive`, читается без привилегий; root-копия того же теста — в
`etc/sa02m-web-update-apply.sh`, потому что root-хелпер не должен source'ить
файл, который может писать `www-data`): pid из
`/var/lib/sa02m-update/update.lock` существует **и** его `cmdline` содержит
`sa02m-update-runner` или `/sa02m-update/runner/` (переиспользованный pid не
считается), **или** жив pid из легаси-лока хелпера
(`/var/lib/sa02m-web-build/update.lock` — фаза клонирования до того, как
раннер взял свой лок), **или** активен `sa02m-update.service` /
`sa02m-update-verify.service`, **или** запущен transient-юнит
`sa02m-update-apply-*.service`. Нет `systemctl` — эта половина `false`.

При `stale = true`: `status`/`legacy.status` = `"error"`; `error_code` =
уже записанный раннером код, иначе `"E_RUNNER_LOST"`; `error_message` = уже
записанное сообщение, иначе `update runner is not running (stage=<stage>, last
update <N>s ago) — reboot the board; verification completes at boot`. CGI
**никогда не пишет** транзакцию — починка только `recover`/`verify` при
загрузке.

Панель (`app/status.js`): `stale` — терминальное состояние (опрос
прекращается), строка «Обновление прервано на этапе «…». Перезагрузите плату —
при загрузке проверка завершится сама», прогресс-бар скрыт; `rolled_back` с
`error_message` показывает «Выполнен откат: <причина>». Старый кэшированный
бандл видит `status:"error"` и останавливается по своему легаси-пути (общая
строка «Ошибка обновления. См. Журнал событий.» — честно, но без причины).

## Жизненный цикл apply — гарантии (с 1.0.6.52)

| Этап | Кто выполняет | Что гарантируется | Чем проверено |
|---|---|---|---|
| Запуск из панели | `web_update_apply.cgi` → `sudo -n sa02m-web-update-apply` из воркера fcgiwrap → `exec` раннера | **G1.** Раннер, запущенный из панели, переживает каждый перезапуск служб, который делает его же health-gate: в самом начале `cmd_apply` он читает `/proc/self/cgroup` и, оказавшись в `fcgiwrap.service` (`KillMode=mixed` — `restart fcgiwrap` убивает всю cgroup SIGKILL'ом), перезапускает себя transient-юнитом `sa02m-update-apply-<txn8>` (`systemd-run`, `KillMode=process`), передаёт лок и выходит. Для доставляющего обновления это происходит в НОВОМ раннере сразу после `exec` старого — до первого записанного файла. Офлайн-путь и запуск по SSH не затрагиваются. Отказ `systemd-run` — «продолжить на месте» (в журнале), не отказ от обновления | `update-cgroup-escape`; на стенде — `systemctl status 'sa02m-update-apply-*'` в состоянии `running` во время `health: restarting fcgiwrap...` |
| Health-gate после deploy | раннер, `health_check` | **G3.** Требуемый юнит считается живым после **двух подряд** `active` в окне `SA02M_UPDATE_HEALTH_SETTLE_SEC` = 30 с (шаг 2 с) — `activating` ждёт, crash-loop (`activating`↔`active`) не проходит на одном сэмпле; `masked`/`disabled`/`ConditionResult=no` — не требуется; при отказе в журнал идут последнее состояние и выдержка `systemctl status`, причина попадает в `error_message` (`E_HEALTH`) | `update-conditional-restart` run 6, `health-gate-operator-disabled` |
| Recover при загрузке | `sa02m-update-recover.service` (`Before=nginx fcgiwrap`) | **G2.** Целое дерево (`files_done == files_total`, `VERSION` = `target_version`) на `verifying`/`committing` **никогда не откатывается по причине порядка загрузки**: recover не перезапускает и не проверяет ничего сам — ставит `boot_verify_pending=true` и запускает `sa02m-update-verify.service` (`--no-block`; fallback — transient-юнит с теми же `After=`); если запустить некого — транзакция остаётся на `verifying` (следующая загрузка повторит, панель покажет `stale`). Неполное дерево откатывается как раньше (`E_POWER` + причина); откат из recover не трогает `nginx`/`fcgiwrap` — systemd поднимет их сам на восстановленном дереве | `update-recover-boot` R1–R3, R5, U1 |
| Проверка после загрузки | `sa02m-update-verify.service` (`After=recover nginx fcgiwrap sa02m-devices-api`, static — без `[Install]`) → `runner verify` | Только `enable[]` + tmpfiles + health-gate, **без** restart-наборов (всё уже стартовало с задеплоенного дерева). Успех → `done`; отказ → откат с `E_HEALTH` и причиной. Идемпотентно: терминальная стадия — no-op, потеря питания посреди verify — повтор при следующей загрузке | `update-recover-boot` R4 |
| Статус для панели | `web_update_apply.cgi` GET | **G4.** Транзакция без живого раннера сообщается как `stale` не позже 120 с после последнего `updated_at` (поля выше); панель останавливает опрос и называет следующий шаг | `web-update-apply-guard` секция G, `test-web-update-semver` раздел F |
| Runtime-watchdog | раннер, `install_imaging_lock`/`cleanup_imaging_lock`/`load_runtime_wdt_prev` | **G5.** Значение `RuntimeWatchdogUSec`, снятое на окно apply, восстанавливается после каждого завершённого apply/verify/rollback — через `exec`, cgroup-escape и перезагрузку: оно хранится в транзакции (`runtime_wdt_prev_usec`; пусто = снимать было нечего); транзакция старого хелпера без поля при живом `0` восстанавливает политику из `sa02m-watchdog.conf`, без политики — ничего не выдумывает и пишет WARN | `update-recover-boot` W1/W2, `watchdog-hold` |
| «Перезагрузка» в панели | `reboot.cgi` | Живой раннер на `applying|verifying|committing|rolling_back` → `{"ok":false,"error_code":"E_UPDATE_RUNNING","error_message":"update in progress (stage=…)"}`, тост «Идёт обновление — перезагрузка отложена»; **`stale`-транзакция остаётся перезагружаемой** — перезагрузка и есть её путь восстановления. CSRF по-прежнему проверяется раньше | `web-update-apply-guard` секция R, `cgi-csrf-policy` |
| Второе «Применить» | `sa02m-web-update-apply` | Шаг 5 выше: живая транзакция не перезаписывается | `web-update-launcher-guard` |

Что остаётся вне гарантий (названо, не скрыто): у доставляющего обновления
(старый раннер 1.0.6.49 на плате) окно clone → prepare → backup (≈1–2 мин)
по-прежнему идёт внутри cgroup fcgiwrap; `systemctl restart fcgiwrap` в это
окно (кнопка «Перезапуск служб», второе OTA) убивает старый раннер на
`validating`/`backing_up` → recover при загрузке чисто откатывает → повтор.
Что из 1.0.6.52 действует уже в доставляющем обновлении — таблица в
`docs/deployment.md` «Пути деплоя».

Схема `transaction.json` **новых стадий не получает** (валидатор
`opt/sa02m-update/lib/transaction.py` отвергает неизвестные стадии и пропускает
неизвестные ключи): добавлены только поля `boot_verify_pending` и
`runtime_wdt_prev_usec`.

## Валидирующие проверки

- `web-update-apply-guard` — `scripts/dev/test-web-update-apply-guard.sh`:
  запускает НАСТОЯЩИЙ CGI (сессия + CSRF из `lib_web_auth.sh`, `sudo`
  подменён на PATH, `check.json` в песочнице) и утверждает таблицу выше по
  телу ответа И по факту вызова `sudo`. RED на 1f6f1a1: отсутствующий
  `check.json` запускал обновление. Секция G (1.0.6.52): `runner_alive`/`stale`
  по живому и мёртвому pid; секция R: `reboot.cgi` против живого/мёртвого
  раннера.
- `test-web-update-semver` — `scripts/dev/test-web-update-semver.mjs`, раздел E:
  фронтенд различает `E_NO_UPDATE` / `E_CHECK_STALE`; раздел F: `stale`
  останавливает опрос, откат показывает причину.
- `web-update-csrf-contract` — шаг 2.
- `web-update-launcher-guard` — шаг 5.
- `update-cgroup-escape`, `update-recover-boot`, `update-conditional-restart`
  (run 6), `health-gate-operator-disabled` — таблица жизненного цикла.
