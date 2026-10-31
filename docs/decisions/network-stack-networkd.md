# Исследование: перевод Ethernet SA-02m с ifupdown на systemd-networkd

**Дата:** 2026-10-31. **Статус: исследование; решения о миграции нет.**
Рекомендация 6 из [network-recovery-standard-linux.md](network-recovery-standard-linux.md).
Решение — за Оператором; документ даёт ему факты и цену.

**Вопрос.** Можно ли заменить ifupdown и самодельную лестницу восстановления eth0
(`etc/fix-eth.sh`, `etc/net-watchdog.sh`, `sa02m-eth-coldboot.sh`,
`sa02m-wait-carrier.sh`, `sa02m-dns-ensure.sh`, `sa02m-routes-ensure.sh`,
`etc/99-lan-recovery.rules`) на `systemd-networkd` (+ `systemd-resolved`), что из
этого уйдёт, что останется и сколько стоит перевод уже стоящих плат.

**Метки достоверности.**

- **[код]** — прочитано в репозитории, `main` на e4f0e0b2 (1.0.7.23), со ссылкой `файл:строка`.
- **[исходник]** — прочитано в исходниках systemd v255 (`src/network/networkd-link.c`,
  тег `v255` на GitHub). На плате это поведение не испытывалось.
- **[док]** — страницы man systemd v255 (`systemd.network(5)`, `networkd.conf(5)`,
  `networkctl(1)`) и `ip-route(8)`. На плате не испытывалось.
- **[стенд]** — прочитано на стенде 192.168.1.135 31.10.2026 по SSH, только чтение:
  SA-02m, 1 Ethernet, веб 1.0.7.23, Armbian 26.5.1 noble. Службы не менялись,
  networkd не запускался. Другие платы и SA-02m-2 не смотрелись.

## Ответ

**По исходникам и документации systemd 255 (на плате не испытано)** networkd
закрывает второй корень из анализа: правило udev на смену линка у нас не
срабатывает, а networkd узнаёт о смене линка из netlink. При появлении линка он
снова ставит статические адреса и маршруты (§1). Если опыт Н1 (§2.1) это
подтвердит, станут не нужны `sa02m-wait-carrier`, `sa02m-routes-ensure`, правка
1.0.7.22 и опрос `net-watchdog` ради маршрутов и адреса.

**Первый корень networkd не закрывает.** На холодной загрузке с вставленным кабелем
линк поднимает только `ip link set down`/`up` [стенд, BUGLOG 1.0.7.23]. Перезапуска
интерфейса при отсутствии линка ни в документации, ни в прочитанном коде networkd
нет. Поэтому «толчок» интерфейса и перезапуск автосогласования для позднего кабеля
остаются в любом стеке (§2).

**Перевод дорогой, и главная цена — не в networkd.** Без доработок не переедут
четыре вещи:

1. **DNS.** `systemd-resolved` на стенде не установлен. По симуляции apt его
   установка удаляет `resolvconf` и обновляет systemd, udev и ещё 7 пакетов
   [стенд]. Офлайн такую установку сделать нечем: обновление веб apt не запускает
   [код].
2. **KLogic.** Его IP применяет хук `post-up` в `interfaces.d` [код]. В networkd
   хуков нет [док], а `networkd-dispatcher` на стенде не установлен [стенд].
3. **`inet-failover.sh`.** Он переставляет маршрут по умолчанию командами
   `ip route del`/`add` [код]. networkd по умолчанию снимает маршруты, которых нет
   в его файлах `.network` [док].
4. **Обновление веб.** OTA и офлайн-пакет не могут записать `/etc/systemd/network/`,
   снять маску с юнита или поставить пакет [код].

**Рекомендация (§5):** сейчас не переводить. Сначала нужны два опыта на стенде
(§2.1). Если Оператор захочет переходить, то только на networkd **без** resolved.
Первыми — новые образы и полная установка. Платы с KLogic остаются на ifupdown.
Платы, обновляемые только по OTA, переводить не раньше, чем появится
автоматический откат по таймеру.

## Что стоит на стенде [стенд]

