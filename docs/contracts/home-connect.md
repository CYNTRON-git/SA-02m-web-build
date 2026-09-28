# Контракт: клиент BSH Home Connect (`sa02m-homeconnect`)

**Гарантия:** бытовые приборы Bosch/Siemens/Neff/Gaggenau, привязанные к
аккаунту Home Connect интегратора, появляются на локальном брокере как обычные
устройства WB `/devices/hc-<id>/controls/<name>` — **только для чтения**;
устройство, о котором облако сообщило «отключено», или все устройства, пока
поток событий облака недоступен дольше 2 минут, несут
`/devices/hc-<id>/meta/error = "r"`; значение, которого облако не прислало,
не публикуется никогда; OAuth-токены читает только собственный
непривилегированный пользователь демона; суточный лимит BSH (1000 вызовов,
включая неудачные) не превышается.

Клиент — необязательная установка. Решения Оператора: Q-B — интегратор вводит
Client ID **своего** приложения Home Connect (слот под будущий ID CYNTRON
оставлен в конфиге, §8); Q-E — **первый выпуск только читает**; Q-F — factory
reset стирает вход в Home Connect; Q-G — модуль строится сейчас. Почему
Device Flow и какие факты о BSH проверены, а какие нет —
`docs/decisions/homekit-home-connect.md` (G6, G7) и §14 ниже.

Машинная грамматика (пути, ключи JSON, топики, состояния, коды ошибок) — на
английском (`PROTOCOL.md` invariant 5); пояснения — на русском. Код:
`opt/sa02m-homeconnect/sa02m_homeconnect/`; каждое перечисление ниже
существует в коде ровно один раз (модуль назван у раздела), и
`test_contract_tables.py` сравнивает их с этим файлом как множества.

---

## 0. Обещания

| # | Обещание | Где держится |
|---|---|---|
| P2 | **Честный статус.** Прибор, о котором облако сообщило `DISCONNECTED`, и все приборы при потоке событий, лежащем дольше `STREAM_STALE_S` = 120 с, несут `meta/error = "r"`. Ключ или значение вне закрытой таблицы §4 не публикуется: контрол не выдумывается | §4, §5 |
| P4 | **Секреты там, где нужны.** `access_token`/`refresh_token` читает только пользователь `sa02m-homeconnect` (файл 0600, проверка владельца и режима при чтении); их нет в журнале, в `/run`, в ответах CGI, в веб-бэкапе, в клоне образа | §8, §11, §12 |
| P5 | **Приборы — обычные устройства MQTT** в конвенции WB: прибор привязывается в «Умном доме» как канал шины, и Алиса, сценарии и HomeKit потребляют его через один документ устройств, без плагина под одну экосистему; привязка остаётся только для чтения у всех потребителей | §3, §4, §6 |
| RO | **Только чтение.** Ни одной команды прибору: REST-клиент умеет только `GET`, запрашиваемые права — `IdentifyAppliance Monitor` (без `Control` и `Settings`), на MQTT клиент ни на что не подписан, топиков `/on` нет | §2, §3, §13 |

## 1. Зависимости и границы

- **Только стандартная библиотека Python** (`urllib`, `ssl`, `threading`,
  `json`) плюс apt `python3-paho-mqtt`. Никакого pip — поэтому пакет целиком
  доставляется OTA.
- Демон ничего не импортирует из `sa02m_alice`/`sa02m_homekit`, и они не
  импортируют его. Потребители видят приборы только через брокер; пикер
  «Умного дома» (`sa02m_alice/config/inventory.py`) читает `inventory.json`,
  чтобы прибор можно было привязать как обычное устройство (§6).
- Диспетчер CGI (`sa02m_homeconnect.api`) — та же стандартная библиотека под
  системным `python3`; paho ему не нужен.
- Демон открывает соединения только к хостам из `constants.HOSTS` (https,
  проверка TLS по системным корням); прокси из окружения игнорируются,
  перенаправления (3xx) — ошибка, а не переход: иначе заголовок `Authorization`
  ушёл бы на хост, названный ответом.

## 2. Облако BSH

| Что | Значение (`constants.py`) |
|---|---|
| Хосты | `api` → `https://api.home-connect.com`, `simulator` → `https://simulator.home-connect.com`. Конфиг называет **ключ**, URL берётся только из таблицы |
| Вход | `POST /security/oauth/device_authorization` (`client_id`, `scope`), `POST /security/oauth/token` (`grant_type` = `urn:ietf:params:oauth:grant-type:device_code` или `refresh_token`, всегда с `client_id`, без секрета) |
| Чтение | `GET /api/homeappliances`, `GET /api/homeappliances/{haId}/status`, `GET /api/homeappliances/{haId}/programs/active` (только когда `OperationState` ∈ `Run`, `DelayedStart`, `Pause`, `ActionRequired` — иначе заведомый 404 стоил бы вызов) |
| Поток событий | `GET /api/homeappliances/events`, `Accept: text/event-stream` |
| Права | `IdentifyAppliance Monitor` |
| Заголовки REST | `Accept: application/vnd.bsh.sdk.v1+json`, `Accept-Language: en-GB` |

`GET /settings` не вызывается: без права `Settings` это был бы заведомо
отказной вызов. Поэтому `power_on` публикуется, только если облако само
присылает `BSH.Common.Setting.PowerState` в потоке событий или в `status` (§14).

## 3. MQTT-топики

Брокер `127.0.0.1:1883`, QoS 1, **всё retained** (конвенция
`docs/MQTT_TOPICS.md`). Идентификатор устройства (`mapping.device_id`):
`hc-` + `haId` в нижнем регистре, всё вне `[a-z0-9-]` → `-`, всего ≤ 64
символа. Ни одна строка облака не становится сегментом топика без этой
очистки; два `haId`, давшие один идентификатор, — второй пропускается с записью
в журнал. Не больше `MAX_APPLIANCES` = 32 приборов.

