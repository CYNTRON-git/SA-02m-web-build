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
- RT-ядро `6.1.0-rc6-rt4` **собрано** с docker-набором netfilter — проба
  *ожидаемо* проходит и docker стартует, но это **не проверено на стенде** до
  загрузки RT-ядра (проверка отложена сознательно, 1.0.5.57).
- `sa02m-grat-arp@.service` (смежное boot-исправление 1.0.5.57): `Type=simple`
  — burst в фоне, multi-user не ждёт ~29 с; гейт закрепляет отсутствие
  регрессии в `Type=oneshot`.