| Компонент | Состояние |
|---|---|
| systemd | 255 (`255.4-1ubuntu8.16`) |
| `systemd-networkd` | бинарник есть, входит в пакет `systemd` (`dpkg -S`); `systemd-networkd.service` замаскирован ссылкой на `/dev/null` от 30.11.2023 — той же даты, что `armbian-default.yaml`. Вывод по дате (не проверен): маску поставила сборка образа; наш установщик маскирует только `systemd-networkd-wait-online` (`scripts/01-system.sh:714-718`) [код] |
| `/etc/systemd/network/` | пусто |
| netplan | `netplan.io` установлен; `/etc/netplan/armbian-default.yaml` содержит только `renderer: NetworkManager`, интерфейсов в нём нет. Значит, штатный стек **этого** образа — NetworkManager через netplan, а не networkd. NetworkManager маскирует наш установщик (`scripts/01-system.sh:700-707`) [код] |
| `systemd-resolved` | **не установлен** (в noble это отдельный пакет); кандидат `255.4-1ubuntu8.17`, то есть новее установленного systemd |
| `networkd-dispatcher` | не установлен; `apt-get -s` ставит его одного, зависимости уже есть |
| `ifplugd` | не установлен |
| DNS | `resolvconf` 1.84; `/etc/resolv.conf` — обычный файл, не ссылка |
| DHCP-клиенты | `isc-dhcp-client` (его вызывают ifupdown и `sa02m-modem-dhcp@`), `dhcpcd-base` |
| Списки apt | есть (стенд в сети); на офлайн-плате их может не быть |

`apt-get -s install systemd-resolved` на стенде: `Remv resolvconf`, обновление
`systemd`, `systemd-sysv`, `systemd-dev`, `libsystemd-shared`, `libsystemd0`,
`libpam-systemd`, `udev`, `libudev1`, `libnss-myhostname` до 8.17 и установка
`systemd-resolved`. Причина — зависимость
`systemd (= 255.4-1ubuntu8.17)` в метаданных пакета.

## 1. Что networkd покрывает сам

