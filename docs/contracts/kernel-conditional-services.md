# Контракт: kernel-conditional службы и владение OPC UA-портами

Домашний адрес политики «какие службы стартуют на каком ядре» и распределения
OPC UA-портов между CODESYS и шлюзом `sa02m-mqtt-opcua`. Машинная грамматика
(имена юнитов, портов, subcommand'ов) — на английском (`PROTOCOL.md`
invariant 5); пояснения — на русском. Введён в 1.0.5.57.

Валидирующий гейт: `.ai-dev/quality/checks/kernel-policy-contract.sh`
(quality-row `kernel-policy-contract`, build beat).

---

## 1. Владение портами OPC UA

| Порт | Владелец | Примечание |
|---|---|---|
| `4840/TCP` | **CODESYS** — его собственный OPC UA-сервер | IANA-порт OPC UA; vendor-фиксирован, не наш и не переносится |
| `4841/TCP` | **`sa02m-mqtt-opcua`** — наш northbound-шлюз MQTT→OPC UA | конвенциональный «второй OPC UA-endpoint на хосте» |

Гарантия: шлюз **никогда не претендует на 4840** — дефолт порта одинаков в трёх
местах и закреплён гейтом (не равен 4840, все три совпадают):

- `etc/sa02m-mqtt-opcua.conf` → `"opcua": { "port": 4841 }`;
- `opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py` — оба дефолта
  (`load_config()` и `ocfg.get("port", …)`).

**Миграция развёрнутых устройств:** `install.sh` **безусловно** переписывает
`"port": 4840 → 4841` в существующем `/etc/sa02m-mqtt-opcua.conf` (остальные
ключи сохраняются; atomic replace; отказ миграции оставляет конфиг нетронутым и
логируется). Конфиг, сознательно оставленный на 4840, — ровно тот класс
конфликта (EADDRINUSE crash-loop при работающем CODESYS), который политика
устраняет. SCADA/OPC UA-клиенты шлюза перенастраиваются на 4841 (CHANGELOG
1.0.5.57, «изменение контракта»).

## 2. Политика автозапуска (kernel × service)

Один дом логики: `/usr/local/sbin/sa02m-kernel-service-guard.sh`
(репо: `etc/sa02m-kernel-service-guard.sh`; ставит и включает
`scripts/01-system.sh`). Policy set: `codesyscontrol codemeter codemeter-logger
codemeter-webadmin`.

| Ядро | Автозапуск CODESYS/CodeMeter | boot-guard | Ручной запуск (панель/systemctl) | docker.service |
|---|---|---|---|---|
| RT (`PREEMPT_RT`) | **выключен** (`apply-policy`) | no-op | **разрешён** | стартует, если проба проходит |
| non-RT | **выключен** (`apply-policy`) | **останавливает** запустившиеся остатки (SysV leftovers) на загрузке | **разрешён** (SMP-джиттер — осознанный выбор пользователя, до следующей загрузки) | **пропущен** (condition-skip), если проба не проходит |

Subcommand-контракты guard-скрипта:

- `apply-policy` — идемпотентная нормализация: `update-rc.d <svc> disable` для
  каждого присутствующего `/etc/init.d/<svc>` (SysV-generated юниты не
  принимают `systemctl disable`) + `systemctl disable` там, где есть настоящий
  unit-файл; отсутствующие службы пропускаются молча. Вызывается установщиком
  (`01-system.sh`, `08-codesys.sh`) и повторным запуском в любой момент.
- `boot-guard` — **только на загрузке** (`sa02m-kernel-service-guard.service`,
  `Type=oneshot` + `RemainAfterExit=yes`, `WantedBy=multi-user.target`,
  ordering-only `After=` на юниты policy-set). На non-RT ядре останавливает
  запустившиеся policy-службы (init.d stop + pkill fallback); на RT — exit 0.
  **Непрерывного принуждения нет** — ручной запуск после загрузки никогда не
  отменяется.
