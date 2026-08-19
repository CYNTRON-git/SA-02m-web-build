# Контракт: режим обновления установщика (refresh) и политика стеков

Домашний адрес правил «что установщик вправе сделать с состоянием служб и со
сторонними стеками при повторном прогоне». Машинная грамматика (имена флагов,
переменных, юнитов, значений файла) — на английском (`PROTOCOL.md`
invariant 5); пояснения — на русском. Введён решением Оператора 2026-08-19
(A + C: режим в установщике + персистентная политика стеков).

Валидирующие тесты: quality-строки `installer-svc-helpers`
(`scripts/dev/test-installer-svc-helpers.sh` — таблица решений и вердиктов),
`installer-svc-policy-gate` (`.ai-dev/quality/checks/installer-svc-policy-gate.sh`
— статический гейт «все модули через helpers»), `service-ctl-policy-write`
(`scripts/dev/test-service-ctl-policy.sh` — записи политики из веб-панели).

---

## 1. Гарантия

**Повторный прогон установщика на настроенной плате не меняет ничего, что
решил оператор.** Конкретно, `install.sh --refresh` (а значит и офлайн-обёртка
`scripts/offline-full-update.sh` — по умолчанию в
refresh) гарантирует:

1. **Службы — никогда не расширяем.** Остановленная / выключенная /
   замаскированная оператором служба остаётся ровно такой; работавшая —
   перезапускается на свежем коде (один раз, и только если код действительно
   обновился после снятия состояния); отсутствовавший юнит получает дефолт
   первой установки своего модуля **только если это стек sa02m** — никогда
   сторонний. Правило never-widen для **прикладных** (app) юнитов действует
   **в обоих режимах** (решение Оператора Q1) — и в полной установке тоже.
