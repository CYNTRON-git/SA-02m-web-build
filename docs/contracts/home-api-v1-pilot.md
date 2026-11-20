# Home API v1: контролируемый пилот и откат

Это узкая процедура для явно разрешённой установки и проверки Home API,
не публикация firmware release. Все repo-owned bytes берутся из отдельного
проверенного commit. Исходная рабочая копия и установленные конфигурации
сохраняются. Общий `deployed_commit` и версия веб-интерфейса не подменяются.

## До изменения платы

1. Зафиксировать source commit, base commit, SHA256 manifest, зелёный strict
   build и независимый review. Собрать архив командой
   `python3 scripts/pack-home-api-pilot.py --ref HEAD --base <base-commit> --output <new-directory>`.
   Упаковываются git bytes; незакоммиченные рабочие файлы в пакет не попадают.
2. Проверить pinned SSH host key именно выбранной платы. Прочитать version,
   активные/enabled units, путь и SHA nginx site, registry/rules revision,
   свободное место, наличие Python3, paho, sa02m-devices/websession.
3. Прочитать enabled сценарии, boot действия, logic templates/targets, end
   timers, `.run` flag, журналы и сохранённое logic state. Не считать отсутствие
   `.run` или недавних runs доказательством пустой in-memory очереди.
4. На исследованной .135: 3 сценария, 2 enabled; boot→notify и switch_light
   для 3 `devices.types.light`. Диммер: 300 ms, шаг10, минимум5,
   `long_dim=true`, `switch_type=auto`; end timers нет. Данные preflight
   перепроверяются непосредственно перед установкой, а не считаются вечными.

## Остаточные риски и окно

Текущий engine не экспортирует active jobs. SwitchLight может бесконечно
перепланировать dim каждые300ms, пока удерживается кнопка; длительность не
ограничена. SIGTERM завершает процесс и flushes journals/state, не отправляя
команд выключения/восстановления. SwitchLight не экспортирует dim-state и
имеет no-op on_boot, поэтому новая копия сама диммер не продолжает. Возможны
прерывание удержания и пропуск физического события в окне reconnect.
Доставка не доказывает jobs=0. Пилот проводится в спокойное окно освещения,
не во время удержания кнопок. Любой новый критический target (насос, клапан,
отопление), code/иная logic или неизвестная очередь требует повторной оценки
окна до restart; marker этого ограничения не отменяет.

## Backup и доставка

Создать уникальный root0700 каталог backup на плате. Для **каждого** пути из
manifest записать existed/uid/gid/mode/SHA и сохранить старые bytes; сохранить
реальный nginx site и symlink target, unit files/drop-ins и enabled/active
состояния `sa02m-home-api`, `sa02m-rules`, `sa02m-cloud-control`, nginx.
Снять backup `/etc/sa02m-alice/sa02m-alice-devices.conf`, всего
`/etc/sa02m-rules` (включая journals/logic state), нового ledger если существует.
Backup содержит приватные настройки: не печатать содержимое и не включать в
публичный отчёт. Hash manifest можно сохранять без значений конфигурации.

Пакет переносится в новый staging directory и проверяется по SHA256SUMS.
Runtime файлы устанавливаются атомарно через sibling temporary file+rename,
root:root0644. Никаких глобальных удалений, `--refresh`, apt, reboot, DTB/UART
или переустановки Mosquitto. `recipes/nginx.patch` — ровно reviewed git delta:
применить к **копии** реального site, проверить контекст (порт/прочие location
сохраняются), сохранить candidate, выполнить `nginx -t` до reload. Не копировать
шаблон с `__PORT__` поверх живого site. При несовпадении контекста остановиться
до изменения активного site, сверить с исходным template.

После unit delivery: `systemctl daemon-reload`. Запустить новый
`sa02m-home-api.service`; он остаётся отдельным opt-in и не меняет настройки
legacy служб. Сначала auth negatives и read-only LAN `/meta`, `/controllers`,
`/snapshot`, history/events; scenario_run пока0, если старая rules ещё работает.

## Одноразовый restart rules

Новая версия `sa02m_rules.upgrade` готовит **один** root0600 marker на текущем
boot ID, случайном nonce, SHA сценарного документа и monotonic expiry60s:

```sh
PYTHONPATH=/opt/sa02m-rules python3 -m sa02m_rules.upgrade prepare
systemctl restart sa02m-rules.service
```

Выполнять подряд в согласованном окне, после повторного preflight; никаких
авторетраев prepare. RuntimeDirectory0700 сохраняется на restart. Consume
отказывается от symlink/hardlink/неверного owner/mode/boot/hash/expiry и
атомарно переименовывает marker в `consumed-<nonce>.json`. Это пропускает
только явные boot triggers **этого** запуска. Enabled flags не меняются,
обычная обработка MQTT/таймеров остаётся; logic startup не подавляется.
Prepare допускает только проверенный здесь switch_light среди enabled logic.
Проверить journal сообщения consume и fresh non-retained heartbeat;
`features.scenario_run` должен стать1 в течение30s. Отказ consume требует
диагностики, не цикла рестартов. Следующий обычный запуск вновь исполняет
boot triggers по прежней политике — это важная часть окна и rollback.

Перезапустить только существующий активный cloud-control для объявления
`X-Home-API-Version:1.0`; не активировать выключенную облачную интеграцию.
После `nginx -t` reload nginx. Проверить прежние CGI, LED/registry services,
порты, serial fingerprint, стабильность службы и независимость LAN от cloud.

## Приёмка и остановка пилота

Сначала read-only native app/cloud snapshot сравнение identity/metadata и
архивов. Затем root координирует один выбранный безопасный световой канал:
исходное состояние, абсолютная команда с новым ID и lease, receipt, повтор
того же ID без второго dispatch, наблюдение, восстановление исходного
состояния отдельной явной командой. Никаких групповых сценариев/критических
выходов по умолчанию. MQTT observed не означает подтверждённую электрическую
приёмку. Фиксировать факт и предел измерения отдельно.

На success сохранить `/var/lib/sa02m-home/pilot-manifest.json` с commit,
SHA, временем, backup path и результатами, без token/cookie/config values.
Состояние opt-in фиксируется отдельно. Архив/manifest вместе с source commit
переносятся на Mac в отдельный `projects/linux/SA-02m-home-api`.

## Откат

Остановить только новый Home API; вернуть записанные старые runtime/unit/site
файлы атомарно с исходными mode/owner, удалить только пути, помеченные new в
backup manifest. `nginx -t` до reload, daemon-reload. Ledger и receipts
**сохранить** для аудита/дедупликации; не откатывать SQLite к старому состоянию.
Не восстанавливать registry/rules/config backup поверх последующих изменений
без сравнения текущих SHA — runtime rollback сам по себе их не меняет.
Restore прежних active/enabled состояний только затронутых служб; rollback
старой rules снова имеет обычные boot side effects и требует того же окна.
Consumed marker — audit receipt, не глобальное отключение boot; неизвестные
markers не удалять автоматически. При failure не сбрасывать firewall,
не переустанавливать пакеты и не перезагружать плату.
