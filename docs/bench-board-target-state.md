# Контракт: целевое состояние стендовых плат (эталон 192.168.1.136)

Единственный дом **операторского** целевого состояния служб и карточек
Cloud / Alice / Services для стенда: как на живой плате `192.168.1.136`
после refresh на **1.0.6.37**. Следующие платы приводятся к этой таблице,
а не к дефолтам установщика.

Машинная грамматика (юниты, флаги, пути, команды) — на английском
(`PROTOCOL.md` invariant 5); пояснения — на русском.

Доступ SSH/веб **не дублировать** — `docs/AGENTS_SSH_AND_DEVICE_ACCESS.md`
и `tools/sa02m-device.env`. Политика refresh / never-widen —
`docs/contracts/installer-refresh-policy.md`. Прошивка образа — только
USB/imageUSB или `tools/imaging/ssh-flash-safe.sh`; **запрещён** `dd` на
живую eMMC.

**Эталон не клонировать как идентичность.** Serial / `device_id` / секреты /
сертификаты 1.136 на другие платы не копировать. Копируется только
enable/active и флаги карточек.

---

## 1. Снимок эталона (read-only)

| Поле | Значение |
|---|---|
| Captured | `2026-09-07T11:36:25Z` (probe only: `cat` / `systemctl is-*` / `list` / `curl` GET) |
| Host | `192.168.1.136`, hostname `SA-02m`, variant `sa02m-1eth` |
| VERSION | `/var/www/network_config/VERSION` → **`1.0.6.37`** |
| deployed_commit | `/var/lib/sa02m-web-build/deployed_commit` → `app-1.0.6.37` |
| Kernel | `Linux SA-02m 6.1.0-rc6 #1 SMP … armv7l` |
| Serial (identity, не копировать) | `1655415360918543` |
| `systemctl --failed` | пусто |
| nginx `:9999` | HTTP 200 (`/` и `/login.html`) |
| 1.136 mutated? | **нет** |

На `/opt/sa02m-alice/` уже лежит overlay фикса Socket.IO timeout
(репо-коммит `398c338`; `sio_connection.py` содержит `wait_timeout` /
`SIO_CONNECT`). Refresh на 1.0.6.37 приносит тот же код штатно.

---

## 2. Как обновлять следующие платы

`install.sh --refresh` **не сходится к этой таблице**. Never-widen сохраняет
то, что уже решил оператор: выключенное остаётся выключенным, включённое —
включённым. Поэтому:

1. Убедиться, что на плате **нет** идущего `install.sh`. Второй установщик
   не запускать.
2. Довести код до **1.0.6.37**: `sudo ./install.sh --refresh` (или офлайн-обёртка
   из `docs/deployment.md`). Не `--with-optional`, если не нужно ставить
   отсутствующие сторонние стеки.
3. **После** refresh явно выставить юниты и флаги из §3–§6.
4. Образ eMMC — только внешний носитель (imageUSB / `prepare-flash-media.sh`).
   Никогда `dd of=/dev/mmcblk2` на живой системе.

---

## 3. Службы: желаемое = снято с 1.136 сейчас

Колонка **Services card** — то, что отдаёт
`sa02m-web-service-ctl.sh list` (карточка «Службы»).