| Поведение сейчас | Кто делает сейчас | networkd | Метка |
|---|---|---|---|
| Статический IP без линка | ifupdown ставит адрес при `ifup` без проверки линка; `sa02m-wait-carrier` ждёт линк перед `ifup`, чтобы `ifup` не упал на шлюзе | `ConfigureWithoutCarrier=yes`. По умолчанию `false`: без линка networkd **не** настраивает интерфейс — для контроллера со статикой задавать обязательно | [док] |
| Конфигурация остаётся при потере линка | ifupdown адрес не снимает; ядро при `ip link set down` удаляет маршруты IPv4 | `IgnoreCarrierLoss=`. Если не задан, берётся значение `ConfigureWithoutCarrier=`, поэтому `ConfigureWithoutCarrier=yes` включает и его. При `false` networkd снимает статику и DHCP | [док] |
| Маршруты возвращаются после появления линка | `sa02m-routes-ensure` разбирает строки `post-up` (`docs/contracts/boot-network-routes.md`); правка 1.0.7.22 в `fix-eth.sh`; опрос `net-watchdog` раз в 30 с, первый проход через 60 с (`etc/net-watchdog.sh:9,28`) | При появлении линка у интерфейса в состоянии `configuring`/`configured` `link_carrier_gained()` вызывает `link_request_static_configs()` — заново запрашивает статические адреса и маршруты (`networkd-link.c:1570-1577`). networkd узнаёт о линке из netlink, а не опросом | [исходник] |
| Маршруты из `post-up ip route add …` | Строка в `eth0.conf`; на стенде `post-up ip route add 192.168.10.0/24 via 192.168.1.100 dev eth0 \|\| true` [стенд] | Секция `[Route]`: `Destination=`, `Gateway=`, `Metric=`, `GatewayOnLink=`. `Gateway=` в `[Network]` — краткая запись такой секции | [док] |
| DNS из настроек панели | `dns-nameservers` → `/etc/network/if-up.d/000resolvconf` → `resolvconf`; если `ifup` не дошёл до хуков, `sa02m-dns-ensure.sh` (`usr/local/sbin/sa02m-dns-ensure.sh:5-26`) | `[Network] DNS=`. Этот ключ читает `systemd-resolved`; сам networkd `/etc/resolv.conf` не пишет. **Без resolved DNS из `.network` никуда не попадает** — нужна своя склейка с `resolvconf` (§3) | [док] |
| DHCP на eth1 и метрика его маршрута | `iface eth1 inet dhcp` + `metric 100`; хук `dhclient-exit-hooks.d/eth1-default-route` (`scripts/02-network.sh:109-138`) | `DHCP=ipv4`, `[DHCPv4] RouteMetric=` (по умолчанию 1024), `UseRoutes=`, `UseDNS=` (по умолчанию включены). Встроенный DHCP-клиент; хуки dhclient больше не вызываются | [док] |
| DHCP на USB-модеме | udev `add` → `sa02m-modem-dhcp@%k.service` → `dhclient` (`etc/udev/99-modem.rules:26,31`, `etc/systemd/sa02m-modem-dhcp@.service`); метрику 100 ставит `dhclient-exit-hooks.d/sa02m-modem-metric` | `.network` с `[Match] Name=enx* usb* wwan*` (глобы поддерживаются), `DHCP=ipv4`, `RouteMetric=100`. Шаблон `eth[1-9]*` из хука модема на SA-02m-2 совпадает с eth1 — в `.network` так делать нельзя | [док] |
| Переключение маршрута по умолчанию по наличию интернета (eth0 → eth1 → модем) | `inet-failover.sh` из `net-watchdog` раз в 30 с: проверяет HTTP 204 через каждый интерфейс и переставляет метрику (`etc/inet-failover.sh:28-31,114-118`) | **Не покрывает.** У networkd метрики постоянные, наличие интернета он не проверяет. Хуже того, `ManageForeignRoutes=` (по умолчанию `yes`, `networkd.conf`) снимает маршруты, которых нет в `.network` (кроме маршрутов с протоколом `kernel`, а `dhcp`/`static` — только при соответствующем `KeepConfiguration=`); маршрут от `ip route add` по умолчанию получает протокол `boot` (`ip-route(8)`) и под исключение не попадает. Маршрут, переставленный `inet-failover.sh`, для networkd чужой; при переконфигурации networkd вернёт свой `Gateway=` рядом с ним. Нужен новый механизм (§3), а не перенос как есть | [док] [код] |
| Индикатор eth0 | `sa02m-eth0-led-poll.service` читает `carrier` раз в секунду (`etc/sa02m-eth0-led-poll.sh`); `net-watchdog` дублирует синхронизацию (`etc/net-watchdog.sh:49-52`) | От стека **не зависит**: опрос идёт по `/sys/class/net/*/carrier`. При переходе сохраняется как есть; синхронизация из `net-watchdog` уходит вместе с ним | [код] |
| `network-online.target` | `systemd-networkd-wait-online` замаскирован: его задание не завершалось и блокировало очередь systemd (`scripts/01-system.sh:709-718`) | `RequiredForOnline=` по умолчанию `yes` для каждого интерфейса с `.network`. eth1 без кабеля и модем держали бы wait-online вечно. Нужно `RequiredForOnline=no` для eth1/модема или оставить wait-online замаскированным | [док] [код] |

**Что это значит для лестницы.** Из обходов, разобранных в анализе (раздел
«Разбор по компонентам»), networkd заменяет по построению `sa02m-wait-carrier`,
`sa02m-routes-ensure`, правку 1.0.7.22, а также `ifup`/`ifdown` и восстановление адреса
в `fix-eth.sh` (`etc/fix-eth.sh:340-371,395-429`, кроме пачки ARP внутри). Опрос `net-watchdog` ради адреса и
маршрутов тоже становится не нужен; drop-in'ы `sa02m-carrier-wait.conf` к
`networking`/`ifup@` теряют смысл вместе с ifupdown. `sa02m-dns-ensure` уходит
только вместе с resolved. Не уходят:

- перезапуск интерфейса на холодной загрузке и перезапуск автосогласования после
  исчерпания бюджета (`etc/fix-eth.sh:296-308`) — §2;
- пачка gratuitous ARP после появления связи (`etc/fix-eth.sh:373-388` и её копия
  после восстановления IP с `:430`; 15 с, раз за загрузку — чтобы Windows-клиенты
  быстрее вышли из ARP FAILED) и юнит `sa02m-grat-arp@.service`, который тянет
  `ifup@%i.service` (`Wants=`/`After=`, `etc/systemd/system/sa02m-grat-arp@.service:6-7`).
  В документации networkd такой функции нет [док]; нужен свой хозяин, а юнит — без
  зависимости от `ifup@`;