| Топик | Содержимое |
|---|---|
| `/devices/hc-<id>/meta` | `{"driver":"sa02m-homeconnect","title":{"en":<имя>,"ru":<имя>},"type":<тип прибора>}` (`type` — только если тип прошёл `^[A-Za-z0-9]{1,32}$`) |
| `/devices/hc-<id>/meta/name`, `/meta/driver` | имя прибора; `sa02m-homeconnect` |
| `/devices/hc-<id>/meta/error` | `"r"` — прибор недоступен или данные могут быть устаревшими (§5); `""` — живой |
| `/devices/hc-<id>/controls/<name>` | значение (§4) |
| `/devices/hc-<id>/controls/<name>/meta` | `{"type":…,"readonly":true,"order":N,"title":{"en":<name>},"units":…}` |
| `/devices/hc-<id>/controls/<name>/meta/type`, `/meta/readonly` (`1`), `/meta/order`, `/meta/units` | субтопики той же меты |

Имя прибора (`mapping.clean_name`): управляющие и форматирующие символы
Юникода удалены, пробелы схлопнуты, ≤ 64 символа; пустое → идентификатор
устройства. Имя — полезная нагрузка, не топик.

При каждом (пере)подключении к брокеру клиент заново публикует всё своё
retained-состояние (брокер без persistence его потерял бы). Удаление устройства
(прибор отвязан от аккаунта, вход удалён, модуль выключен) — пустой retained
payload в каждый топик закрытого набора этого устройства.

## 4. Таблица контролов

Единственный дом таблицы в коде — `mapping.MAPPING`. Все контролы только для
чтения.

| control | WB type | units | Источник | Правило значения |
|---|---|---|---|---|
| `connected` | `switch` | — | `connected` в списке приборов; события `CONNECTED`/`DISCONNECTED` | JSON `true` → `1`, `false` → `0` |
| `power_on` | `switch` | — | `BSH.Common.Setting.PowerState` | `On` → `1`; `Off`, `Standby`, `MainsOff` → `0`; иное — не публикуется |
| `door_open` | `switch` | — | `BSH.Common.Status.DoorState` | `Open` → `1`; `Closed`, `Locked` → `0`; иное — не публикуется |
| `running` | `switch` | — | `BSH.Common.Status.OperationState` | `Run` → `1`, любое другое значение перечисления → `0` |
| `finished` | `switch` | — | `BSH.Common.Status.OperationState` | `Finished` → `1`, любое другое → `0` |
| `remote_start_allowed` | `switch` | — | `BSH.Common.Status.RemoteControlStartAllowed` | JSON bool → `1`/`0` |
| `remote_control_active` | `switch` | — | `BSH.Common.Status.RemoteControlActive` | JSON bool → `1`/`0` |
| `local_control_active` | `switch` | — | `BSH.Common.Status.LocalControlActive` | JSON bool → `1`/`0` |
| `operation_state` | `text` | — | `BSH.Common.Status.OperationState` | последний сегмент (`Ready`, `Run`, `Finished`, …) |
| `active_program` | `text` | — | `BSH.Common.Root.ActiveProgram`; ответ `programs/active` | последний сегмент ключа программы (`Eco50`); `null`/нет программы → `""` |
| `remaining_s` | `value` | `s` | `BSH.Common.Option.RemainingProgramTime` | целое 0…604800 |
| `progress_pct` | `value` | `%` | `BSH.Common.Option.ProgramProgress` | целое 0…100 |
| `last_event` | `text` | — | `BSH.Common.Event.ProgramFinished`, `…ProgramAborted`, `…AlarmClockElapsed` | последний сегмент ключа события, когда значение `…EventPresentState.Present` или отсутствует; `Off`/`Confirmed` — не событие |
| `last_event_ts` | `value` | — | метка времени того же события | секунды эпохи (из `timestamp` события, иначе время приёма) |

Правила типов: bool — только JSON `true`/`false` (строка `"true"` отклоняется);
числа — конечные, в диапазоне, округляются до целого; значения перечислений —
только грамматика `BSH.Common.EnumType.<Тип>.<Значение>` с последним сегментом
`[A-Za-z0-9_]{1,64}`. Отклонённое значение не публикует ничего.

**Какие контролы появятся у прибора, решает сам прибор**: публикуется только
то, что облако реально прислало. Ожидания по типам ниже выведены из общих
ключей BSH и **на приборах не проверены** (§14):

| Тип прибора | Ожидаемые контролы помимо `connected` |
|---|---|
| `Dishwasher`, `Washer`, `Dryer`, `WasherDryer` | `door_open`, `operation_state`, `running`, `finished`, `active_program`, `remaining_s`, `progress_pct`, `remote_start_allowed`, `remote_control_active`, `last_event` |
| `Oven` | то же плюс `last_event` = `AlarmClockElapsed` |
| `CoffeeMaker`, `Hood` | `operation_state`, `running`, `finished`, `active_program`, `local_control_active` |
| `Hob` | `operation_state`, `local_control_active`, `last_event` |
| `FridgeFreezer`, `Refrigerator`, `Freezer` | `door_open` |
| любой | `power_on` — только если `PowerState` приходит при праве `Monitor` |

## 5. Доступность

- `meta/error = "r"` прибора, пока верно хоть одно: облако сообщает прибор
  отключённым; его значения ещё не перечитаны после старта демона или после
  долгого обрыва потока; поток событий лежит дольше `STREAM_STALE_S` = 120 с.
  Иначе `""`. Значения при `"r"` сохраняются (retained) — видно последнее
  известное, помеченное как недостоверное.
- **На старте** демон ставит `"r"` всем приборам прошлого запуска
  (`appliances.json`): их retained-значения неизвестного возраста, пока не
  перечитаны.
- **При остановке** со включённым модулем — `"r"` всем (облако никто не
  слушает); при выключении модуля или удалении входа — топики удаляются (§3).
- Поток восстановился после обрыва дольше 120 с → список приборов и каждый
  подключённый прибор перечитываются (с шагом `INVENTORY_PACE_S` = 15 с между
  приборами, в рамках бюджета §7). Короткий обрыв — без перечитывания.
- `CONNECTED` прибора → `connected = 1` и перечитывание этого прибора;
  `DISCONNECTED` → `connected = 0`, `"r"`; `PAIRED` → перечитать список;
  `DEPAIRED` → топики прибора удаляются.
