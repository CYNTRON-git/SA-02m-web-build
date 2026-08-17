# Контракт: `mplc_project_deploy.cgi` — загрузка и развёртывание проекта MPLC4

Домашний адрес контракта веб-эндпоинта, который принимает экспорт проекта
MasterSCADA4D (`.zip`) и разворачивает его на работающий MPLC4 RT: остановка →
резервная копия → замена → запуск → проверка загрузки, с автоматическим откатом
при любой ошибке. Машинная грамматика (поля JSON, коды ошибок, пути, имена
файлов) — на английском (`PROTOCOL.md` invariant 5); пояснения — на русском.

Файлы: CGI `www/network_config/cgi-bin/mplc_project_deploy.cgi`; привилегированный
помощник `etc/sa02m-mplc-project-deploy.sh` (→ `/usr/local/sbin/`); логика разбора/
валидации `opt/sa02m-mplc/lib/project_zip.py`; UI-блок «Обновление проекта MPLC» в
`index.html` + `static/js/app/status.js`. Провижининг — `scripts/03-webserver.sh`.

## Что грузится

Экспорт IDE MasterSCADA4D — `.zip`, содержащий **ровно** папку `cfg/` с четырьмя
файлами (подтверждено на стенде, 1.0.5.69):

```
cfg/config.bin      cfg/ProjInfo.json      cfg/VMInfo.json      cfg/_files.xml
```

Цель развёртывания — **жёстко зашитый** путь `/opt/mplc4/server/cfg/`
(WorkDirectory RT = `/opt/mplc4/server`, RT грузит `cfg/config.bin`). Путь никогда
не берётся из запроса или из имён записей в zip.

## Запрос

- `POST`, `multipart/form-data`, поле `file` — сам `.zip`. Cookie `session_token`
  обязателен; заголовок `X-SA02M-CSRF` обязателен (проверяются **до** любой
  мутации). Тело ограничено 5 МБ (реальный экспорт ~14 КБ).
- `GET` (без query) — метаданные текущего развёрнутого проекта (читается
  `cfg/ProjInfo.json`): `{"ok":true,"deployed":true|false,"project":{name,id,ide_version}}`.
  Дополнительно возвращает `license` — лицензию рантайма MPLC4 (см. ниже).
- `GET ?result=1` — статус асинхронного развёртывания (JSON статус-файла;
  `pending` пока помощник не запишет терминальный результат).

## Лицензия рантайма MPLC4 (поле `license` в GET-метаданных)

Рантайм MPLC4 при каждом старте пишет свою лицензию в новейший
`/var/log/mplc4/0/<YYYY_MM_DD>.txt` — блок `<Protect>`. GET-эндпоинт разбирает
**последний** такой блок (последний старт) новейшего лога и возвращает:

```json
"license":{"activated":true,"points":<N>,"clients":<M>}   // активирована
"license":{"activated":false}                             // демо (не активирована)
"license":{"activated":false,"unknown":true}              // лог недоступен/не разобран
```

- `InstancesLimit=<N>` → **points** (точки); `SessionsLimit=<M>` → **clients**
  (клиенты) — English-грамматика полей JSON; UI показывает «Точки»/«Клиенты».
- Строка `Not activated` в последнем блоке ⇒ `activated:false`: числовые лимиты
  тогда демо-умолчания, они НЕ возвращаются (UI: «не активирована»).
- **Привилегии / fail-safe** — CGI работает как `www-data`; лог-каталог
  world-readable (`drwxrwxrwx`, файлы `-rw-rw-rw-`), читается напрямую без sudo.
  Чтение — потоковый проход ВПЕРЁД (блок `<Protect>` пишется при старте и тонет
  под тысячами heartbeat-строк, поэтому read хвоста его бы пропустил), ограничено
  64 МиБ (poll-path hygiene). ЛЮБАЯ ошибка чтения/
  разбора → `license:{activated:false,unknown:true}`; GET никогда не падает.

## Валидация загрузки (attack surface — недоверенный multipart на LAN)

Порядок, всё **fail-closed** (`opt/sa02m-mplc/lib/project_zip.py`, покрыто
`opt/sa02m-mplc/tests/test_project_zip.py`):

1. **Cap тела** — `CONTENT_LENGTH` > 5 МБ отклоняется до чтения; Python-чтение
   stdin ограничено тем же пределом.
2. **Anti zip-bomb** — cap количества записей (≤ 32), затем cap размера каждой
   записи (≤ 8 МБ, по `ZipInfo.file_size` до распаковки) и суммарного (≤ 24 МБ).
3. **Zip-slip / traversal** — каждое имя записи проходит проверку: отказ при `..`,
   абсолютном пути, ведущем `/` или `~`, обратном слэше, двоеточии, и при записи-
   симлинке (биты режима в `external_attr`). Допускается **только** закрытый
   allow-list из 4 имён `cfg/…`; любой другой файл-член → отказ.
4. **Реальный экспорт MasterSCADA** — должны присутствовать ровно эти 4 файла;
   `ProjInfo.json` парсится как JSON и содержит `ProjectId` и `ProjectName`.