- `inet-failover.sh`;
- мёртвые правила udev — их стоит убрать в любом случае (рекомендация 3 анализа).

Переход не проверялся ни на одной плате. Строка «маршруты возвращаются» прочитана в
исходнике и требует опыта (§2.1, опыт Н1).

## 2. Что не уходит: линк на холодной загрузке

Первый корень из анализа (BUGLOG 1.0.7.23): на стенде с вставленным кабелем линка нет
272,9 с; один `mii-tool -r` на 94,5 с его не поднял; поднимает только
`ip link set down`/`up` (`etc/fix-eth.sh:280-285`). Причина не установлена. К
сетевому стеку это не относится: пространство пользователя не начинает согласование
PHY, его начинает драйвер при открытии интерфейса. networkd открывает интерфейс тем
же `IFF_UP`, что и ifupdown. Вывод не проверен: может оказаться, что при раннем
подъёме из networkd линк ведёт себя иначе (опыт Н1, шаг 2).

Что меняется под networkd:

- **Перезапуск интерфейса остаётся**, но сжимается до одноразового юнита: через
  N секунд после `systemd-networkd` проверить `carrier`; при `0` — `ip link set down`,
  пауза, `up`, с бюджетом попыток как у `MAX_LINK_CYCLES` (`etc/fix-eth.sh:16`). Маршруты
  и DNS он больше не трогает: при `ConfigureWithoutCarrier=yes` networkd сам вернёт
  статику, когда линк появится (§1). Это тот же «толчок», что сейчас делает
  `sa02m-eth-coldboot.sh`, без поясов DNS и маршрутов. Для eth1 на SA-02m-2
  `sa02m-eth1-coldboot.sh` делает другое — `ip link set up` и `ethtool -r`, без
  `down` (`usr/local/sbin/sa02m-eth1-coldboot.sh:12-14`); его стоит перенести как
  есть. Вывод по исходнику, не по
  опыту: внешний `ip link set down` networkd видит как потерю линка; при
  `IgnoreCarrierLoss=yes` он конфигурацию не снимает, а после `up` с линком снова
  запрашивает статику.
- **Поздний кабель** (кабель вставлен после исчерпания бюджета). Сейчас сторож на
  каждом проходе перезапускает автосогласование (`mii-tool -r`,
  `etc/fix-eth.sh:296-308`; по комментарию там, без этого шага кабель после пятого
  цикла оставлял плату без сети до перезагрузки, стенд 28.10). В прогоне 30.10 линк
  появился без перезапуска интерфейса, но прогон не различает, поднял его этот
  перезапуск или сам PHY (`docs/contracts/boot-network-routes.md` §8). Значит, при
  переходе этот шаг **нельзя убрать вместе с `net-watchdog`**: он переезжает в тот же
  юнит, что и толчок, — периодический перезапуск автосогласования, пока
  `carrier=0` и интерфейс поднят. Маршруты после линка сейчас возвращает опрос
  сторожа (через 12 с в том прогоне); под networkd — событие netlink, без опроса
  [исходник].
- Если причина корня 1 найдётся (рекомендация 2 анализа), толчок станет не нужен в
  любом стеке.

### 2.1 Опыты, нужные до любого решения (для стенда; эта сессия их не ставила)

Сессия только читала стенд: изменения служб на нём ведёт другая сессия. Опыты
передаются родительской сессии. Каждый — с самооткатом, как в BUGLOG 1.0.7.23.

- **Н1 — networkd на холодной загрузке.** Остановить и отключить `networking`,
  `net-watchdog`, `fix-eth@*`, `sa02m-eth-coldboot`. Снять маску с `systemd-networkd`.
  Положить `/etc/systemd/network/10-eth0.network`, повторяющий `eth0.conf` стенда:
  `Address=192.168.1.135/24`, `Gateway=192.168.1.1`, `[Route] Destination=192.168.10.0/24`,
  `Gateway=192.168.1.100`, `ConfigureWithoutCarrier=yes`, `LinkLocalAddressing=no`,
  `[Link] RequiredForOnline=no`. Кабель вставлен, холодная загрузка. Записать:
  (1) адрес и оба маршрута до появления линка; (2) появляется ли линк без перезапуска
  интерфейса — под ifupdown за 272,9 с не появился; (3) после ручного
  `ip link set eth0 down; sleep 3; ip link set eth0 up` — вернулись ли оба маршрута без
  наших скриптов и за какое время после «Link is Up»; (4) кабель вынут и вставлен —
  остались ли адрес и маршруты. Откат: маска на networkd, `networking` и лестница
  включены обратно, перезагрузка, проверка `ip route`.