- **Повтор значений, пока поток жив.** Пока поток событий жив (открыт, не
  устарел и за последние `main.STREAM_QUIET_MAX_S` = 70 с пришло хоть что-то,
  включая keep-alive), клиент раз в `main.VALUE_HEARTBEAT_S` = 60 с заново
  публикует кешированные значения контролов приборов с `meta/error = ""` (без
  меты, без вызовов облака — бюджет не тратится). Поток лёг или молчит —
  повторов нет, и потребители, считающие значение устаревшим через 90 с
  (Алиса), честно помечают его устаревшим.
- **Остаточный риск, названный:** при аварийном падении процесса, которое
  systemd не перезапустит, retained-значения остаются без `"r"` — MQTT даёт
  один will на соединение, а приборов много. Перезапуск (`Restart=on-failure`)
  снимает риск: первое, что делает новый процесс, — ставит `"r"` всем.

## 6. Статус

Три файла в `/run/sa02m-homeconnect/` (единственный писатель — главный поток
демона; `status.py`).

**Состояния** (`constants.STATES`):

| Состояние | Значение |
|---|---|
| `not_installed` | нет установочного следа (юнит `/etc/systemd/system/sa02m-homeconnect.service`, пакет `/opt/sa02m-homeconnect/sa02m_homeconnect`, системный пользователь `sa02m-homeconnect`). Демон его не пишет — выводит диспетчер (плата получила пакет только по OTA) |
| `disabled` | в конфиге `enabled = false`; демон пишет статус и выходит с кодом 0 |
| `missing_deps` | не импортируется `paho.mqtt.client` (apt `python3-paho-mqtt`); выход 0, `message` называет модуль |
| `missing_client_id` | нет ни своего Client ID, ни предустановленного; в облако ничего не уходит |
| `unlinked` | вход не выполнен (или отказан — см. причины) |
| `awaiting_user` | идёт Device Flow: код в `link.json`, демон опрашивает сервер |
| `link_expired` | код входа истёк; нужна новая «Подключить» |
| `connecting` | вход есть, список приборов или поток ещё не получены; также «остановлен в ожидании рестарта» (`message: stopped`) |
| `connected` | поток событий открыт и список приборов прочитан |
| `rate_limited` | вызовы запрещены до `rate_limited_until`: ответ 429 (`retry_after`) или исчерпан суточный лимит (`daily_limit`) |
| `offline` | облако недоступно по сети, включая ответ, не пришедший за `HTTP_TIMEOUT_S` = 20 с (вход, обновление токена, REST, поток); карточка — «BSH недоступно из этой сети» |
| `token_revoked` | сервер отверг refresh-токен (`invalid_grant`); секрет стёрт, нужен новый вход |
| `error` | непредвиденная ошибка (выход 1), небезопасный файл токенов, незнакомое состояние в файле или устаревший статус |

**Причины** (`constants.REASONS`, дополняют состояние):

| Причина | Значение |
|---|---|
| `access_denied` | пользователь отказал во входе на странице BSH |
| `client_id_rejected` | сервер не знает Client ID (`invalid_client`) |
| `token_store_insecure` | файл токенов — симлинк, не обычный файл, с жёсткими ссылками, чужой или доступен группе/другим: не читается и не используется |
| `token_store_corrupt` | файл токенов не разбирается; состояние `unlinked` |
| `budget_local_reached` | израсходовано 800 из 1000: REST остановлен до следующих суток UTC, поток и обновление токена продолжаются |
| `retry_after` | ответ 429 с `Retry-After` |
| `daily_limit` | израсходованы все 1000 вызовов суток |
| `stream_down` | поток событий лежит дольше 120 с; все приборы `"r"` |
| `status_stale` | живое состояние старше 90 с (выводит диспетчер) |
| `conf_unreadable` | при `missing_deps`: конфиг есть, но демон не может его прочитать (сломаны владелец, группа или режим, §11) — не «выключен»: выход 0, при старте или на ходу, retained-топики приборов не удаляются (приборы `"r"`); отсутствующий конфиг по-прежнему значит «выключен»; карточка: «Нет доступа к настройкам» |

**`status.json`** (0644; никогда токен, никогда коды входа, никогда имена
приборов — только счётчики). Ключи (`status.STATUS_KEYS`):

| Ключ | Тип |
|---|---|
| `state` | строка состояния (без `not_installed`) |
| `ts` | секунды эпохи |
| `version` | версия пакета |
| `reason` | причина или `""` |
| `message` | диагностика (≤ 200 символов) |
| `enabled` | bool |
| `linked` | bool — есть действующий вход |
| `host` | `api`/`simulator` |
| `client_id_source` | `own`, `vendor` или `""` |
| `appliances` | число приборов |
| `appliances_connected` | из них подключённых |
| `stream` | `up`, `down`, `off` |
| `budget_day` | сутки учёта, `YYYY-MM-DD` (UTC) |
| `budget_used` | вызовов за сутки |
| `budget_limit` | 1000 |
| `budget_local` | 800 |
| `budget_remaining` | `1000 − budget_used` |
| `rate_limited_until` | секунды эпохи или 0 |
| `last_event_ts` | время последнего события потока (кроме keep-alive) |

Статус переписывается при каждом изменении и не реже `STATUS_HEARTBEAT_S` =
30 с.

**`link.json`** (0640, группа `www-data`, **только** в `awaiting_user`):
`user_code`, `verification_uri`, `verification_uri_complete`, `expires_at`,
`ts`. `device_code` — тоже учётные данные — сюда не попадает никогда (живёт
только в памяти демона).

**`inventory.json`** (0640, группа `www-data`): `ts`, `appliances` —
`[{device_id, name, type, brand, connected, controls}]`, где `controls` —
реально опубликованные контролы. `haId` (серийный идентификатор) сюда не
пишется. Читатель — пикер «Умного дома» (`www-data` внутри
`sa02m_alice_topics.cgi`): файл другого пользователя, поэтому чтение
ограничено (64 КиБ, 32 прибора — копия `MAX_APPLIANCES`), `device_id`
проверяется по грамматике `hc-`, контролы — по закрытому списку
`HC_BINDABLE_CONTROLS`, все только для чтения; любая привязка `on_off` к
`/devices/hc-…` хранится с `writable: false`, прочие умения отвергаются
(`alice-mqtt-mapping.md` §Binding inventory, §Device document). Повтор живых
значений раз в `VALUE_HEARTBEAT_S` = 60 с держится внутри окна свежести
реестра Алисы `STATUS_STALE_S` = 90 с; оба числа сверяет
`opt/sa02m-alice/tests/test_inventory_homeconnect.py`.

