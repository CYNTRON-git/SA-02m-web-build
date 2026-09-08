# Контракт: `POST /cgi-bin/web_update_apply.cgi` — запуск интернет-обновления веб

Домашний адрес контракта эндпоинта, который запускает root-хелпер
`sa02m-web-update-apply` (OTA с GitHub). Здесь — ТОЛЬКО ветка «POST без
`confirm_version`» (кнопка «Применить» в «Обновление веб»); офлайн-транзакция
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
  (`POST web_update_check.cgi`) пересобирает файл; после неё Apply снова
  доступен, если есть что применять.
- Оба кода — **новые ошибки на мутирующем эндпоинте**, фронтенд обрабатывает
  их явно (`app/status.js` `webUpdApplyRefusal`): своя строка статуса, кнопка
  «Применить» остаётся выключенной; в общий путь «Ошибка обновления. См.
  Журнал событий» они не попадают.
- Путь к `check.json` переопределяется только окружением
  `SA02M_WEB_BUILD_STATEDIR` (то же имя, что у `etc/sa02m-update-runner.sh`) —
  для харнесса; nginx/fcgiwrap его не задают.

## Валидирующие проверки

- `web-update-apply-guard` — `scripts/dev/test-web-update-apply-guard.sh`:
  запускает НАСТОЯЩИЙ CGI (сессия + CSRF из `lib_web_auth.sh`, `sudo`
  подменён на PATH, `check.json` в песочнице) и утверждает таблицу выше по
  телу ответа И по факту вызова `sudo`. RED на 1f6f1a1: отсутствующий
  `check.json` запускал обновление.
- `test-web-update-semver` — `scripts/dev/test-web-update-semver.mjs`, раздел E:
  фронтенд различает `E_NO_UPDATE` / `E_CHECK_STALE`.
- `web-update-csrf-contract` — шаг 2.