- **Н2 — чужой маршрут.** В той же конфигурации переставить маршрут по умолчанию
  так, как это делает `inet-failover.sh` (`ip route del default dev eth0; ip route add
  default via 192.168.1.1 dev eth0 metric 300`), затем `networkctl reconfigure eth0` и
  цикл линка. Записать, какие маршруты по умолчанию остались. Ответ решает судьбу
  переключения на модем (§3).

## 3. Кто опирается на ifupdown и `interfaces.d`

| Потребитель | Что предполагает | Что нужно под networkd | Метка |
|---|---|---|---|
| Панель: чтение — `config.cgi:48-69` | Разбирает `iface … inet static`, `address`, `netmask`, `gateway`, `dns-nameservers` из `/etc/network/interfaces.d/<if>.conf` | Читать `.network` (другая грамматика: `Address=ip/prefix`, без `netmask`) или общий источник — свой файл настроек, из которого генерируется конфиг стека | [код] |
| Панель: запись — `apply.cgi:385-424` + слияние с чужими строками `apply.cgi:50-130` | Перегенерирует станс ifupdown, сохраняя чужие строки (KLogic) | Новый писатель `.network`; правило слияния пересмотреть — чужих строк в `.network` не бывает | [код] |
| Панель: применение — `apply.cgi:325-356` | `sudo ifdown`/`ifup`, при ошибке — `systemctl restart networking` | `networkctl reload` + `networkctl reconfigure <if>` (оба есть в 255) | [код] [док] |
| Привилегии — `etc/sudoers.d/sa02m-www:25-28,36-37,71` | `www-data` может вызывать `sa02m-iface-conf-write.sh`, `sa02m-ensure-eth1-dhcp-hook.sh` (хук dhclient для eth1, `apply.cgi:419`), `ifup`/`ifdown` для четырёх имён, перезапуск `networking` и `fix-eth.service` (кнопки `restart.cgi:39,42`), `sa02m-conf-rm.sh` (удаление конфига соседнего имени, `apply.cgi:197-215`) | Новые закреплённые строки для `networkctl`; изменение границы безопасности — через ревью безопасности и гейт `sudoers-pin-contract` | [код] |
| Писатель и удалятель с правами root — `etc/sa02m-iface-conf-write.sh:41-126`, `etc/sa02m-conf-rm.sh` | Разрешены четыре пути в `interfaces.d`; построчная проверка грамматики ifupdown против внедрения `pre-up` (аудит B1) | Новые разрешённые пути в `/etc/systemd/network/` и новая проверка содержимого. Исполняемых директив в `.network` нет, так что проверка проще, но писать её заново | [код] |
| Модель угроз — `docs/threat-model.md:634` (строка A2→S1/S2) | Описывает внедрение через писатель `interfaces.d` | Переписать строку под новый писатель | [код] |
| Разрешение имени — `lib_net_iface.sh:7-23` | Если живого интерфейса нет, имя берётся из файла `<if>.conf` | Тот же резервный путь по `.network` | [код] |
| Состояние и отладка — `status.cgi:866`, `ssh_debug.cgi:130-134,251` | Читают `eth*.conf`, показывают `networking` | Читать `.network`, показывать `networkctl status` | [код] |
| Установщик — `scripts/02-network.sh:57-138,192-209,280-333,383,410-521,553-866,873-879` | Пишет `/etc/network/interfaces` и `eth*.conf`, хуки dhclient, юниты лестницы, drop-in'ы, `sa02m-grat-arp@`, включает `networking.service`, мигрирует имена в `interfaces.d`, чинит `gateway`/`dns`, применяет адрес сразу через `ifup` | Переписать модуль: генерация `.network`, снятие маски с networkd, маска на `networking`, перенос существующей настройки (§4) | [код] |
| Сброс к заводским — `etc/sa02m-factory-defaults/lists/wipe.list:9`, шаблоны `templates/etc/network/interfaces.d/eth{0,1}.conf`, `sa02m-factory-reset-runner.sh:1104-1149` | Стирает `interfaces.d/*.conf` и ставит шаблоны ifupdown | Шаблоны `.network` и новый путь в списке разрешённых. Подписанный пакет `.sa02m-defaults` перевыпустить | [код] |
| Резервная копия и восстановление — `etc/sa02m-web-backup.sh:64-65`, `etc/sa02m-restore-backup.sh:84`; упаковщик заводских настроек `scripts/pack-factory-defaults.py:53-54` кладёт шаблоны `interfaces.d` в подписанный пакет | Сохраняет `interfaces.d`; восстановление разрешает только этот каталог | Сохранять и восстанавливать и `/etc/systemd/network/` | [код] |
| Другие сетевые скрипты — `etc/fix-eth1-internet.sh:184-230,368,546-549,815-827` (ремонт eth1: пишет `interfaces.d`, смотрит на `networking`/`ifup@`, вызывает `ifdown`/`ifup`), `usr/local/sbin/sa02m-eth1-coldboot.sh` (eth1 на SA-02m-2, выходит, если нет `interfaces.d/<if>.conf`) | Стек — ifupdown | Перевести или вывести из обращения | [код] |
| Порядок юнитов относительно `networking.service` — `etc/systemd/sa02m-restore-dtb.service:5`, `etc/systemd/sa02m-iface-canonical.service:26`, `etc/systemd/system/sa02m-iface-canonical-retry.service:13`, `etc/systemd/sa02m-pre-start.service:5` (`Before=`), `etc/systemd/system/sa02m-dns-ensure.service:7` (`After=`) | Привязка к юниту ifupdown | Под networkd эти строки ничего не упорядочивают: перенести на `systemd-networkd.service`. Для `sa02m-iface-canonical` порядок важен — имя должно устояться до того, как networkd сопоставит `.network` (если сопоставлять по имени) | [код] |
| Именованные `.link` в `/etc/systemd/network/` — установщик удаляет `10-*`/`11-*` для eth/end (`scripts/02-network.sh:358-361`, `scripts/01-system.sh:658`) | Каталог сейчас считается мусорным для этих имён | Генератор `.network` не должен использовать эти имена | [код] |
| Сборка корневой ФС — `tools/debian-rootfs/create-sa02m-rootfs.sh:119,268` | Ставит пакеты `ifupdown`, `isc-dhcp-client` | Решить, оставлять ли ifupdown в образе ради отката (§4, п. 5 — нужен) | [код] |
| Обновление веб: что сохраняется — `sa02m-update-runner.sh:84-87,1068`, `validate_package.py:42-45`, `scripts/offline-update-deploy-map.json:327` | `interfaces.d` — пользовательская настройка, обновление её не трогает | Добавить `/etc/systemd/network/` во все копии списка | [код] |
| Обновление веб: куда можно писать — `sa02m-update-runner.sh:685-694,1053-1062`, `validate_package.py:74-89`, четвёртая копия — в упаковщике `scripts/pack-offline-update.py` (гейт `ota-dst-allowlist-parity`) | Только `/etc/systemd/system/sa02m-*`, `/usr/local/{sbin,lib,libexec}`, точный путь `fix-eth.sh` и т. п. `/etc/systemd/network/` в списке нет; `fix-eth@.service` и `net-watchdog.service` — не `sa02m-*`, их OTA тоже не обновит | Либо расширить список (изменение границы безопасности), либо генерировать `.network` на плате своим помощником из `/usr/local/sbin` (§4) | [код] |
| Обновление веб: что умеет с юнитами — манифест `stop_before_apply`, `restart`, `enable`, `restart_if_*` (`validate_package.py:335-340`), без apt (`sa02m-update-runner.sh:5`) | — | Снять маску с networkd, замаскировать `networking`, поставить resolved — нечем. Только помощник на плате | [код] |
| Образ — `tools/imaging/firstboot-overlay/` (копии `fix-eth.sh`, `net-watchdog.sh`, `sa02m-eth-coldboot.sh`, юнит; гейт `firstboot-overlay-parity`) | Лестница ifupdown в образе | Пересобрать накладку образа | [код] |
| KLogic — `docs/contracts/ethernet-iface-naming.md` §4–5, `lib_net_iface.sh:31-36` | Хук `post-up /home/klogic/adjust-eth0` — **единственный** путь, которым KLogic применяет IP; KLogic `set-route` делает `route del default` | В networkd хуков нет; `networkd-dispatcher` не установлен [стенд]. Адреса и маршруты, которые поставит скрипт KLogic, для networkd чужие. Реалистичный вариант — `Unmanaged=yes` для интерфейса под KLogic (networkd его не трогает, но тогда и не восстанавливает), либо KLogic-платы остаются на ifupdown | [код] [док] [стенд] |
| Канонические имена — `docs/contracts/ethernet-iface-naming.md` §1 | `ifup eth0` падает, если интерфейс ещё `end0` | `.network` можно сопоставлять по `MACAddress=` и не зависеть от переименования. Юнит канонических имён остаётся ради панели и KLogic | [док] [код] |
| Переключение маршрута — `etc/inet-failover.sh`, вызывается только из `net-watchdog.sh:44-47` | Свободно переставляет маршруты по умолчанию | Ему нужен свой юнит с таймером (сейчас он живёт в `net-watchdog`) и новый способ влиять на маршрут, совместимый с networkd. Возможны `ManageForeignRoutes=no` (тогда networkd держит свой маршрут рядом с чужим) или отказ от `Gateway=` в `.network` для интерфейсов под переключением. Выбор — после опыта Н2 | [код] [док] |
| Гейты качества — `.ai-dev/quality/tools.json` | Есть строки, которые проверяют ifupdown, лестницу восстановления, писатель конфигов `interfaces.d`, строки sudoers, заводские настройки и списки путей обновления веб | Переход затрагивает строки всех этих классов: их переписывают, расширяют на новые пути или выводят из обращения вместе с кодом. Точный перечень собирается из реестра при планировании миграции — реестр единственный дом списка строк и меняется каждый выпуск (`docs/agent-rules/web-code-rigor.md`, «Reviewer floor summary») | [код] |
| Контракты — `boot-network-routes.md`, `boot-network-dns.md`, `ethernet-iface-naming.md` §3–5 | Описывают обходы ifupdown | Переписать: часть гарантий становится свойством networkd | [код] |