**Правило слияния** (`api.merged_status` — единственный дом): нет следа →
`not_installed`; конфиг выключен → `disabled`; нет Client ID в конфиге →
`missing_client_id` (конфиг — истина, демон догонит за 2 с); включён и нет
`status.json` → `connecting`; в файле `disabled`/`missing_client_id` при
включённом конфиге с ID → `connecting`, а свежесть считается от записи
конфига; любое состояние, кроме `disabled` и `missing_deps` (пишутся один раз
перед выходом), с `ts` дальше 90 с от текущего времени → `error` +
`status_stale`; иначе — состояние демона.

## 7. Бюджет запросов

BSH допускает **1000 вызовов в сутки на пару client+user, неудачные
считаются** (G7). `budget.py`:

- Каждый вызов облака — REST (`api`), подключение потока (`sse`), запрос к
  токен-эндпоинту (`token`, включая опросы Device Flow) — **записывается до
  отправки** в `/var/lib/sa02m-homeconnect/budget.json` (атомарно, fsync).
  Упавший вызов (в том числе таймаут без единого байта ответа) и падение
  процесса посреди запроса учтены; перезапуск счёт не обнуляет.
- С 800 (`LOCAL_BUDGET`) — только поток и токены; REST ждёт следующих суток.
  С 1000 (`DAILY_LIMIT`) — ничего.
- Ответ 429 блокирует **все** вызовы до `Retry-After` (секунды или
  HTTP-дата; без заголовка — 60 с; максимум сутки), блокировка тоже в файле.
- Повтор — только сетевой ошибки и 5xx, не больше двух раз, пауза 2 → 4 с
  ± 20 %; ни один 4xx не повторяется. 401 → одно обновление токена и один
  повтор. На каждой паузе демон переписывает последний статус: три таймаута
  подряд и обновление токена перед ними держат главный поток почти 90 с, и
  без этого живой демон выглядел бы для диспетчера `status_stale`.
- Поток: переподключение 60 с → 30 мин ± 20 %, пауза сбрасывается только
  после 30 минут непрерывной работы потока — сервер, который принимает и сразу
  рвёт соединение, не превращает переподключение в шторм.
- Обновление токена — не чаще 10 в минуту.
- Сутки — по UTC. Файл бюджета, которому нельзя верить (не разбирается,
  чужой, доступен другим), считается как 800 уже израсходованных — ошибка в
  сторону меньшего числа вызовов.
- Отвязка (`unlink`) бюджет **не** сбрасывает: BSH считает вызовы по паре
  client+user, и повторная привязка того же аккаунта не начинает счёт заново.

## 8. Вход: Device Flow

1. Интегратор вводит Client ID своего приложения Home Connect (тип входа
   приложения — Device Flow; секрет не нужен) и включает модуль.
2. «Подключить» → диспетчер пишет в конфиг `link_requested_at` = сейчас,
   хелпер перезапускает демон.
3. Демон, увидев запрос не старше `LINK_REQUEST_TTL_S` = 300 с и ещё не
   обработанный, вызывает `device_authorization`, пишет `link.json`, состояние
   `awaiting_user`. Карточка показывает код и ссылку на страницу BSH.
4. Демон опрашивает токен-эндпоинт с интервалом **из ответа** (`interval`, при
   отсутствии 5 с); `slow_down` добавляет 5 с; срок кода — **из ответа**
   (`expires_in`, при отсутствии 300 с — меньшее из двух опубликованных
   значений; допустимо 30…1800 с). По истечении срока опросы прекращаются без
   вызова.
5. Исходы: токены → `tokens.json`, `link.json` удалён, дальше `connecting`;
   `expired_token` → `link_expired`; `access_denied` → `unlinked` +
   `access_denied`; `invalid_client` → `unlinked` + `client_id_rejected`.

Прочее:

- Access-токен обновляется за `REFRESH_BEFORE_S` = 1 ч до срока (`expires_in`
  из ответа; по документации 24 ч). Ответ без нового refresh-токена оставляет
  прежний. Неудачное досрочное обновление при ещё действующем токене запросы
  не роняет: повтор через 60 с с удвоением до 30 мин (`oauth.ensure_fresh` —
  единственный дом этой паузы). Истёкший и необновлённый токен — ошибка
  вызова: состояние `offline` или `connecting` с `message`.
- `invalid_grant` при обновлении → `tokens.json` заменяется маркером
  `{"revoked_at": …}` (секрет стёрт), состояние `token_revoked` переживает
  перезапуск до нового входа; приборы остаются с `"r"`.
- Токены привязаны к Client ID и хосту, для которых выданы: смена любого
  делает их непригодными — демон удаляет их и начинает с `unlinked`.
- `verification_uri`/`verification_uri_complete` принимаются только `https` на
  доменах `home-connect.com`, `home-connect.cn`, `singlekey-id.com` (и их
  поддоменах): поддельный ответ облака не превратит карточку в ссылку куда
  угодно. `user_code` — `^[A-Za-z0-9-]{4,32}$`.
- **Слот под ID CYNTRON** (Q-B): ключ `vendor_client_id` конфига используется,
  только пока `client_id` пуст; карточка его не пишет, любое сохранение
  переносит его без изменений. Появление ID CYNTRON — правка конфига, не кода.

## 9. CGI API — `cgi-bin/sa02m_homeconnect_api.cgi`

Протокол диспетчера (единственный вход CGI):
`python3 -m sa02m_homeconnect.api <METHOD>` под системным `python3`,
`PYTHONPATH=/opt/sa02m-homeconnect`, тело запроса — на stdin; stdout —
**ровно две строки**: JSON ответа, затем глагол хелпера (§10) или пустая
строка; код выхода 0 всегда, когда строки выведены. Диспетчер не открывает
файл токенов и не возвращает ни токенов, ни `device_code`. Порядок проверок в
CGI (сессия → метод → CSRF на каждом POST → тело ≤ 16 КиБ → `timeout` →
толчок хелпера только на POST и только для закреплённых глаголов) — тот же, что
у `sa02m_homekit_api.cgi` (`homekit-bridge.md` §11).

