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
   `docs/decisions/selective-csrf-policy.md`); отсутствует/неверен →
   `{"ok":false,"error":"csrf","error_code":"E_CSRF","reason":"<r>"}`, где
   `reason` ∈ `no_header | mismatch | no_token_file | no_session` (с 1.0.6.53;
   поле аддитивное — старый бандл его игнорирует). Проверка
   `web-update-csrf-contract` пинит эту строку.
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
  песочнице — с 1.0.6.53 там же `csrf_token.cgi`, случаи 11–14).
- Оба кода — **новые ошибки на мутирующем эндпоинте**, фронтенд обрабатывает
  их явно (`app/status.js` `webUpdApplyRefusal`): своя строка статуса, кнопка
  «Применить» остаётся выключенной; в общий путь «Ошибка обновления. См.
  Журнал событий» они не попадают.
- Путь к `check.json` переопределяется только окружением
  `SA02M_WEB_BUILD_STATEDIR` (то же имя, что у `etc/sa02m-update-runner.sh`) —
  для харнесса; nginx/fcgiwrap его не задают. Каталог транзакции — тем же
  правилом через `SA02M_UPDATE_STATEDIR` (имя раннера).

## POST — ответ после запуска (с 1.0.6.53: вердикт из дома живости)

Через 1 с после `sudo -n sa02m-web-update-apply` CGI отвечает по **одному
тесту живости** (`lib_web_update.sh`, тот же, что у GET ниже) — а не по
`kill -0` на дочернем pid, который для setuid `sudo`→root из-под `www-data`
всегда отвечает EPERM («мёртв»):

| Что видно через 1 с | Ответ | Кто это |
|---|---|---|
| `/var/lib/sa02m-web-build/update.lock` существует, **или** его pid жив и `cmdline` содержит `sa02m-web-update-apply` | `{"ok":true,"status":"running","log":"Обновление запущено...","legacy":{"status":"running"}}` | хелпер в фазе клонирования |
| `web_upd_runner_alive` (pid лока раннера + `cmdline`, юниты `sa02m-update*`) | то же `running` | хелпер уже сделал `exec` раннера (быстрый клон — локальный git-сервер, стенд 2026-09-23) |
| `transaction.json` на стадии «идёт» и `updated_at` не старше `WEB_UPD_STALE_AFTER_S` = 120 с | то же `running` | след раннера до его лока (или между `exec` и локом) |
| Ничего из этого | `{"ok":false,"status":"error","log":"<хвост update.log>","legacy":{"status":"error"}}` | хелпер умер, не оставив следа — читайте журнал |

Инварианты:

- **CGI не пишет `update_status`** — файл принадлежит хелперу
  (`etc/sa02m-web-update-apply.sh`); до 1.0.6.53 ложный `error` туда
  записывал CGI, и следующий GET его воспроизводил.
- **Ветка «уже выполняется»** (до запуска) судит pid легаси-лока по
  `cmdline`, а не по `kill -0`: переиспользованный pid не блокирует запуск,
  живой root-хелпер — блокирует.
- Панель (`app/status.js` `applyWebUpdate` → `_webUpdConfirmError`): на
  `status:"error"` **без** `error_code` и на таймаут/обрыв POST делает ОДИН
  GET через 1,5 с — занято → переходит к опросу; иначе — ошибка из ответа.
  Отказы с кодом не перепроверяются: `E_NO_UPDATE` / `E_CHECK_STALE` — свои
  строки (раздел E), `E_CSRF` — строка «Ошибка защиты сессии — повторите
  действие» (тон ошибки, «Применить» по данным последней проверки) **без**
  второго тоста: реакцию на сам отказ — сообщение о прокси, обновление токена
  и повтор, или выход — уже выполнила обёртка `fetch` в `app.js`
  (`docs/decisions/selective-csrf-policy.md` «Реакция панели»); гейт
  `test-web-update-semver` G8.