| Unit | enabled | active | Services card | Примечание |
|---|---|---|---|---|
| `sa02m-alice-client` | disabled | inactive | alice `user_disabled=true` | Яндекс-клиент **выкл** |
| `sa02m-alice-config` | disabled | inactive | — | вместе с клиентом |
| `sa02m-cloud-control` | enabled | active | — | карточка Cloud, профиль `cloud` |
| `sa02m-cloud-agent` | enabled | active | — | enrolled, heartbeat |
| `sa02m-cloud-frpc` | enabled | active | — | туннель `running` |
| `sa02m-cloud-heartbeat` | disabled | inactive | — | legacy; агент заменяет |
| `sa02m-frpc` | disabled | inactive | — | legacy WireGuard-эры |
| `sa02m-rules` | enabled | active | — | |
| `sa02m-flasher` | enabled | active | — | |
| `sa02m-devices-api` | enabled | active | — | |
| `sa02m-devices-logger` | enabled | active | — | |
| `sa02m-modbus-mqtt` | enabled | active | mqtt-bridge ON | |
| `sa02m-telemetry` | enabled | active | mqtt-telemetry ON | |
| `sa02m-serial-gateway` | enabled | active | — | |
| `sa02m-mqtt-opcua` | disabled | inactive | — | extra OFF |
| `sa02m-mqtt-snmp` | disabled | inactive | — | extra OFF |
| `docker` | disabled | inactive | docker `user_disabled=true` | |
| `docker.socket` | disabled | inactive | — | выключать **вместе** с docker |
| `nodered` | disabled | inactive | node-red `user_disabled=true` | extra OFF |
| `codesyscontrol` | disabled | inactive | codesys `user_disabled=true` | extra OFF |
| `klogic` | disabled | inactive | klogic `user_disabled=true` | extra OFF |
| `mplc4` | enabled | active | mplc4 ON | `active=exited` у обёртки init.d — норма |
| `mosquitto` | enabled | active | mosquitto ON | |
| `nginx` | enabled | active | — | infra |
| `fcgiwrap` | enabled | active | — | `fcgiwrap.socket` masked — норма |
| `sa02m-userspace-watchdog` | enabled | active | — | |
| `sa02m-failure-monitor` | enabled | active | — | |
| `net-watchdog` | enabled | active | — | |
| `sa02m-watchdog-feed` | disabled | inactive | — | не поднимать |

`/etc/sa02m_stacks.conf` на эталоне: все `STACK_*=present`
(CODESYS, DOCKER, KLOGIC, MPLC, NODERED). Стеки **установлены**, юниты
кроме MPLC — `user_disabled`. Не писать `disabled` в stacks.conf, если
задача только выключить службу (иначе установщик не вернёт стек без
`--with-optional`).

---

## 4. Alice / Yandex

Цель карточки: **«клиент выключен»**, юнит снят.

| Флаг / артефакт | Эталон 1.136 |
|---|---|
| `client_enabled` | `false` |
| `cloud_control_enabled` | `true` (это **не** Яндекс; гейт `sa02m-cloud-control`) |
| unlink markers (`unlinked_at`, `unlinked_reason`, `unlinked_reason_text`) | **PRESENT** (`unlinked` / `controller_unlink`, stamp `2026-09-07T11:10:34Z`) |
| `/var/lib/sa02m-alice/device.crt.pem` | ABSENT |
| `/var/lib/sa02m-alice/pending_claim.json` | ABSENT |
| `/var/lib/sa02m-alice/ca.crt.pem` | ABSENT |
| `/run/sa02m-alice/status.json` | `state=disabled`, `client_enabled=False`, `message=yandex client disabled (client_enabled=false)` |
| `/run/sa02m-alice/status-cloud.json` | `state=connected`, `cloud_control_enabled=True` |

**Мина 1.136:** «Отвязать» само по себе **оставляет клиент включённым**.
После unlink обязательно:

```bash
# флаги — тот же дом, что карточка (config_store)
# client_enabled=false; юнит снять
systemctl disable --now sa02m-alice-client sa02m-alice-config
```

Порядок на следующей плате, если Яндекс ещё привязан:

1. Отвязать (карточка / API unlink).
2. Выставить `client_enabled=false`.
3. `systemctl disable --now sa02m-alice-client sa02m-alice-config`.
4. Проверить: нет `device.crt.pem` / `pending_claim.json`; status
   `state=disabled`; карточка «клиент выключен».

`cloud_control_enabled=true` не трогать при выключении Яндекса — это
отдельный юнит.

---

## 5. Cloud cyntron

Эталон **подключён**. Секреты / токены / `device_secret` не снимать и не
копировать.