5. **Хэши `_files.xml` — по умолчанию ВЫКЛ.** Реестр IDE хранит хэш **не** как
   plain SHA-256 упакованных байтов (эмпирически не совпадает с реальным
   экспортом — IDE хэширует контент до экспорта), поэтому reject-on-mismatch
   отбраковал бы легитимную загрузку. Проверка реализована (`verify_file_hashes`,
   флаг `verify_hashes`), но включается только после подтверждения алгоритма на
   устройстве. Основную защиту дают пункты 1–4, не хэш.
6. **Распаковка** — извлекаются только 4 allow-list-члена в **фиксированные**
   базовые имена в жёстко зашитой цели; имя записи zip не участвует в пути.

Извлечение zip выполняется в Python (`zipfile`), не в shell; ни одно значение
запроса не попадает в shell-слово; `mplc4` — литерал, не из запроса.

## Развёртывание (state machine помощника, каждый шаг под таймаутом)

`validate → stopping → backup → replace → starting → verify`. Инварианты:

- **Единственный in-flight** — помощник держит `flock` на
  `/var/lib/sa02m-mplc/incoming/.deploy.lock` весь прогон; второй запуск выходит.
  CGI отклоняет POST при занятом lock (`E_LOCK`) и при занятом флэшере (`E_BUSY`).
- **Порт-лиз RS-485** — отказ, если флэшер держит лиз COM (шина RS-485 общая);
  запуск `mplc4` идёт **только** через существующий
  `sa02m-web-service-ctl.sh start mplc4`, который сам уважает лиз. Помощник не
  переизобретает управление службой.
- **Fail-closed удаление** — существующий проект удаляется **только** после
  успешной резервной копии (`cfg-<ts>.tgz`), либо когда его нет (первое
  развёртывание).
- **Откат при любой ошибке** replace/start/verify — восстановление из копии и
  перезапуск `mplc4`; первое развёртывание без копии → удаление недописанного
  `cfg/` и перезапуск.
- **verify-load — сигнал успеха.** SUCCESS только при `Configuration was load
  successful` в мониторном логе RT (`/var/log/mplc4/monitor/mplc_monitor.log`),
  читается **только новый** хвост лога (offset снят до старта). Голый старт с
  `LoadConfig() error` / `Loading default configuration` — это FAILURE, а не
  ложный green.
- **Retention** — хранится не более 5 копий `cfg-*.tgz`, старые удаляются.
- **Привилегии** — CGI работает как `www-data`; вся root-работа — один
  закреплённый `sudo -n /usr/local/sbin/sa02m-mplc-project-deploy.sh *` (не ad-hoc
  sudo отдельных команд).

## Ответы (всегда HTTP 200, JSON)

```json
{"ok":true,"pending":true,"stage":"validate"}                       // POST принят
{"ok":true,"pending":false,"stage":"done","result":"success","project":{"name":"…","id":"…","ide_version":"…"}}
{"ok":false,"error":"unauthorized"}
{"ok":false,"error":"csrf","error_code":"E_CSRF"}
{"ok":false,"error_code":"E_SIZE","error_message":"body too large"}
{"ok":false,"error_code":"E_TRAVERSAL","error_message":"…"}          // zip-slip
{"ok":false,"error_code":"E_MEMBERS","error_message":"…"}            // не экспорт
{"ok":false,"error":"flasher_busy","error_code":"E_BUSY"}
{"ok":false,"error":"busy","error_code":"E_LOCK"}
{"ok":false,"pending":false,"stage":"verify","result":"error","error_code":"E_VERIFY","error_message":"…"}
```

Коды стадий/ошибок (English enum): stage `validate|stopping|backup|replace|
starting|verify|done`; error_code `E_UPLOAD|E_SIZE|E_ZIP|E_TRAVERSAL|E_MEMBERS|
E_PROJINFO|E_HASH|E_BUSY|E_LOCK|E_STOP|E_BACKUP|E_EXTRACT|E_START|E_VERIFY|E_CMD|
E_CSRF|E_INTERNAL`. UI мапит error_code → русское сообщение.

## Валидирующие проверки (рецепты)

1. **`py-unit-mplc`** (`opt/sa02m-mplc/tests/test_project_zip.py`) — функциональный
   net атак-поверхности: zip-slip (`../../etc/cron.d/evil`, `/etc/passwd`),
   симлинк-член, лишний/отсутствующий член, битый `ProjInfo.json`, cap размера
   записи, извлечение только в фиксированные имена, binary-safe разбор multipart.
2. **`mplc-project-deploy-contract`**
   (`.ai-dev/quality/checks/mplc-project-deploy-contract.sh`) — статический шлюз:
   auth+CSRF до мутации в CGI, жёстко зашитый `MPLC_CFG_DIR`, один закреплённый
   sudoers-путь, verify по `load successful` в помощнике, retention-cap,
   таймауты, отсутствие request-значения в shell-слове.