- Проверено: `web-update-apply-guard` секция H (H1/H1b/H2/H3/H3b — быстрая
  передача с живым раннером / со свежей транзакцией, пустой след без записи
  `update_status`, живой хелпер без второго запуска, переиспользованный pid
  запускается; RED на 1.0.6.52: H1, H1b, H2, H3b), `test-web-update-semver`
  раздел G (панель: одна перепроверка перед ошибкой; RED на 1.0.6.52).

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
файл, который может писать `www-data`). Основной детектор — **pid лока**:
каждая точка входа раннера берёт лок первой, и pid из
`/var/lib/sa02m-update/update.lock` считается живым, когда процесс существует
**и** его `cmdline` содержит `sa02m-update-runner` или `/sa02m-update/runner/`
(переиспользованный pid не считается). Дополнительно — на моменты до лока:
жив pid из легаси-лока хелпера (`/var/lib/sa02m-web-build/update.lock` — фаза
клонирования), **или** `sa02m-update.service` / `sa02m-update-verify.service`
имеет `ActiveState` ∈ `active|activating|reloading` (оба — `Type=oneshot`, у
которых `activating` и есть состояние «выполняется»; `is-active` для них
всегда отвечал бы «нет»), **или** запущен transient-юнит
`sa02m-update-apply-*.service`. Нет `systemctl` — эта половина `false`.

При `stale = true`: `status`/`legacy.status` = `"error"`; `error_code` =
уже записанный раннером код, иначе `"E_RUNNER_LOST"`; `error_message` = уже
записанное сообщение, иначе `update runner is not running (stage=<stage>, last
update <N>s ago) — reboot the board; verification completes at boot`; поле
`log` начинается с человеческой строки «Обновление прервано на этапе
«<стадия>»: перезагрузите плату — при загрузке проверка завершится сама.» —
единственный канал к СТАРОМУ кэшированному бандлу, который `stale` не читает,
а `log` выводит в журнал событий. CGI **никогда не пишет** транзакцию —
починка только `recover`/`verify` при загрузке (или `runner reclaim`).

Панель (`app/status.js`): `stale` — терминальное состояние (опрос
прекращается), строка «Обновление прервано на этапе «…». Перезагрузите плату —
при загрузке проверка завершится сама», прогресс-бар скрыт; `rolled_back` с
`error_message` показывает «Выполнен откат: <причина>». Старый кэшированный
бандл видит `status:"error"` и останавливается по своему легаси-пути (общая
строка «Ошибка обновления. См. Журнал событий.» — честно, но без причины).

## Жизненный цикл apply — гарантии (с 1.0.6.52)