## 4. Перевод стоящих плат и откат

**Ограничения поля.** Многие платы не выходят в интернет: ни apt, ни внешнего
источника пакетов. Обновляются они офлайн-пакетом `.sa02m`, а у него нет установки
пакетов и операций с масками юнитов (§3). Ошибка в сетевом стеке на удалённой
площадке делает плату недоступной, и починить её может только человек на месте.

**Путь, который проходит через эти ограничения** (схема, не план):

1. **Не трогать resolved.** DNS остаётся на `resolvconf`. Серверы из настроек панели
   передаёт в него небольшая склейка на плате — то, что сейчас делает ступень 1
   `sa02m-dns-ensure.sh`, только по событию networkd, а не после `ifup`. Пакеты не
   нужны: networkd уже есть в `systemd` [стенд].
2. **Генератор на плате.** `/usr/local/sbin/sa02m-net-render` (путь в списке
   разрешённых OTA) строит `/etc/systemd/network/*.network` из того источника, который
   пишет панель. Если источником остаются `interfaces.d/eth*.conf`, панель на первом
   этапе можно не трогать: генератор переводит станс ifupdown в `.network`.
3. **Переключатель.** Ключ в `/etc/sa02m_network.conf` (сохраняется обновлением)
   выбирает стек. Юнит `sa02m-net-stack.service` (путь `sa02m-*` разрешён) при загрузке
   приводит маски в соответствие с ключом: networkd или `networking` + лестница.