- `docker-capable` — `ExecCondition`-проба для docker
  (`etc/systemd/system/docker.service.d/sa02m-kernel-guard.conf`):
  `modprobe -qn nft_compat` — **проба возможностей**, не сопоставление имени
  ядра (модуль ИЛИ builtin ⇒ docker's iptables-nft path работоспособен).
  Отказ пробы ⇒ docker **не стартует** (fail-closed по capability); юнит
  «пропущен», не «failed». Требует systemd ≥ 243.
- Неизвестный subcommand ⇒ exit 2 (fail-closed usage; вход — только
  фиксированные слова из юнитов/установщика).

## 3. Следствия для UI и установщика

- `scripts/08-codesys.sh` — **install-only**: deb + apt-hold + drop-in; не
  включает и не запускает runtime; проверка — статус dpkg. Rootfs-ветка тоже
  disabled-by-default.
- Веб-панель: «Запустить» CODESYS = **только runtime** (никакого
  `codesys_rc_enable` / `systemctl enable` в start/install-путях —
  закреплено гейтом); вместе с CODESYS поднимается демон CodeMeter
  (runtime-only — иначе молчаливый demo-режим). «Остановить» по-прежнему
  снимает автозапуск (`codesys_rc_disable`) — симметрия сохранена.
- Запуск docker из панели на непригодном ядре: `systemctl start` возвращает 0
  при condition-skip ⇒ UI отвечает ok, строка остаётся «остановлен» при
  следующем опросе, причина — в journal. Принятый failure-mode UX (честнее
  прежнего crash-loop; улучшение сообщений — вне этого выпуска).
- Политика ставится установщиком **fleet-wide** (любое устройство с CODESYS):
  полевому устройству, которому нужен автозапуск CODESYS, потребуется включить
  его вручную один раз.

## 4. Честные границы

- **non-RT сторона проверена на стенде** (2026-07-30, две перезагрузки,
  non-RT ядро 6.1.0-rc6): `sa02m-kernel-service-guard` active;
  `codesyscontrol` + `codemeter` **disabled и inactive** после обеих
  перезагрузок; docker **чисто пропущен** по `ExecCondition` — ноль
  failed-юнитов в обоих прогонах; opcua-шлюз active на `0.0.0.0:4841`.
  Плата возвращается на SSH за 35 с (networking 21 с, шлюз 47 с).
- RT-ядро `6.1.0-rc6-rt4` собрано с docker-набором netfilter (incl.
  `NFT_COMPAT`) и модемными драйверами (kernel-build-фиксы 1.0.5.58) — docker
  **проверен на стенде на RT-профиле** (2026-07-30): активен, NAT-цепочка на
  месте, network create/rm работают, `qmi_wwan` + модемное семейство
  загружаются. Оба профиля (SMP и RT) собраны с этим набором и подтверждены на
  железе в 1.0.5.58.
- `sa02m-grat-arp@.service` (смежное boot-исправление 1.0.5.57): `Type=simple`
  — burst в фоне, multi-user не ждёт ~29 с; гейт закрепляет отсутствие
  регрессии в `Type=oneshot`.

## 5. Задержки загрузки (boot-time holds) — event-юниты не включаются статически

Правило: юнит, привязанный к устройству (`BindsTo=sys-subsystem-net-devices-…`)
и запускаемый udev-событием, **никогда не включается статически**
(`systemctl enable` / `WantedBy=multi-user.target`): статическое включение
затягивает device-job юнита в загрузочную транзакцию, и при отсутствующем
устройстве `multi-user.target` ждёт полный 90-секундный JobTimeout.

Доказательство (стенд, 2026-07-30, два живых захвата `systemctl list-jobs`
во время загрузки): `multi-user.target` держал ~90 с job
`sys-subsystem-net-devices-enx344b50000000.device`, притянутый статически
включённым инстансом `sa02m-modem-dhcp@enx344b50000000.service` (разовое
ручное включение на стенде; установщик инстансы не включает). Событийный
путь уже существовал: `etc/udev/99-modem.rules` стартует/останавливает
`sa02m-modem-dhcp@%k` на add/remove интерфейса. Подозрение на
`sa02m-rs485-roster` как держателя загрузки **опровергнуто** тем же захватом.

Исправление (1.0.5.58): у `sa02m-modem-dhcp@.service` удалена секция
`[Install]` (тот же идиом, что `sa02m-iface-canonical-retry.service`);
`01-system.sh` идемпотентно удаляет устаревшие enable-симлинки
`multi-user.target.wants/sa02m-modem-dhcp@*`. Пины — гейт §6.

Измерено после деплоя фикса (стенд, 2026-07-30, перезагрузка): multi-user
достигнут на **45,3 с** monotonic (до фикса — 98,3 с по live-захвату
`list-jobs`); `systemd-analyze` «Startup finished» — **46,1 с** (до фикса
~1:40); failed-юнитов ноль; docker/networking/opcua активны; enx-device-job
в `list-jobs` отсутствует.

### 5.1. `sa02m-mqtt-opcua` — ранний `READY=1` (декуплинг от загрузки, 1.0.5.60)

Правило: `Type=notify`-юнит с медленной инициализацией шлёт `sd_notify("READY=1")`
**до** этой инициализации, а не после — иначе `multi-user.target` ждёт всю
инициализацию. Для `sa02m-mqtt-opcua.service` построение адресного пространства
OPC UA силами vendored python-opcua на A40i занимает ~17 с суммарно, из них
**~9 с — конструирование объекта `OpcuaServer()`** (стенд: замер на плате),
которое выполняется в `OpcuaGateway.__init__` (`self._server = OpcuaServer()`),
т.е. **до** `start()`. При `Type=notify` эта инициализация держала загрузку
(systemd оставался в `activating` до READY).

Исправление (1.0.5.60): в `opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua.py` вызов
`sd_notify("READY=1")` (+ один явный `sd_notify("WATCHDOG=1")`) перенесён в самое
начало `main()` — сразу после `parse_args()` и **до** `load_config()` /
`gw = OpcuaGateway(cfg)`, т.е. до ~9-секундного конструирования `OpcuaServer()`.
Ранняя позиция внутри `start()` (после конструктора) была бы недостаточна:
журнал загрузки показал, что первая строка `start()` исполнялась уже на 37,9 с, и
`multi-user` всё ещё держался +16 с. Юнит
(`etc/systemd/sa02m-mqtt-opcua.service`) не изменён: `Type=notify`,
`WatchdogSec=60`, `TimeoutStartSec=60` сохранены. READY в t≈0 помечает юнит
`active` сразу, а вся сборка адресного пространства идёт в состоянии RUNNING, не
затягивая загрузку. Watchdog-окно (60 с) c запасом покрывает инициализацию
(один ping в t≈0, далее цикл в `start()` шлёт каждые ~30 с = `WATCHDOG_USEC/2`);
реальный сбой инициализации по-прежнему выбрасывает исключение из
`load_config()`/`__init__`/`start()`, процесс выходит с ненулевым кодом →
`Restart=on-failure`. Модульный `from opcua import Server` (~2,9 с импорт)
остаётся над `main()` — принятый остаток, импорты не реструктурируются. Пин —
гейт §7.

Измерено на стенде (192.168.1.136, 2026-07-30, перезагрузка): до фикса
`multi-user` @39 с, шлюз держал его +20 с (OPC UA-сервер привязывался на 38,5 с,
до multi-user), итог 42,2 с. После корректного фикса: `multi-user` @**30,05 с**;
`systemd-analyze critical-chain | grep -c opcua` = **0** — шлюз ушёл из
критической цепочки multi-user (её теперь держит docker/containerd/network, не
наш шлюз); шлюз `Started`/READY @29,6 с, но уже НЕ гейт; OPC UA-сервер
привязывается @41,6 с, 104 узла наполняются @44,7 с — оба уже **после**
multi-user, в фоне; watchdog-timestamp растёт (жив); ноль failed-юнитов; `4841`
слушает; итог 33,1 с.

**Честная граница:** сокет OPC UA на `4841` не привязан до ~41,6 с — окно ~20 с
после того, как systemd показал юнит `active (running)` (сначала конструируется
`OpcuaServer()`, затем работает `server.start()`). Для фонового телеметрийного
моста это принято: окно самоустраняется, а SCADA/OPC UA-клиент, подключившийся в
этот промежуток, получает connection-refused и повторяет попытку (штатное
поведение клиента). Ничто локальное не потребляет endpoint шлюза на загрузке
(проверено: ни один юнит не упорядочен `After=`/`Wants=` относительно шлюза).