| Действие | Ответ `ok:true` | Глагол хелпера |
|---|---|---|
| `status` (или `GET`, или `POST {}`) | объект статуса (ниже) | — |
| `enable` | `{"ok":true,"action":"enable","status":{…}}` | `enable` |
| `disable` | `{"ok":true,"action":"disable","status":{…}}` | `disable` |
| `set_client_id` | `{"client_id":"<id>"\|""}` → `{"ok":true,"action":"set_client_id","status":{…}}`; сбрасывает `link_requested_at` | `restart`, только если модуль включён и ID изменился |
| `link` | `{"ok":true,"action":"link","status":{…}}`; пишет `link_requested_at` | `restart` |
| `unlink` | `{"ok":true,"action":"unlink","status":{…}}`; сбрасывает `link_requested_at` | `unlink` |

**Объект статуса:** `ok, state, reason, message, enabled, linked, host,
client_id` (свой ID — не секрет, нужен полю ввода), `client_id_source,
vendor_preset` (bool), `appliances, appliances_connected, appliance_list`
(`[{device_id, name, type, brand, connected, controls}]`, только при `linked`),
`stream, budget {day, used, limit, local, remaining}, rate_limited_until`
(только в `rate_limited`), `last_event_ts, link`
(`{user_code, verification_uri, verification_uri_complete, expires_at}` только
в `awaiting_user` и только если файл прошёл форму и срок не вышел, иначе
`null`), `read_only` (всегда `true`), `version, ts`.

**Коды ошибок диспетчера** (`{"ok":false,"error":…}`):

| Код | Когда |
|---|---|
| `method_not_allowed` | метод не `GET`/`POST` |
| `invalid_json` | тело POST не объект JSON |
| `payload_too_large` | тело > 16 КиБ |
| `not_found` | неизвестное `action` |
| `not_installed` | мутация на плате без установочного следа — конфиг не пишется, хелпер не зовётся |
| `invalid_client_id` | не строка или не `^[A-Za-z0-9_-]{8,128}$` (пустая строка допустима — очистка) |
| `not_enabled` | `link` при выключенном модуле |
| `missing_client_id` | `link` без Client ID |
| `already_linked` | `link` при действующем входе (сначала `unlink`) |
| `conf_write_failed` | запись конфига не удалась (+ `message`) |

Транспорт — всегда HTTP 200 (идиом проекта), `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`. Толчок хелпера — `timeout 11 sudo -n
/usr/local/sbin/sa02m-homeconnect-web-trigger.sh <глагол>`; бюджет 8 с + 11 с <
20 с `fastcgi_read_timeout` nginx. Если толчок случился, к ответу добавляется
`"trigger":"ok"|"failed"|"timeout"` и, если хелпер назвал код ошибки из
`[a-z_]`, `"trigger_error":"<код>"` (текст хелпера не эхом).

**Коды самого CGI** (до диспетчера или вместо него; сверяются с литералами
`www/network_config/cgi-bin/sa02m_homeconnect_api.cgi` строкой
`homeconnect-cgi`, случай K):

| Код CGI | Когда |
|---|---|
| `unauthorized` | нет живой сессии — ответ до любой работы, до чтения CSRF |
| `csrf` (+ `error_code: "E_CSRF"`, `reason`) | POST без верного `X-SA02M-CSRF` (`docs/decisions/selective-csrf-policy.md`) |
| `method_not_allowed` | метод не `GET`/`POST` |
| `payload_too_large` | `CONTENT_LENGTH` > 16 КиБ или длиннее 6 цифр |
| `not_installed` | пакета нет на плате (веб пришёл без модуля): `GET` → `{"ok":true,"state":"not_installed","linked":false,"read_only":true,…}`, `POST` → ошибка |
| `homeconnect_api_failed` | диспетчер упал, завис (`timeout 8`) или вывел не JSON |

## 10. Привилегированный хелпер

`/usr/local/sbin/sa02m-homeconnect-web-trigger.sh`, ровно четыре закреплённых
глагола (`constants.TRIGGER_VERBS`):

| Глагол | Действие |
|---|---|
| `enable` | `unmask` + `enable` + `restart` юнита |
| `disable` | `stop` + `disable` (демон, остановленный при выключенном конфиге, сам удаляет свои топики и `inventory.json`) |
| `restart` | `restart`, только если модуль включён |
| `unlink` | `stop` → удалить `tokens.json` и хвосты `.hc-*.tmp` → `start`, если включён. **`budget.json` и `appliances.json` остаются**: первый — §7, второй нужен перезапущенному демону, чтобы удалить retained-топики приборов отвязанного аккаунта |

Каждый `systemctl` — под `timeout`, удаление — только после полной остановки
юнита (`is-active` = `inactive`/`failed`; `deactivating` — отказ
`still_running`), симлинк каталога состояния — отказ `state_dir_invalid`; в
каталогах демона root только удаляет имена (`rm -f` удаляет ссылку, не её
цель), `disable` и `unlink` удаляют и `link.json` (убитый демон сам его не
уберёт). Глаголы идут по одному (`flock` на собственном файле хелпера;
занято — `busy`). Статус при `disable` root не пишет: диспетчер выводит
`disabled` из конфига (§6). Реализация —
`usr/local/sbin/sa02m-homeconnect-web-trigger.sh`, грант —
`etc/sudoers.d/sa02m-homeconnect` (четыре глагола), гейт — строка
`homeconnect-trigger`.

## 11. Файлы и права

