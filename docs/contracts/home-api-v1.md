# Единый Home API платы

`/api/home/v1` обслуживает локальное приложение и облачный мост одним
процессом `sa02m-home-api`. Канон формата и примеры —
[home-api-v1-wire.json](home-api-v1-wire.json), исполняемая проверка —
`opt/sa02m-alice/sa02m_alice/home/contract.py`. Старые CGI и Alice events
сохраняют совместимость; их команды без `command_id` не приобретают
автоматически новые гарантии дедупликации.

## Доступ и идентичность

LAN использует существующий `session_token`; каждый POST, включая lease,
проверяет `X-SA02M-CSRF`. Ошибки нового API имеют реальные HTTP-коды:
401 для сессии, 403 с `E_CSRF` для CSRF. Никакого CORS или общего exec нет.
HTTP допустим в явно выбранной частной LAN; пароль и cookie нельзя отправлять
на произвольный публичный адрес. HTTPS/VPN на внешней границе сохраняются.

Демон слушает только `/run/sa02m-home/api.sock`, 0660 root:www-data.
Публичные пути снова проверяют сессию внутри демона. Внутренний путь
`/internal/home/v1` доступен только Unix-peer UID 0; nginx его не проксирует.
`home_api_v1` зарегистрирован только в существующем авторизованном cloud
Socket.IO профиле. Облачный owner/admin ACL выполняется до RPC в cloud repo.
`request_id` принадлежит транспорту; `command_id` не заменяется им.
Cloud handshake объявляет `X-Home-API-Version: 1.0` только в cloud профиле.
Это поддержка установленного протокола, не здоровье процесса: отказ нового
демона остаётся 503. Только текущая авторизованная старая регистрация без
заголовка получает 422 `home_api_unsupported` с `legacy_control_supported:true`.
Неверная/несовместимая версия, offline и timeout не разрешают fallback.

`controller_id` — существующий board serial, общий с остальными интеграциями,
никогда IP. Внешний cloud URL использует fleet ID, отдельное поле
`fleet_controller_id`. Приложение привязывает явно выбранный LAN адрес к
прочитанному controller identity и не меняет маршрут записи автоматически.

## Состояние и управление

Snapshot сохраняет `home/devices/rooms/groups/scenarios/online/linked` и
добавляет `catalog_revision`, metadata всех capability instances, `value`,
`state`, `quality`, `observed_at`, `availability`, `can_control`.
`online` означает живое подключение локального MQTT, `linked` — загруженный
локальный registry. Реальная облачная связь вынесена в `cloud_linked` и
не блокирует LAN. `live/cached/unknown` относится к значению, не к разрешению.
Недавнее non-retained состояние/uptime того же физического устройства
разрешает абсолютную команду при cached значении; один retained cache — нет.
Существующие device-offline, readonly, range/mode/RGB/scale/inversion правила
исполняет исходный DeviceRegistry, без второго конвертера.

Для нового пути подготовка публикации не подменяет кеш желаемым состоянием.
Publisher делает одну QoS1 попытку к localhost:1883, ждёт PUBACK с timeout,
не сохраняет reconnect очередь и не ставит retain. Потеря ответа —
`outcome_unknown`, не повод отправить новое управление.

SQLite ledger в `/var/lib/sa02m-home/receipts.sqlite3` сохраняет команду и
`dispatch_started` до side effect (`synchronous=FULL`). Одинаковый ID/смысл
возвращает исходную квитанцию; другой смысл — 409. После рестарта незавершённые
квитанции становятся `outcome_unknown`. Автоповторов нет. Окно хранения —
не менее 24 ч monotonic-времени, максимум 10000 записей и 4096 SQLite pages; переполнение запрещает новую
команду. Lease действует 60 с по monotonic clock текущего процесса; после
рестарта прежняя lease недействительна. Дубликат существующей квитанции
доступен независимо от срока lease. После удаления старой квитанции старая
lease уже не допускает её повторное исполнение.
Очистка никогда не использует wall clock. После рестарта все прочитанные
квитанции консервативно сохраняются ещё полное окно; это может раньше
дать 429 на заполненном хранилище, но не повторить команду при коррекции часов.

`published` подтверждает только broker PUBACK. `observed` — совпавшее новое
non-retained MQTT состояние после отправки; `physical_confirmation` всегда
false, пока отдельного физического измерения нет. Через 15 с без наблюдения
результат `outcome_unknown`. Квитанция конфигурации `applied` подтверждает
запись документа, не электрический выход.

В квитанции изменения каталога `catalog_revision` соответствует сохранённой
версии документа. Если сериализованный `result` занимает до 64 КиБ UTF-8
включительно, клиент получает его целиком. Для большего результата плата
сохраняет краткую форму с `details_omitted: true`, исходным `ok` и, когда
операция вернула объект, его ID в прежнем поле, например `result.room.id`.
Статус и подтверждение команды сохраняются. При GET по UUID и повторном POST
прежние объёмные квитанции возвращаются в той же краткой форме.