2. **Сторонние стеки (Node-RED, CODESYS, MPLC, Docker, KLogic) в refresh
   никогда не ставятся, не переустанавливаются, не обновляются и не
   включаются**, кроме случая «уже установлен И не `disabled`» — и тогда
   обновляется только sa02m-надстройка (drop-in'ы, unit-обёртка, плагины).
   Без apt/pip/npm, без vendor-инсталлятора, без распаковки payload, без
   создания пользователей и репозиториев.
3. **Стеки sa02m — это изделие:** отсутствующие ставятся и в refresh (путь
   1.0.3.34 → текущий); их apt/pip-зависимости ставятся при наличии сети;
   оффлайн ⇒ `WARN` + деградация, никогда не жёсткий выход.
4. **Удаление оператором помнится** через перезагрузки, отсутствие юнита и
   любой путь установки (полный `install.sh`, обёртка, кнопка в панели)
   — файлом `/etc/sa02m_stacks.conf` (§3).
5. **Установка на чистую плату не меняется** (режим по умолчанию — `full`):
   те же состояния юнитов, те же модули.

## 2. Режим (`SA02M_INSTALL_MODE`)

- `install.sh --refresh` ⇔ `SA02M_INSTALL_MODE=refresh`; `--with-optional` ⇔
  `SA02M_WITH_OPTIONAL=1` (явное согласие на сторонние стеки; перекрывает и
  персистентный `disabled` — решение Q3). Пусто/отсутствует ⇒ `full`;
  всё прочее ⇒ `ERR` + exit 2 (fail closed).
- Классы юнитов (один дом реализации — `scripts/lib.sh`):
  - **app** — прикладной юнит, чьё состояние принадлежит оператору
    (flasher, mosquitto, мост, телеметрия, шлюз, alice, devices, roster,
    cloud-agent, mplc4, nodered, docker). Правила: п.1.1. Вызов —
    `sa02m_svc_capture <unit>` ДО установки файлов юнита,
    `sa02m_svc_apply <unit> app <on|enabled|off> [norestart] [--stack=<ID>]`
    ПОСЛЕ. `<on|enabled|off>` — дефолт первой установки.
  - **infra** — платформенный юнит, которым владеет установщик (nginx,
    fcgiwrap, networking, watchdogs, chrony, fake-hwclock, ModemManager,
    журнал, sa02m-системные юниты): в любом режиме гарантированно снята маска
    и включён автозапуск (+ `start`/`restart` по флагу) — решение Q2;
    исторические маски на них — наши же ошибки образов, установщик их чинит.
    Вызов — `sa02m_svc_apply <unit> infra [start] [restart]`.
- Свидетели против ложного «нового юнита»: `absent` требует трёх подтверждений
  (`is-enabled` rc=1 с пустым выводом, нет unit-файла на диске, менеджер
  отвечает на `is-active`); зависший D-Bus читается как `timeout` — ничего не
  расширяется. Свидетель `ActiveEnterTimestampMonotonic` гасит повторный
  рестарт, когда ctl/vendor-инсталлятор уже перезапустил службу сам.
- Пакеты: `sa02m_pkg_install_tier required|optional|thirdparty` и
  `sa02m_pip_install` — tier `thirdparty` в refresh без `--with-optional`
  ничего не ставит (INFO); `required`/`optional` без изменений (оффлайн ⇒
  WARN + пропуск). Статический гейт запрещает в модулях сырые
  `systemctl enable|start|unmask|restart|reload-or-restart` и inline
  `apt-get/pip/npm install`; ужесточающие глаголы (`stop`, `disable`, `mask`,
  `reset-failed`, `daemon-reload`, `reload`, `try-restart`) остаются сырыми.

## 3. Файл политики `/etc/sa02m_stacks.conf`

Один дом кода: `etc/sa02m-stacks-policy.sh` (POSIX sh; source'ится `lib.sh`
из дерева и мягко — `sa02m-web-service-ctl.sh` из
`/usr/local/lib/sa02m-stacks-policy.sh`, куда его кладёт `03-webserver.sh`).

```
# SA-02m third-party stacks policy. Written by install.sh and sa02m-web-service-ctl.sh; hand-editable.
# STACK_<ID>=present|absent|disabled   disabled = removed/refused by the operator: never auto-installed.
STACK_CODESYS=absent
STACK_DOCKER=present
STACK_KLOGIC=absent
STACK_MPLC=present
STACK_NODERED=disabled
```

- ID: `NODERED CODESYS MPLC DOCKER KLOGIC` — только сторонние (стеки sa02m —
  изделие: из панели не удаляются, их состояние покрывает захват юнитов;
  ключей для них нет). root:root 0644; под `/etc/sa02m_*.conf` ⇒ уже в
  `PRESERVE_PATHS` update-runner'а.
- Файл никогда не `source`-ится — парсится awk с закрытым множеством значений;
  неизвестное значение читается как `absent`, то есть ровно как отсутствие
  файла: порча файла не может расширить поведение сильнее безфайлового
  (в refresh `absent` ⇒ `skip-absent`; потерять можно только `disabled`, а на
  это и так нужен root).
- **Писатели:** (1) `install.sh` — миграция `sa02m_stack_policy_derive
  --write`: создаёт файл по живому состоянию **только если его нет** (решение
  оператора никогда не перезаписывается); (2) модули 07/08/09/12 — `present`
  после успешной установки; (3) ctl `cmd_install` ⇒ `present` /
  `cmd_uninstall` ⇒ `disabled` при rc 0 (кнопки «Установить»/«Удалить»).
  `start`/`stop` файл **не** трогают: он хранит *наличие*, а run-state
  сохраняет захват юнитов. Под `SA02M_ROOTFS_BUILD` никто не пишет (образ не
  запекает политику; первый прогон на устройстве выводит её сам). Запись
  атомарна (tmp + mv), равное значение — no-op.

## 4. Вердикт модуля (`sa02m_stack_verdict <ID>`)

| policy | installed | full | refresh | `--with-optional` (любой режим) |
|---|---|---|---|---|
| disabled | любое | `skip-disabled` | `skip-disabled` | `install` |
| present/absent | да | `install` (свой same-version skip действует) | `overlay` | `install` |
| present/absent | нет | `install` | `skip-absent` | `install` |

Каждый сторонний модуль (07 Node-RED, 08 CODESYS, 09 MPLC, 12 Docker) читает
вердикт **первым действием** — до `useradd`, `dpkg -i`, vendor `install.sh`,
apt и curl (гейт (e)). `overlay` = только sa02m-надстройка: 07 — ничего
(INFO с установленной версией), 08 — drop-in + apt-hold + apply-policy,
09 — плагины + unit-обёртка (cmp-gated) + capture/apply, 12 — daemon.json +
alternatives + capture/apply (`norestart`: рестарт docker убивает контейнеры).
Каждый отказ называет дорогу назад: кнопка «Установить» в панели или
`--with-optional`. Плюс idempotency-метки: MPLC —
`/opt/mplc4/.sa02m-payload-version` (пишут 09 и ctl `mplc4_install`, один
формат), Node-RED — версия из `package.json` против пина, CODESYS — версия
dpkg против версии .deb.

## 5. Смежные контракты

- `docs/contracts/kernel-conditional-services.md` §3 — MPLC4 включён по
  умолчанию **при первой установке**; при обновлении состояние оператора
  восстанавливается точно (в т.ч. после vendor-инсталлятора, который сам
  делает enable+start — restore-exact в `sa02m_svc_apply`).
- `docs/contracts/ethernet-iface-naming.md` — `sa02m-iface-canonical` остаётся
  enable-only (`sa02m_svc_apply … infra` без `start`).
- Порт-lease (`sa02m-domain.md ## Subsystems`) — установщик делает только
  упорядоченные рестарты, lease флашера не берёт.
- Подписанный update-runner (`etc/sa02m-update-runner.sh`) не менялся: refresh
  — это эквивалент его политики «файлы + рестарт активных» на стороне
  установщика; плата, обновлённая runner'ом и затем `--refresh`, видит одну и
  ту же сервисную политику.