| Путь | Владелец, режим | Читает | Пишет |
|---|---|---|---|
| `/var/lib/sa02m-homeconnect/` | `sa02m-homeconnect:sa02m-homeconnect` 0700 | демон | демон |
| `…/tokens.json` — токены или маркер `revoked_at` | 0600 | демон | демон (атомарно + fsync) |
| `…/budget.json` — счёт вызовов суток | 0600 | демон | демон |
| `…/appliances.json` — `[{ha_id, device_id}]` опубликованных приборов | 0600 | демон | демон |
| `…/.hc-*.tmp` — хвосты прерванной атомарной записи | 0600 | — | демон |
| `/run/sa02m-homeconnect/` | `sa02m-homeconnect:www-data` 2750 (setgid: новые файлы получают группу `www-data`, членство демону не нужно) | демон, CGI | демон |
| `…/status.json` | 0644 | CGI | демон |
| `…/link.json` | 0640, группа `www-data` | CGI | демон |
| `…/inventory.json` | 0640, группа `www-data` | CGI | демон |
| `/etc/sa02m-homeconnect/` | `www-data:sa02m-homeconnect` 2750 (setgid: файл, созданный здесь, получает группу клиента) | демон (вход, список) | CGI (владелец; атомарная запись требует права на каталог) |
| `…/sa02m-homeconnect.conf` — `[account] enabled, client_id, vendor_client_id, host, link_requested_at` + `[control] mode = off`; не секрет | `www-data:sa02m-homeconnect` 0640 | демон (группа, только чтение), CGI, хелпер | CGI (атомарно), root (сохраняя владельца) |
| `/opt/sa02m-homeconnect/` | `root:root`, `go=rX` | демон, CGI | установщик, OTA |

Каждая запись пакета (`fsutil.atomic_write`) идёт относительно дескриптора
каталога, открытого `O_NOFOLLOW`: временный `.hc-<случайное>.tmp` создаётся
`O_CREAT|O_EXCL|O_NOFOLLOW` 0600, режим и группа ставятся через дескриптор,
`fsync`, `rename` в том же каталоге, `fsync` каталога. Подложенный симлинк на
временном или конечном имени не проходится; `rename` заменяет ссылку, а не её
цель. Файлы демона читаются тем же путём (`fsutil.read_private`): симлинк,
не обычный файл, несколько жёстких ссылок, чужой владелец или биты группы/
других — отказ без чтения.

Юнит (`etc/systemd/system/sa02m-homeconnect.service`): `User=sa02m-homeconnect` (системный, без оболочки и дома),
**без группы `www-data`** (ни в юните, ни в `/etc/group`: она читает пароль
панели и все веб-конфиги) — файлы 0640 для CGI получают группу от setgid-каталога
`/run/sa02m-homeconnect`; конфиг клиент читает через свою группу, **без ACL**
(в продуктовом RT-ядре POSIX ACL нет, стенд 1.135, 2026-09-28): каталог
конфига принадлежит `www-data` с setgid-группой клиента, так что конфиг,
сохранённый CGI атомарно, всегда получает группу клиента, а клиент его только
читает. Проверка — действием: `usr/local/sbin/sa02m-daemon-access.sh apply
homeconnect` восстанавливает владельца, группу и режим и пробует чтение от
имени клиента (`setpriv`), а заодно — что пароль панели и документ устройств
Алисы ему не читаются; это последний шаг `scripts/06d-homeconnect.sh`
(провал — русская строка, выход 1) и `ExecStartPre=-+` юнита перед каждым
стартом (провал — в журнале, демон показывает `conf_unreadable`); установщик
исключает из `www-data` учётную запись, созданную раньше; гейты
`daemon-least-privilege` (строки) и `daemon-access-effect` (действие) — один
дом правила — `docs/contracts/homekit-bridge.md` §13.
`ProtectSystem=strict`,
`ReadWritePaths=/var/lib/sa02m-homeconnect /run/sa02m-homeconnect`,
`NoNewPrivileges`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
`MemoryMax=32M`, `CPUQuota=20%`, `TasksMax=16`, `Nice=10`,
`Restart=on-failure`, `TimeoutStopSec=8s` (остановка укладывается в 10 с
`timeout systemctl stop` хелпера); `ExecStart=/usr/bin/python3 -m
sa02m_homeconnect`, `Environment=PYTHONPATH=/opt/sa02m-homeconnect`. `www-data`
в группу `sa02m-homeconnect` не входит — токенов не читает (P4). Каталоги
создаёт `opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf`, который в
`/etc/tmpfiles.d/` кладёт только установщик `06d` (OTA приносит его в `/opt`
инертным: строки называют пользователя, которого на плате без модуля нет).

## 12. Жизненный цикл

- **Установка** — `scripts/06d-homeconnect.sh`: пользователь, каталоги,
  засевка конфига (`enabled = false`), юнит выключен (`app off`,
  `installer-refresh-policy.md`). Повторный запуск обновляет код, не трогая
  конфиг, токены и бюджет.
- **OTA** обновляет пакет, юнит, хелпер, sudoers, CGI и JS, но не
  устанавливает модуль: без пользователя плата показывает `not_installed`.
  `/var/lib/sa02m-homeconnect/` и `/etc/sa02m-homeconnect/` — во всех списках
  «не разворачивать»; активный клиент перезапускается после обновления,
  остановленный не запускается.
- **Включение/выключение** — карточка или «Пуск»/«Стоп» каталога служб.
  Демон перечитывает конфиг каждые 2 с: `enabled = false` → удаление своих
  топиков, выход 0.
- **Отвязка** — §10.
- **Factory reset — СТЕРЕТЬ** (Q-F: при перепродаже аккаунт прежнего владельца
  не должен видеть плату, а плата — его приборы): конфиг к шаблону
  (`enabled = false`) первым, затем остановка юнита — демон при остановке с
  выключенным конфигом удаляет retained-топики приборов, — затем очистка
  содержимого `/var/lib/sa02m-homeconnect/` (включая `.hc-*`). Очистка —
  и входа, и пар моста HomeKit (`homekit-bridge.md` §14) — идёт только после
  того, как остановлены **оба** юнита; отказ остановки любого — `E_APPLY`,
  конфиги откатываются, не стёрто ничего, а юниты, работавшие до сброса,
  после восстановления конфигов запускаются снова (по мере сил; не работавший
  до сброса остаётся остановленным).