4. **Автооткат.** После переключения — сторожевой таймер. Если за N минут нет
   адреса, маршрута по умолчанию или ответа шлюза, юнит возвращает ifupdown и
   перезагружает плату. Тот же принцип, что у проверки здоровья обновления
   (`docs/contracts/web-update.md`, «Жизненный цикл apply»). Без этого пункта
   переводить OTA-платы нельзя.
5. **Откат** — тот же ключ в обратную сторону. `interfaces.d` при переходе не
   удаляются, поэтому возврат обратим без потерь. Платы с KLogic генератор не
   переводит: интерфейс с хуком `/home/klogic/` остаётся на ifupdown, а при
   смешанной схеме для него пишется `Unmanaged=yes`.

**Цена** (качественно; часов никто не мерил): новые помощник-генератор, юнит
переключения со сторожем автоотката и склейка DNS; новый писатель для панели (или
генератор поверх старого писателя); новые строки sudoers и ревью безопасности;
шаблоны сброса к заводским и перевыпуск подписанного пакета; четыре места со
списком сохраняемых путей и четыре копии списка назначений; юнит толчка с
перезапуском автосогласования и пачкой gratuitous ARP (§1–2); новый дом для
`inet-failover.sh`; накладка образа и сборка корневой ФС; строки гейтов
перечисленных в §3 классов;
три контракта и строка модели угроз; опыты Н1/Н2 и проверка на SA-02m-2 (eth1, DHCP, модем).
До выхода нового `.sa02m`-пакета парк живёт с двумя стеками, и каждую сетевую правку
придётся делать и проверять дважды.