| Этап | Кто выполняет | Что гарантируется | Чем проверено |
|---|---|---|---|
| Запуск из панели | `web_update_apply.cgi` → `sudo -n sa02m-web-update-apply` из воркера fcgiwrap → `exec` раннера | **G1.** Раннер, запущенный из панели, переживает каждый перезапуск служб, который делает его же health-gate: в самом начале `cmd_apply` он читает `/proc/self/cgroup` и, оказавшись в `fcgiwrap.service` (`KillMode=mixed` — `restart fcgiwrap` убивает всю cgroup SIGKILL'ом), перезапускает себя transient-юнитом `sa02m-update-apply-<txn8>` (`systemd-run`, `KillMode=process`), передаёт лок и выходит. **Не действует в обновлении, которое доставляет этот код** на плату ≤ 1.0.6.51: старый раннер `exec`'ит копию самого себя, и весь тот apply идёт под старым кодом (замирает на 85 %, завершается при загрузке — `docs/deployment.md`). Офлайн-путь и запуск по SSH не затрагиваются. Отказ `systemd-run` — «продолжить на месте» (в журнале), не отказ от обновления | `update-cgroup-escape`; на стенде — `systemctl status 'sa02m-update-apply-*'` в состоянии `running` во время `health: restarting fcgiwrap...` |
| Health-gate после deploy | раннер, `health_check` | **G3.** Требуемый юнит считается живым после **двух подряд** `active` в окне `SA02M_UPDATE_HEALTH_SETTLE_SEC` = 30 с (шаг 2 с) — `activating` ждёт, crash-loop (`activating`↔`active`) не проходит на одном сэмпле; `masked`/`disabled`, а также `ConditionResult=no` **при `ConditionTimestampMonotonic != 0`** (условие реально проверялось в эту загрузку; для юнита, старт которого не пытались выполнить, systemd тоже отвечает `no`, и такой enabled-юнит — регрессия, а не выбор оператора) — не требуется; при отказе в журнал идут последнее состояние и выдержка `systemctl status`, причина попадает в `error_message` (`E_HEALTH`). **Не прочитанный манифест — отказ, а не «нечего проверять»** (с 1.0.6.54): если чтение из манифеста списков `units_active`/`http_url`/`version_file`/`version`, наборов перезапуска (`restart`, `restart_if_active`, `restart_if_changed`) или `enable[]` завершилось ошибкой, шаг проваливается с `error_message` = `manifest read failed: …` (`E_HEALTH`, откат) и ничего не перезапускает; ключ, **отсутствующий** в манифесте (пустой `http_url`, нет `units_active`), по-прежнему означает «проба не настроена». Ошибка чтения `daemon_reload` — `daemon-reload` выполняется (безопасная сторона) | `update-conditional-restart` run 6, run 7; `health-gate-operator-disabled` |
| Recover при загрузке | `sa02m-update-recover.service` (`Before=nginx fcgiwrap`) | **G2.** Целое дерево (`files_done == files_total`, `VERSION` = `target_version`) на `verifying`/`committing` **никогда не откатывается по причине порядка загрузки**: recover не перезапускает и не проверяет ничего сам — ставит `boot_verify_pending=true` и запускает `sa02m-update-verify.service` (`--no-block`; fallback — transient-юнит с теми же `After=`); если запустить некого — транзакция остаётся на `verifying` (следующая загрузка повторит, панель покажет `stale`). Неполное дерево (и `applying`/`rolling_back`) откатывается как раньше (`E_POWER` + причина), но в контексте загрузки откат **ничего не перезапускает** — только `daemon-reload`: все юниты стартуют с восстановленного дерева, как только recover выйдет (набор перезапусков здесь загнал recover в его же `TimeoutStartSec=300` на стенде 1.135 — откат остался на `rolling_back` с локом и сторожем 0). Плата в таком остатке сходится за одну загрузку: журнал воспроизводится повторно, лок снимается, сторож восстанавливается по политике. SIGTERM от таймаута превращается в выход с EXIT-trap: лок остаётся для следующего recover, но сторож возвращается сразу | `update-recover-boot` R1–R3, R5–R7, U1 |
| Остаток без перезагрузки | `runner reclaim` (через `scripts/sa02m-update-remedy.sh`) | Тот же recover во время работы для транзакции, чей раннер исчез: при живом раннере (лок занят) — ничего не делает и выходит с кодом 3 (скрипт на нём останавливается); `rolling_back`/`applying` — откат дозавершается с перезапусками (веб поднят); целое `verifying`/`committing` — **сначала наборы перезапусков, до которых погибший apply не дошёл** (fcgiwrap, `nginx -t` + reload, `restart[]`/`restart_if_*`; отказ `nginx -t` — откат с `E_HEALTH`), затем `sa02m-update-verify.service`, который запускается сразу и делает enable+tmpfiles+health-gate; терминальная стадия с оставшимся локом — лок снимается, сторож восстанавливается. При загрузке (recover) наборы перезапусков не выполняются никогда. На плате `reclaim` ещё не выполнялся — гарантия харнесса. Рецепт — `docs/deployment.md` «Ремонт застрявшего обновления без перезагрузки». Что не делается само: без перезагрузки и без вызова `reclaim`/нового apply остаток лежит — периодического вызова нет (решение о таймере — отдельное) | `update-recover-boot` R8a–e, M |
| Проверка после загрузки | `sa02m-update-verify.service` (`After=recover nginx fcgiwrap sa02m-devices-api`, static — без `[Install]`) → `runner verify` | Только `enable[]` + tmpfiles + health-gate, **без** restart-наборов (всё уже стартовало с задеплоенного дерева). Успех → `done`; отказ (включая отказ шага `enable[]` — непрочитанный манифест, с 1.0.6.54) → откат с `E_HEALTH` и причиной, verify не обрывается на середине. Идемпотентно: терминальная стадия — no-op, потеря питания посреди verify — повтор при следующей загрузке | `update-recover-boot` R4, R4d |
| Статус для панели | `web_update_apply.cgi` GET | **G4.** Транзакция без живого раннера сообщается как `stale` не позже 120 с после последнего `updated_at` (поля выше); панель останавливает опрос и называет следующий шаг | `web-update-apply-guard` секция G, `test-web-update-semver` раздел F |
| Runtime-watchdog | раннер, `install_imaging_lock`/`cleanup_imaging_lock`/`load_runtime_wdt_prev` | **G5.** Значение `RuntimeWatchdogUSec`, снятое на окно apply, восстанавливается после каждого завершённого apply/verify/rollback — через `exec`, cgroup-escape и перезагрузку: оно хранится в транзакции (`runtime_wdt_prev_usec`; пусто = снимать было нечего); транзакция старого хелпера без поля при живом `0` восстанавливает политику из `sa02m-watchdog.conf`, без политики — ничего не выдумывает и пишет WARN; новый hold над менеджером, уже стоящим на 0 под политикой (остаток убитого recover), запоминает значение политики, а не 0 | `update-recover-boot` W1–W3, `watchdog-hold` |
| «Перезагрузка» в панели | `reboot.cgi` | Живой раннер на `applying|verifying|committing|rolling_back` → `{"ok":false,"error_code":"E_UPDATE_RUNNING","error_message":"update in progress (stage=…)"}`, тост «Идёт обновление — перезагрузка отложена»; **`stale`-транзакция остаётся перезагружаемой** — перезагрузка и есть её путь восстановления. CSRF по-прежнему проверяется раньше | `web-update-apply-guard` секция R, `cgi-csrf-policy` |
| Второе «Применить» | `sa02m-web-update-apply` | Шаг 5 выше: живая транзакция не перезаписывается | `web-update-launcher-guard` |
| Раскладка файла (с 1.0.6.54) | раннер, `journal_append` → `atomic_install_file` | **G6.** Строка журнала, описывающая файл (`op`, `dst`, `backup`, `mode`, `owner`), лежит на диске (`fdatasync`) **до** переименования, которое она описывает; порядок на каждый изменённый файл: `fdatasync(journal)` → `fdatasync(tmp)` → `rename` → `fsync(dir)`. Обрыв питания в любой точке оставляет старый или новый файл (никогда усечённый) и журнал, который его уже знает, — откат по журналу не оставит НОВЫЙ файл, о котором журнал молчит. Каждый шаг проверяется, и провал любого из них — отказ всего apply (`E_APPLY`, откат) **до** переименования, а не «файл засчитан»: копия для отката (`backup`), запись строки журнала, её `fdatasync`, `install`, `fdatasync(tmp)`, `mv`, `fsync(dir)` — без отката на полный `sync`, который об ошибке сообщить не может (провал `fsync(dir)` случается уже после переименования; строка журнала файл уже описывает, откат его восстанавливает). То же для удаления по манифесту (`delete[]`: копия → строка журнала → удаление) и для отметки версии раннера. Если не прочитан список `stop_before_apply`, apply останавливается (`E_APPLY`) **до** раскладки — `sa02m-flasher` не остаётся держать порты RS-485 во время обновления | `update-deploy-skip` случаи 6 (порядок), 7 (экранирование `dst`), 9a/9b (отказ install/mv), 11a–11d (отказ копии / записи журнала / его fdatasync / журнала отметки), 12a–12d (удаление), 13a/13b (отказ fdatasync(tmp) / fsync(dir)), 14a/14b (`stop_before_apply`) |

Сбой чтения манифеста там, где раннер работает с `errexit` (версия и коммит
цели сразу после распаковки пакета — стадия `validating`; `commit_markers` —
стадия `committing`), не маскируется, а **обрывает раннер** с ненулевым кодом:
`on_exit` возвращает сторож, транзакция остаётся на своей стадии, панель через
120 с показывает `stale`, `recover` при загрузке доводит её — `validating`
заканчивается `E_POWER` без изменения файлов, целое дерево на `committing`
уходит в `sa02m-update-verify`. Это поведение задумано, а не случайно (с 1.0.6.54).

Что остаётся вне гарантий у доставляющего обновления (старый раннер на плате
— окно до выхода из cgroup) и что из 1.0.6.52 действует уже в нём — одно
место: `docs/deployment.md` «Пути деплоя», «Что из 1.0.6.52 действует уже в
доставляющем OTA».

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