- **Снятие образа — СТЕРЕТЬ**: содержимое `/var/lib/sa02m-homeconnect/`
  (включая `.hc-*`), `enabled = false` в конфиге, юнит выключен — на всех пяти
  площадках `image-identity-reset.md` §4.
- **Веб-бэкап** — `/var/lib/sa02m-homeconnect/` не включается никогда;
  конфиг включается (Client ID не секрет).

**Где это исполняется.** Установщик — `scripts/06d-homeconnect.sh`
(`install.sh --with-homeconnect` или `SA02M_WITH_HOMECONNECT=1`; уже
установленный клиент обновляется `install.sh` и без флага; в заводской образ не
входит), юнит и tmpfiles — §11, хелпер и грант — §10, CGI — §9; списки «не
разворачивать» OTA — `etc/sa02m-update-runner.sh` (`PRESERVE_PATHS`, загрузочный
`PRESERVE_PREFIXES`), `opt/sa02m-update/lib/validate_package.py`,
`scripts/offline-update-deploy-map.json`; перезапуск только активного —
`restart_if_active` раннера и `scripts/pack-offline-update.py`; веб-бэкап —
`etc/sa02m-web-backup.sh` (только конфиг) и `etc/sa02m-restore-backup.sh`
(конфиг, без перезапуска юнита); factory reset —
`erase_owner_state()` в `etc/sa02m-factory-reset-runner.sh`; снятие образа
— `wipe_homeconnect_identity()` на пяти площадках
(`image-identity-reset.md` §8); «Пуск»/«Стоп» каталога служб —
`homeconnect_sync_enabled` в `etc/sa02m-web-service-ctl.sh` (строка
`homeconnect|Home Connect|sa02m-homeconnect.service`, «установлен» = пакет и
системный пользователь). Проверки — §15.

## 13. Только чтение и вне объёма v1

Первый выпуск **не управляет приборами**: нет команд, нет подписок на MQTT,
нет права `Control`/`Settings`, у REST-клиента нет методов, кроме `GET`,
`[control] mode` принимает только `off` (иное — отказ с записью в журнал).
Остановка программы и выключение (безопасное направление, Q-E вариант 2) —
отдельное решение Оператора и отдельный выпуск; запуск программ не планируется
вовсе.

Вне объёма v1: прокси для исходящих соединений; несколько аккаунтов;
локальный протокол приборов (`hcpy`); изображения приборов; настройки
(`/settings`); облачное реле токенов.

## 14. Что не проверено до стендовой проверки (G6, G7)

Код не зашивает ни одно из этих чисел и поведений, но они не подтверждены на
настоящем облаке — из контейнера разработки облако BSH недоступно
(`docs/decisions/homekit-home-connect.md` G6):

- доступен ли API для аккаунтов SingleKey ID из России (G6, критерий 2). С
  российского IP на стенде (2026-09-28) облако BSH **не ответило**: TCP и TLS
  проходят, затем ни байта за 25 с — клиент там стоит в `offline`;
- фактические `expires_in`/`interval` Device Flow (5 или 10 мин) и срок
  жизни refresh-токена (G7);
- принимает ли токен-эндпоинт `client_id` в запросе обновления (RFC 6749
  допускает его для публичного клиента);
- на каких доменах BSH отдаёт `verification_uri` (список §8 — допущение);
- приходит ли `BSH.Common.Setting.PowerState` и отвечает ли `programs/active`
  при праве `Monitor` без `Settings`;
- форма данных событий `CONNECTED`/`DISCONNECTED`/`DEPAIRED` (`haId` в
  данных или только в `id`) — парсер принимает оба;
- окно суточного лимита BSH: сутки UTC или скользящие 24 ч (запас 200
  вызовов — страховка), и минутные лимиты сверх ≤ 10 обновлений токена;
- ограничено ли неопубликованное приложение своим тестовым аккаунтом (для v1
  снято решением Q-B).

Процедура стендовой проверки — `docs/decisions/homekit-home-connect.md` G6/G7;
её результат вносится туда и, если меняет поведение, сюда.

## 15. Проверка контракта