## 5. Рекомендация и компромиссы

| Вариант | Что даёт | Цена и риски |
|---|---|---|
| A. Оставить ifupdown и лестницу, убрать мёртвые правила udev (рекомендация 3 анализа) | Ничего не ломает | Второй корень остаётся; реакция на линк — опрос раз в 30 с |
| B. ifupdown + `ifplugd` (рекомендация 5 анализа) | Реакция на линк по событию; уходят `wait-carrier`, `routes-ensure`, `dns-ensure`, опрос сторожа | Пакет `ifplugd` (universe) офлайн не поставить; при вынутом кабеле `ifdown` снимает статический адрес; KLogic живёт как сейчас; `inet-failover`, перезапуску автосогласования и синхронизации индикатора нужен новый дом взамен `net-watchdog` |
| C. networkd без resolved | Реакция по netlink; статика держится без линка; маршруты возвращаются сами; пакеты не нужны | Всё из §3–4; переключение на модем нужно перепроектировать; KLogic остаётся на ifupdown; два стека в парке |
| D. networkd + resolved | То же, что C, плюс DNS без склейки | Установка resolved удаляет `resolvconf` и обновляет systemd/udev; офлайн-платам недоступна |

**Рекомендация:** сейчас — A. Решение о C принимать после опытов Н1 и Н2, а
причину корня 1 искать параллельно (рекомендация 2 анализа). Если Н1 покажет, что
networkd возвращает маршруты сам, а Н2 — что переключение на модем можно сделать
без борьбы с ним, разумная последовательность такая: C — сначала новые образы и
полная установка; OTA-платы — только когда появится автооткат (§4, п. 4); KLogic-платы — на ifupdown.
Вариант D не рекомендуется, пока в парке есть офлайн-платы. Вариант B дешевле C, но
теряет статический адрес при вынутом кабеле — для контроллера со статикой это хуже,
чем сейчас.

Ни один вариант не убирает толчок интерфейса на холодной загрузке. Его убирает
только найденная причина корня 1.

## Источники

- Анализ: [network-recovery-standard-linux.md](network-recovery-standard-linux.md);
  `docs/bugs/BUGLOG.md`, записи 1.0.7.21, 1.0.7.22, 1.0.7.23.
- Контракты: `docs/contracts/boot-network-routes.md`, `boot-network-dns.md`,
  `ethernet-iface-naming.md`, `web-update.md`.
- systemd v255: `systemd.network(5)` (`ConfigureWithoutCarrier=`, `IgnoreCarrierLoss=`,
  `KeepConfiguration=`, `[Route]`, `[DHCPv4]`, `DNS=`, `Name=`, `RequiredForOnline=`,
  `LinkLocalAddressing=`), `networkd.conf(5)` (`ManageForeignRoutes=`), `networkctl(1)`
  (`reload`, `reconfigure`, `up`, `down`); исходник `src/network/networkd-link.c`
  (`link_carrier_gained`, `link_carrier_lost`, `link_reconfigure_impl`), тег `v255`.
- Стенд 192.168.1.135, 31.10.2026, только чтение: `systemctl --version`,
  `dpkg-query`, `dpkg -S`, `systemctl show`/`is-enabled`, `ls -l /etc/systemd/system`,
  `/etc/netplan`, `/etc/network`, `apt-cache policy`, `apt-cache show`, `apt-get -s install`.