## Каталог, сценарии и версии

GET `/devices`, `/rooms`, `/groups` возвращает полные соответствующие
stored массивы с `catalog_revision`; `/snapshot` — представление состояния.
GET `/scenarios` возвращает summaries/journal, `?id=...` — полный scenario.
Все изменения проходят через `/commands`, включая rename и CRUD. Для них
обязателен `expected_revision` прочитанного документа. Revision не включает
runtime runs/notifications. Чтение каталога и revision согласовано общей
блокировкой; каталог и сценарный документ блокируются в одном порядке.
Изменение device.upsert существующего target объединяет data с исходной
строкой, сохраняя опущенные поля. Rename комнаты/группы без списка устройств
сохраняет состав; явный пустой список очищает его.
Scenario upsert объединяет patch с прочитанной строкой и проверяет известные
поля прежним валидатором. Расширения строки, trigger/action/condition items
и condition container сохраняются также при полном GET→upsert roundtrip;
значения известных полей всегда берутся из валидатора. Home путь отказывает
при dropped элементах вместо молчаливой потери части сценария. Metadata-only
rename/enable сохраняет прежнюю возможность отключить сценарий с уже удалённым
binding, не повторяя target resolution. Пустой `code` у block/scene допустим;
непустой код и создание type=code запрещены. Device extensions также проходят
roundtrip regression.

Scenario.run не создаёт старый `.run` флаг. Короткий MQTT запрос несёт только
command ID, а rules engine получает одноразовый durable claim от dispatcher.
Retained и повторный claim отклоняются. Deadline 5 с проверяется перед
исполнением. Под блокировкой каталога и сценариев движок повторно проверяет
исходный revision, делает reload и run_now; изменение сцены между prepare
и получением запроса не запускает новый вариант. Ответ `engine_started`
не подтверждает завершение отложенных действий; дальнейшее состояние находится
в существующем сценарном журнале. Код сценариев не редактируется этим API,
универсального cancel нет: прежний run_off выключает оборудование и не
является нейтральной отменой. Эти возможности не объявляются поддержанными.
`features.scenario_run=1` требует живого non-retained heartbeat новой rules
службы, не старше 30 с. Наличие файла на диске либо retained heartbeat не
доказывает работающий claim protocol. Старый процесс получает явный отказ
`rules_runtime_upgrade_required`, без публикации команды.

Локальный scheduler остаётся существующим `sa02m-rules`, работает без WAN.
Его clock/DST/boot/state policy не заменяется облачным scheduler. Этот change
не обещает выполнение пропущенных wall-clock событий при неизвестных часах.

## История, события и пределы

История и события используют существующие `sa02m_devices` функции, без
синтетического backfill. `source=board_archive`,
`coverage=available_records_only`; физический `device_id`, units и timestamps
сохраняются. История: 1h/6h/24h/7d/30d или window_s 60..2592000, существующее
bucket aggregation. События: limit 1..500, максимум 30 дней, newest first.
Ответ ограничен 2 MiB; запрос 64 KiB, 16 query keys по одному значению до
512 символов. Нет SSE, очистки журнала, shell, произвольного MQTT topic.
Отсутствующий rules пакет даёт feature 0 и явный unsupported. Пакет
`sa02m-devices` обязателен для канонической panel session проверки; доступность
его archive API отражается отдельно в history/events features.
Account/claim/push остаются облачными возможностями.

## Проверки и включение

Quality row `home-api-v1` выполняет настоящие HTTP auth/CSRF проверки,
SQLite dedupe/restart/limits, fake MQTT и настоящий локальный MQTT socket,
scenario revision/claim/deadline, POSIX locks и wire fixtures. Мутации с
оптимистическим кешем и выключенным CSRF дают ожидаемый RED внутри теста.
Полные Alice и rules suites остаются обязательными. Strict Alice permission
test запускается обычным пользователем; root skip не считается PASS.

Установщик доставляет новый unit выключенным, сохраняя прежний opt-in.
Для включения нужны совместимые Alice, rules и devices пакеты, nginx location,
оба systemd unit файла. Пилотный package manifest и rollback должны сохранить
прежние файлы, nginx config, unit active/enabled состояния и конфигурации.
После backup: атомарная доставка, `nginx -t`, daemon-reload, запуск только
нового API, согласованный restart cloud-control/rules и nginx reload.
Точная pilot процедура с одноразовым skip-boot, остаточными рисками и
откатом — [home-api-v1-pilot.md](home-api-v1-pilot.md).
Никаких перезагрузок платы, изменений UART, прошивок или дисков эта функция
не требует. Аппаратный тест проводится отдельно по выбранному безопасному
каналу с исходным состоянием, наблюдением и восстановлением; software PASS
не считается аппаратной приёмкой.