| Строка реестра / тест | Что доказывает | Состояние |
|---|---|---|
| `py-unit-homeconnect` (`opt/sa02m-homeconnect/tests/`, stdlib `unittest`, локальный фейк облака на `http.server`) | Device Flow (успех, `slow_down` +5 с, истечение серверное и локальное без вызова, отказ, неизвестный ID, чужой домен `verification_uri`), обновление (за 1 ч, сохранение старого refresh-токена, `invalid_grant` → маркер без секрета, ≤ 10/мин); бюджет (учёт до отправки, неудачные вызовы, переживает перезапуск, 800/1000, 429 блокирует всё, смена суток, недоверенный файл = 800); REST (только `GET`, ни один 4xx не повторяется, 5xx с паузой, 401 → одно обновление, перенаправление — ошибка, https-only); SSE (поля, многострочные данные, `id` из `haId`, пределы строки/события, переподключение после EOF и молчания дольше дедлайна, рост паузы, остановка прерывает чтение); таблица §4 и очистка id/имён; топики §3, `meta/error`, удаление; повтор значений §5, пока поток жив, и его отсутствие при лёгшем или молчащем потоке; статус и права файлов §6/§11; диспетчер §9; демон целиком (вход → `connected` → события → топики, `DISCONNECTED` → `"r"` без переподключения, обрыв потока → `"r"` всем и восстановление с перечитыванием, отвязка и выключение чистят топики, отзыв переживает перезапуск, 429, небезопасный файл токенов не используется; ни токен, ни `device_code` не попадают в журнал, `/run` и MQTT; каждый запрос к фейку учтён в бюджете) | действует |
| `test_unresponsive_cloud.py` (в составе `py-unit-homeconnect`) | облако, которое принимает соединение и молчит (как облако BSH с российского IP на стенде): вход, обновление токена, REST и поток кончаются одним состоянием `offline`; каждая попытка учтена в бюджете до отправки; вход сам не повторяется; во время серии повторов REST статус переписывается, а не стареет до `status_stale` | действует |
| `test_token_sinks.py` (в составе `py-unit-homeconnect`) | идентификаторы `access_token`/`refresh_token` есть только в `oauth.py` и `token_store.py`, `device_code` — только в `oauth.py` (allow-list, не список запрещённых мест); обход не пуст, обе «законные» точки реально содержат идентификаторы | действует (отдельной статической строки `homeconnect-token-sinks` нет: allow-list держит этот тест) |
| `test_contract_tables.py` (в составе `py-unit-homeconnect`) | таблица §4, состояния, причины и ключи `status.json` §6, действия и коды ошибок диспетчера §9, глаголы §10 совпадают с кодом как множества; пустая или пропавшая таблица — провал | действует |
| `homeconnect-trigger` (`scripts/dev/test-homeconnect-trigger.sh`) | хелпер §10: четыре глагола и ничего больше, каждый `systemctl` под `timeout` (измерено шимом), `unlink` удаляет ровно `tokens.json` + `.hc-*.tmp` + `link.json` и только после полной остановки (`active`/`deactivating` — отказ), `budget.json`/`appliances.json` остаются, симлинк каталога — отказ, `restart` читает `enabled` только в `[account]`, блокировка `busy`, истинность `enabled` совпадает с `config.load()` | действует |
| `daemon-least-privilege` (`.ai-dev/quality/checks/daemon-least-privilege.sh`) | §11, строки: в юните нет `SupplementaryGroups=` и `www-data`, `scripts/06d-homeconnect.sh` не добавляет учётные записи в группы и исключает клиента из `www-data` при обновлении, `/run/sa02m-homeconnect` — 2750 `:www-data`, каталог конфига — 2750 `www-data:sa02m-homeconnect`, ни одной строки ACL, установщик заканчивается проверкой действия, юнит запускает её перед стартом. RED 2026-09-28 на дереве с `www-data` и на ACL-дереве 9cf41b5 | действует (сборка) |
| `daemon-access-effect` (`scripts/dev/test-daemon-access.sh`) | §11, действие на настоящих файлах от не-root uid: новая раскладка проходит, 660 `root:www-data` — провал с именем конфига, `apply` лечит, сохранение CGI от `www-data` оставляет конфиг читаемым клиенту; чужой файл под именем конфига не передаётся `www-data` (I), жёсткая ссылка и FIFO — отказ (J), чтение клиентом документа устройств Алисы — провал (K), новый конфиг от `www-data` получает группу клиента и 0640 (L) | действует (сборка) |
| `homeconnect-cgi` (`scripts/dev/test-homeconnect-cgi.sh`) | CGI §9: сессия до всего, CSRF на каждом POST, тело ≤ 16 КиБ, `GET` ничего не толкает, `sudo` получает ровно четыре закреплённых глагола, отказ/зависание хелпера — фиксированный enum, сбой диспетчера — `homeconnect_api_failed`, коды CGI = таблица «Код CGI», настоящий диспетчер не отдаёт ни токена, ни `device_code` | действует |
| `homeconnect-card-smoke` (`scripts/dev/homeconnect-card-smoke.mjs`, review, Chromium) | карточка: каждое состояние §6 и причина — по-русски, код входа и QR только в `awaiting_user` (QR сверяется с эталонной матрицей), ссылка только https на доменах BSH, имена приборов как текст, кнопки заблокированы во время действия, подтверждение отвязки, ≤ 560 px | действует |
| `py-unit-alice` (`test_inventory_homeconnect.py`, `test_models.py`), `alice-topics-cgi` (случай 8) | потребитель §6: пикер читает `inventory.json` ограниченно и с проверкой (размер, число приборов, грамматика id, закрытый список контролов), привязка `on_off` к `hc-` — только чтение, прочие умения отвергаются; копии `MAX_APPLIANCES`, имён `MAPPING` и `VALUE_HEARTBEAT_S` < `STATUS_STALE_S` сверены с пакетом текстом | действует (сборка) |
| `py-unit-rules` (`test_homeconnect_recipes.py`) | рецепты сценариев из `docs/HOME_CONNECT_INTEGRATION.md` проходят проверку хранилища без искажений и срабатывают в движке ровно по фронту (повтор значения раз в 60 с не срабатывает) | действует (сборка) |
| `sudoers-pin-contract` | грант `etc/sudoers.d/sa02m-homeconnect` — полный реестр (четыре глагола), закрепление аргументов, путь гранта = путь установки на всех трёх путях доставки | действует |
| `cgi-csrf-policy` | строка `sa02m_homeconnect_api.cgi` в реестре CSRF: токен до мутации | действует |
| `installer-svc-policy-gate`, `installer-order` (случай 6), `install-atomic` | `06d`: захват до `app off`, юнит и хелпер через `sa02m_atomic_install`; `05-mqtt.sh` до `06d`; пакет до засевки конфига, хелпер и sudoers до CGI | действует |
| `update-conditional-restart` (прогон 8) | `sa02m-homeconnect` в `restart_if_active` онлайн и офлайн, ни в одном `restart[]`; работающий перезапускается, остановленный только опрашивается | действует |
| `alice-conf-homes` (разделы 13–15) | конфиг и хранилище токенов во всех четырёх списках «не разворачивать»; бэкап несёт конфиг и ни байта токенов; восстановление принимает конфиг, отвергает токены, юнит не трогает; засевка `06d` не идёт по подложенной ссылке и изолирована (`python3 -I`); tmpfiles попадает в `/etc/tmpfiles.d/` только через `06d` | действует |
| `alice-image-identity` (часть C) | пять площадок снятия образа, `image-identity-reset.md` §8 | действует |
| `factory-reset-runner` (C1–C7, O1–O4) | factory reset §12: конфиг к шаблону → остановка → очистка, отказ при неостановленном юните и подложенном конфиге, без пакета — `enabled = false` с сохранённым Client ID; неостановленный клиент не стоит пар HomeKit (O1), неостановленный мост не трогает клиента (O2); откат запускает снова только работавшие до сброса юниты (O3–O4) | действует |
| `service-ctl-policy-write` (раздел 3) | «Пуск»/«Стоп» синхронизирует `enabled` через `python3 -I`: пакет, подложенный в cwd, не импортируется от root | действует |

Не проверено здесь (стенд): §14 целиком; настоящий `systemd`/`sudo`/`visudo`
на плате (моделируются шимами и реестром грантов).