| Источник | Ключи (без секретов) |
|---|---|
| `/run/sa02m-cloud-status.json` | `state=active`, `identity=present`, `tunnel=running`, `serial=1655415360918543`, `device_id=sa02m-1655415360918543` |
| `/etc/sa02m-cloud/agent.conf` | `[cloud] enrolled=true`, `api_url=https://cloud.cyntron.ru/api/v1`, `server_host=cloud.cyntron.ru`; `[device] web_port=9999` |

Желаемая поза оператора на следующих платах (своя enrollment, свой serial):

- `sa02m-cloud-agent` + `sa02m-cloud-frpc` + `sa02m-cloud-control` —
  **enabled + active**;
- `cloud_control_enabled=true`;
- если плата уже в облаке — оставить; если нет — привязать **эту** плату,
  не клонировать 1.136.

Это **расходится с дефолтом установщика**: `scripts/06-alice.sh` ставит
`sa02m-cloud-control` как `app off`. Refresh сам его **не включит**.
После refresh, если юнит ещё disabled: enable + `cloud_control_enabled=true`.

---

## 6. Docker и прочие extra

```bash
systemctl disable --now docker docker.socket
systemctl disable --now nodered codesyscontrol klogic
systemctl disable --now sa02m-mqtt-opcua sa02m-mqtt-snmp
systemctl disable --now sa02m-frpc sa02m-cloud-heartbeat sa02m-watchdog-feed
```

Дефолт Docker с 1.0.6.37 — `app off`. На плате, где Docker ещё ON,
refresh его не выключит — нужен явный `disable --now`.

---

## 7. Чем эталон отличается от дефолта установщика

| Тумблер | First-install default | Золото 1.136 |
|---|---|---|
| `sa02m-alice-client` / `client_enabled` | off | off + **unlink** (маркеры есть, certs нет) |
| `sa02m-cloud-control` / `cloud_control_enabled` | off | **ON / connected** |
| `docker` + `docker.socket` | off (с 1.0.6.37) | off (`user_disabled`) |
| `nodered` | on, если стек поставлен | **off** (`user_disabled`) |
| `codesyscontrol` | off (kernel guard) | off (`user_disabled`) |
| `klogic` | зависит от стека | **off** (`user_disabled`) |
| `mplc4` | on, если стек поставлен | on |
| CPU profile | — | `SA02M_CPU_PROFILE=adaptive` (не часть службы) |

---

## 8. Чеклист следующих 5 плат

1. Нет живого установщика. Довести VERSION до **1.0.6.37** через
   `install.sh --refresh` (never-widen; не стартовать второй install).
2. Если Яндекс привязан — unlink, затем `client_enabled=false` и
   `systemctl disable --now sa02m-alice-client sa02m-alice-config`.
3. `systemctl disable --now docker docker.socket`.
4. Выключить extra: `nodered`, `codesyscontrol`, `klogic`,
   `sa02m-mqtt-opcua`, `sa02m-mqtt-snmp`, legacy `sa02m-frpc` /
   `sa02m-cloud-heartbeat`.
5. Включить/оставить cloud как на эталоне: agent + frpc + cloud-control
   ON, `cloud_control_enabled=true` (своя привязка, не serial 1.136).
6. Не трогать рабочие sa02m-службы эталона (flasher, rules, mosquitto,
   мост, telemetry, devices-*, serial-gateway, nginx/fcgiwrap, mplc4,
   watchdogs).
7. Пост-проверка: VERSION `1.0.6.37`; `curl` `:9999` → 200;
   `systemctl --failed` пуст; карточка Алисы «клиент выключен»;
   Docker inactive; cloud-control `state=connected` если плата в облаке.
8. SSH только через `py -3 tools/ssh/sa02m_remote.py exec` (не голый `ssh`).

---

## 9. Повторный съём эталона

Если 1.136 снова эталон — **только чтение**. Скрипт: `systemctl is-enabled`
/ `is-active` по таблице §3, флаги из
`/etc/sa02m-alice/sa02m-alice-client.conf` (не значения токенов), имена
файлов в `/var/lib/sa02m-alice/`, ключи `/run/sa02m-cloud-status.json`,
`systemctl --failed`, `VERSION`. Не `enable`/`disable`/`start`/`stop` на
1.136.
