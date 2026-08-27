# Развёртывание веб-интерфейса SA-02m

Единственный дом процедуры деплоя. Деплой выполняется **строго по этому
документу**, не импровизируя (PROTOCOL.md инвариант 4;
`.ai-dev/procedures/deployment.md`). Изменения репозитория попадают на
устройство только через описанные здесь пути — правка файла на устройстве в
обход git запрещена.

Машинно-читаемое (команды, пути, имена служб) — на английском; пояснения —
по `docLanguage` (ru).

---

## Что где лежит

- Репозиторий = overlay файловой системы устройства: `www/` → `/var/www`,
  `etc/` → `/etc`, `opt/` → `/opt` (см. `install.sh`).
- Веб-корень на устройстве: `/var/www/network_config`.
- nginx на устройстве слушает **:9999 (обычный HTTP)**, за ним fcgiwrap + Bash CGI.
- Демон прошивальщика: `sa02m-flasher.service`, unix-socket
  `/run/sa02m-flasher/flasher.sock`.
- Маркер развёрнутого коммита: `/var/lib/sa02m-web-build/deployed_commit`.

## Пути деплоя

| Путь | Когда | Чем |
|---|---|---|
| **www-only** | изменения только в `www/` (frontend + CGI) | `scripts/update-www-only.sh` |
| **full install** | новое устройство, или менялись `etc/`/`opt/`/systemd/демон | `install.sh` (на настроенной плате — `install.sh --refresh`, «Режим обновления» ниже) |
| **web-update (OTA)** | штатное самообновление с интернетом | вкладка «Обновление» → GitHub (`web_update_*.cgi`, semver); apply через shared runner при наличии |
| **offline package** | обновление без интернета платы **≥ 1.0.5.60** (runner ≥ 1.0.5.66 — `MIN_VERSION`/`MIN_UPDATER` в `scripts/pack-offline-update.py`) | вкладка «Обновление» → файл `.sa02m`; packer на ПК: `python scripts/pack-offline-update.py` |
| **offline full update** | плата **< 1.0.5.60** (старый updater, `.sa02m` не примется) или любой разрыв `etc/`/`opt/` с текущим релизом, без интернета | полный архив `origin/main` в `/tmp` платы + `scripts/offline-full-update.sh` — «Офлайн-вариант» в разделе «Полный деплой» (запускает `install.sh --refresh`) |
| ~~self-upgrade bridge~~ **(ОТМЕНЁН — не использовать)** | плата **< 1.0.5.75** без интернета/по SSH | **вместо моста — «offline full update» выше.** Мост переписывал боевые version-ветки force-push'ем и ОТКЛОНЁН: `docs/decisions/no-force-push-version-branches.md` |
| **vendor-payload** | доставка/обновление опционального стека (Node-RED) вне релиза веб-интерфейса | процедура «Доставка vendor-payload Node-RED» ниже |

Состояние updater: `/var/lib/sa02m-update` (runner `/usr/local/libexec/sa02m-update-runner`,
ключи `/etc/sa02m-update/trusted-keys/`). Bootstrap релиза N ставит runner/keys/units
через `scripts/03-webserver.sh` / `update-www-only.sh` (`[ -f ]`-гарды); применение
`.sa02m` — с N+1. Подробности формата: `docs/OFFLINE_UPDATE_PACKAGE_V1.md` (когда появится).

`update-www-only.sh` синхронизирует `www/` → `/var/www/network_config`, чинит
права (CGI 755, static 644, owner `www-data`), пишет маркер коммита,
перезапускает fcgiwrap. nginx перезапуска не требует. Идемпотентен.

**Важно:** `update-www-only.sh` разворачивает `www/` и — если `etc/` есть в
дереве рядом — часть helper-скриптов/юнитов из `etc/` (идемпотентно, по
`[ -f … ]`-гардам), включая bootstrap updater (`opt/sa02m-update`, libexec runner).
Он НИКОГДА не разворачивает демон прошивальщика (`opt/sa02m-flasher/`) и его
tmpfiles-юнит. Если релиз менял демон/`opt/sa02m-flasher/` — нужен `install.sh`
(полный) ИЛИ отдельная доставка `opt/` + рестарт `sa02m-flasher`. Для чисто-www
деплоя `etc/` намеренно не несут (см. шаг 2).

---

## Процедура www-only деплоя (проверена: 1.0.5.0 → 192.168.1.136, 2026-07-13)

Устройство private-repo обычно НЕ имеет доступа к origin, а рабочий git-чекаут
на нём может отсутствовать. Тогда файлы доставляются с рабочей машины по SSH.

### Предусловия
- Версия репозитория согласована: `python scripts/sync-app-version.py --check`
  (на Windows `python`, не сломанный `python3`-stub).
- Известны SSH-доступ и host-key устройства.

### Шаги
1. **Бэкап** текущего веб-корня на устройстве (для отката):
   ```
   ssh root@<dev> 'tar czf /root/www-backup-$(date +%Y%m%d-%H%M%S).tgz -C /var/www network_config'
   ```
2. **Доставка** `www/` и `scripts/` в staging на устройстве. Два
   равнозначных транспорта:
   - `pscp -r www scripts root@<dev>:/root/sa02m-deploy-<ver>/`;
   - архив из git (гарантированно LF, без CRLF-сюрпризов Windows-чекаута):
     `git archive --format=tar.gz -o deploy.tar.gz HEAD www/network_config scripts`,
     загрузка по SFTP (paramiko / WinSCP), на устройстве
     `tar xzf … -C /root/sa02m-deploy-<ver>` (практика деплоев 1.0.5.10–12).
   `etc/` намеренно не несём для www-only — тогда скрипт пропускает
   helper/sudoers/systemd-инсталлы (они уже provisioned), и деплой ограничен
   синхронизацией www — минимальный риск на работающем сервере.
3. **Снять CRLF** со скриптов (репозиторий часто синхронизируется с Windows):
   ```
   ssh root@<dev> "sed -i 's/\r$//' /root/sa02m-deploy-<ver>/scripts/*.sh"
   ```
4. **Запуск** санкционированного скрипта:
   ```
   ssh root@<dev> 'bash /root/sa02m-deploy-<ver>/scripts/update-www-only.sh'
   ```
5. **Проверка деплоя:** `VERSION`, `APP_VERSION` в `app.js`, `?v=` в
   `index.html` — все равны новой версии; owner `www-data:www-data`.
6. **Функциональная проверка (обязательна):**
   - Страница грузится: `curl -o /dev/null -w '%{http_code}' http://<dev>:9999/`
     (ожидается 200), `/login.html` 200, `/static/js/app.js` содержит новый
     `APP_VERSION`.
   - Авторизация прошивальщика: реальная сессия → `/api/flasher/status` 200,
     без cookie → 401.
   - Прошивальщик работает: остановить MQTT-мост (`systemctl stop
     sa02m-modbus-mqtt`), `POST /api/flasher/scan` по нужному COM, дождаться
     `state:"done"`, вернуть MQTT (`systemctl start sa02m-modbus-mqtt`).
7. **Откат** при проблеме: распаковать бэкап из шага 1 обратно в `/var/www`,
   `chown -R www-data:www-data /var/www/network_config`, `systemctl restart
   fcgiwrap`.

### Замечания по устройству-стенду 192.168.1.136
- nginx слушает `:9999` (HTTP), не 443.
- RS-485: MQTT-мост `sa02m-modbus-mqtt` держит COM-порт (по конфигу
  `/etc/sa02m-modbus-mqtt.yaml`); для скана/прошивки порт освобождается
  остановкой моста или port-lease самого прошивальщика.
- На стенде подключены два модуля MR-02m на COM4 @115200: адрес 6 (`6AI6AO`),
  адрес 8 (`4DO6DI`).
- Выхода в интернет со стенда нет: в прогоне полного деплоя 2026-08-05 модуль
  Node-RED упал на `registry.npmjs.org`, остальная установка прошла до конца.
  С тех пор этот случай — не ошибка: без payload'а и без сети модуль пишет
  `WARN` и выходит с кодом 0 (см. Предусловия полного деплоя). Чтобы Node-RED
  на стенде всё-таки ставился, нужен payload — процедура ниже.
- Превращение этой платы в мастер-образ для клонирования — процедура
  «Подготовка золотого образа (мастер для клонирования)» ниже (проверена на
  этом стенде 2026-08-18).

---

## Доставка только моста MQTT (`opt/sa02m-modbus-mqtt`)

Когда релиз менял только мост (без нового systemd/tmpfiles): бэкап
(`cp -a /opt/sa02m-modbus-mqtt /root/opt-bridge-backup-<date>`), копия
файлов из staging (`cp -a <staging>/opt/sa02m-modbus-mqtt/. /opt/sa02m-modbus-mqtt/`),
проверка (`python3 -m py_compile /opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py`),
рестарт (`systemctl restart sa02m-modbus-mqtt`), контроль
(`systemctl is-active` + журнал без ERROR). Каталог `tests/` инсталлер и эта
процедура на устройство не несут. Практика деплоев 1.0.5.10–12.

## Полный деплой (`install.sh`)

Нужен для нового устройства или когда релиз менял `etc/`/`opt/`/демона/tmpfiles.
Запускается на устройстве из полного дерева репозитория: `sudo bash install.sh`.
Идемпотентен (повторный прогон не портит настроенное устройство — установщик и
есть путь обновления). Детали и порядок модулей — `install.sh` и `scripts/0*.sh`.

Полного чекаута на устройстве обычно нет — дерево доставляется с рабочей машины
по SSH (процедура ниже, проверена: 192.168.1.136, 2026-08-05).

### Режим обновления (`--refresh`) и политика стеков

Полный контракт (гарантия, классы юнитов, схема `/etc/sa02m_stacks.conf`,
таблица вердиктов) — `docs/contracts/installer-refresh-policy.md`; здесь —
рабочая выжимка.

**Гарантия:** повторный прогон на настроенной плате не меняет ничего, что
решил оператор. `sudo ./install.sh --refresh` (офлайн-обёртка обновления
использует этот режим по умолчанию):

- остановленные/выключенные/замаскированные оператором службы остаются такими;
  работавшие перезапускаются на свежем коде (один раз);
- сторонние стеки (Node-RED, CODESYS, MPLC, Docker) **не ставятся и не
  включаются**; уже установленный и не отключённый оператором получает только
  sa02m-надстройку; никаких apt/pip/npm для сторонних пакетов;
- стеки sa02m ставятся/обновляются как обычно (зависимости — при наличии
  сети, оффлайн ⇒ WARN и деградация);
- «Удалить» в панели записывает `disabled` в `/etc/sa02m_stacks.conf` — такой
  стек не вернёт ни один путь установки. Дорога назад: кнопка «Установить»
  в панели или `--with-optional` (ставит/обновляет сторонние стеки, включая
  отключённые).

Правило «не расширять состояние прикладных служб» действует и в полной
установке (`./install.sh` без флагов): полный режим отличается только тем, что
ставит отсутствующие сторонние стеки (кроме `disabled`). Установка на чистую
плату не меняется.

**Приёмочная проверка** (снимок до/после; повторный прогон ⇒ пустой diff,
кроме создаваемого при первом прогоне `/etc/sa02m_stacks.conf`):

```
bash scripts/dev/board-state-snapshot.sh > /root/before.txt
bash /tmp/sa02m-upd/scripts/offline-full-update.sh
bash scripts/dev/board-state-snapshot.sh > /root/after.txt
diff /root/before.txt /root/after.txt
```

### Предусловия
- Изменение уже смержено в `main` — деплой ведётся из смерженного коммита, не
  из рабочего дерева (шаг 2).
- **Источник опциональных стеков: payload или интернет.** Node-RED ставится из
  локального payload'а (`/opt/vendor-installers/nodered/`, приоритетно) либо из
  сети. Проверить до запуска, если рассчитываете на сеть:
  ```
  ip route            # должен быть default-маршрут
  cat /etc/resolv.conf  # непустой
  ```
  **Нет ни payload'а, ни интернета — это не ошибка установки:** модуль пишет
  `[WARN] Node-RED: нет ни vendor-payload …, ни доступа к registry.npmjs.org`
  и выходит с кодом 0, установка идёт дальше (та же логика, что у MPLC).
  Прежней строки `[ERR] Нет доступа к registry.npmjs.org` больше нет — при
  проверке лога (шаг 7) ищите `WARN`, а не только `ERR`.
  Чтобы стек не ставился вовсе — пропустить модуль явно:
  `SA02M_SKIP_NODERED=1 bash install.sh`. Так же отключаются остальные
  опциональные стеки: `SA02M_SKIP_MQTT`, `SA02M_SKIP_GATEWAY`,
  `SA02M_SKIP_CODESYS`, `SA02M_SKIP_MPLC`, `SA02M_SKIP_DOCKER`
  (перечень — `install.sh`).
- **MPLC runtime на git-archive деплое — обязательная доставка payload'а.**
  Runtime-тарбол MPLC (`mplc4.tar.gz`, ~18 MB) под vendor-EULA и **не в git**
  (`.gitignore`), поэтому в `git archive` он не попадает. Чтобы MPLC поставился
  на полном деплое, **до** запуска `install.sh` разложить vendor-дроп на
  устройство:
  ```
  pscp -r .\MPLC4\cyntron\* root@<dev>:/opt/vendor-installers/mplc4/
  ```
  (полный рецепт pscp/plink — `docs/vendor-integrations.md` →
  «Ручная загрузка на боевое устройство»). `09-mplc.sh` выберет **новейший**
  из доставленного payload'а и `MPLC4/cyntron`; без payload'а шаг MPLC штатно
  пропускается (`[WARN]`, не ошибка). Плагины ЦИНТРОН приезжают из git
  (`firmware/mplc4/`) — их доставлять отдельно не нужно.
- Известны SSH-доступ и host-key устройства.

### Шаги
Шаги 2-3 выполняются **на рабочей машине**, остальные — **на устройстве**.
`<topic>` — короткое имя задачи, общее для каталога доставки и файла лога.

1. **(устройство) Бэкап `/etc` целиком.** Установщик правит настройки по всему
   дереву `/etc` и переписывает в том числе файлы дистрибутива —
   `/etc/network/interfaces`, `fake-hwclock.service`, `resolv.conf.d/head` и
   `base`, `docker/daemon.json`, конфиг сайта nginx; дописывает
   `chrony.conf`; кладёт свои drop-in'ы в `system.conf.d`/`user.conf.d`,
   `sudoers.d`, `tmpfiles.d`, `sshd_config.d` и генерируемые
   `/etc/sa02m_*.conf|env` (там же пароли MQTT и веб-доступа). Поимённый
   список быстро устаревает — снимаем каталог целиком:
   ```
   tar czf /root/etc-backup-$(date +%Y%m%d-%H%M%S).tgz -C / etc
   ```
   Веб-корень — бэкапом из шага 1 www-only процедуры.
   **Что откат покрывает:** файлы, пришедшие из репозитория, возвращает сам
   репозиторий (он и есть их дом); переписанные настройки дистрибутива —
   только этот архив. **Чего НЕ покрывает:** установленные пакеты и
   опциональные стеки (Node-RED, Docker, CODESYS/MPLC) — это откат
   конфигурации, а не удаление софта.
2. **(рабочая машина) Архив из смерженного коммита** (не из рабочего дерева) —
   всё дерево, а не только `www/`: полной установке нужны `etc/`, `opt/`,
   `scripts/`, `install.sh`:
   ```
   git fetch origin && git archive --format=tar.gz -o deploy.tar.gz origin/main
   ```
   `git fetch` обязателен: после мержа на форже локальный `origin/main` ещё
   указывает на прежний коммит, архив соберётся молча и без ошибок, размер
   сойдётся — и на устройство уедет предыдущая версия.
   В прогоне 2026-08-05 архив весил ~2.3 MB (растёт вместе с репозиторием —
   ориентир, не константа; сверяется размер загрузки, шаг 3).
3. **(рабочая машина) Загрузка по SFTP** (paramiko). Хост и учётные данные из
   `tools/ssh/sa02m_remote.py` (`connect()`, `DEFAULT_HOST`/`DEFAULT_USER`/
   `DEFAULT_PASS`) — не дублировать их в скрипте деплоя:
   ```python
   import sys; sys.path.insert(0, "tools/ssh")
   import sa02m_remote as R
   cli = R.connect(R.DEFAULT_HOST, R.DEFAULT_USER, R.DEFAULT_PASS)
   sftp = cli.open_sftp()
   sftp.put("deploy.tar.gz", "/root/deploy.tar.gz")
   print(sftp.stat("/root/deploy.tar.gz").st_size)
   ```
   **Сверить размер на устройстве с локальным** — расхождение означает обрыв
   загрузки; повторить, не распаковывая.
4. **(устройство) Распаковка:**
   ```
   mkdir -p /root/sa02m-deploy-<topic> && tar xzf /root/deploy.tar.gz -C /root/sa02m-deploy-<topic>
   ```
   Снимать CRLF на этом пути НЕ нужно: `.gitattributes` фиксирует
   `*.sh text eol=lf`, поэтому архив из git приходит с LF — тот же довод, что
   в шаге 2 www-only процедуры. Шаг 3 www-only процедуры (`sed -i 's/\r$//'`)
   обязателен только при доставке другим транспортом (`pscp` из
   Windows-чекаута) — и тогда включает сам `install.sh`, не только
   `scripts/*.sh`.
5. **(устройство) Pre-flight:** `bash -n /root/sa02m-deploy-<topic>/install.sh`.
6. **(устройство) Запуск в фоне, с логом** — не в переднем плане:
   ```
   cd /root/sa02m-deploy-<topic> && nohup bash install.sh > /root/install-<topic>.log 2>&1 &
   ```
   С пропуском опционального стека (Предусловия) присваивание ставится
   **перед `nohup`** — это префикс к командному слову:
   ```
   cd /root/sa02m-deploy-<topic> && SA02M_SKIP_NODERED=1 nohup bash install.sh > /root/install-<topic>.log 2>&1 &
   ```
   После `nohup` присваивание станет для него именем программы, и запуск
   упадёт с `nohup: failed to run command` — установка не начнётся.
   Равнозначный вариант: `nohup env SA02M_SKIP_NODERED=1 bash install.sh …`.
   Далее опрашивать лог короткими отдельными вызовами (`tail -n 30
   /root/install-<topic>.log`). Причина: `install.sh` трогает сетевой стек —
   SSH-канал переднего плана может оборваться посреди установки; кроме того у
   `tools/ssh/sa02m_remote.py exec` таймаут канала ~120 s, и он висит на
   фоновом потомке, пока тот держит stdout. Наблюдавшаяся длительность на
   стенде — около 11 минут.
7. **(устройство) Проверка после установки:**
   - `systemctl --failed` — список пуст;
   - `curl -s -o /dev/null -w '%{http_code}' http://<dev>:9999/` — 200;
   - развёрнутый `/var/www/network_config/VERSION` равен ожидаемой версии;
   - **MPLC-драйверы** (если MPLC ставился): `md5sum /opt/mplc4/mplc_cyntron.so`
     начинается на `f6ae6026` (186356 B — сборка с публикацией лицензии, с
     1.0.6.5; до неё было `bf412755`/180356 B);
     `/opt/mplc4/mplc_protocol_fast_modbus.so` присутствует (`9eba65e3`,
     226276 B); в логе нет WARN про ненайденный
     `mplc_protocol_fast_modbus.so`. Отпечаток обязан совпадать с
     `firmware/mplc4/` — этот каталог авторитетен, и расхождение означает, что
     на устройстве осталась прежняя сборка;
   - **Алиса выключена по умолчанию** (первый install): `systemctl is-enabled
     sa02m-alice-client sa02m-alice-config` = `disabled`, `is-active` =
     `inactive`; веб-карточка Алисы при этом отвечает. На повторном install
     ранее включённая оператором Алиса **сохраняется** включённой;
   - **Core-службы восстановлены**: устройство, где `sa02m-modbus-mqtt` был
     активен до установки, после re-install снова `is-active` (на свежем коде);
     mosquitto/nginx/fcgiwrap активны; ничего работавшего не осталось
     остановленным/выключенным;
   - **sudoers чист**: `visudo -c` завершается кодом 0 (в логе строка
     «агрегатная проверка visudo -c пройдена»);
   - `grep -E '\[(ERR|WARN)\]' /root/install-<topic>.log` — просмотреть каждую
     строку. Только `ERR` недостаточно: упавший опциональный стек `install.sh`
     логирует как `[WARN] <NN-модуль>.sh завершился с ошибкой` (он запускает
     их через `|| log WARN`), и установка при этом идёт дальше. **Пропущенный
     стек виден ТОЛЬКО по `WARN`** — Node-RED без payload'а и без интернета
     штатно завершается кодом 0 со строкой `[WARN] Node-RED: нет ни
     vendor-payload …`; прежней `[ERR] Нет доступа к registry.npmjs.org`
     больше не будет.
   - Установленный Node-RED (если ставился): в логе есть
     `[OK] Node-RED: потоки запущены`. Открытый порт 1880 сам по себе ничего
     не доказывает — рантайм, поднявшийся с остановленными потоками, слушает
     точно так же.

### Офлайн-вариант: архив + `offline-full-update.sh`

Для платы, которой штатные пути не подходят: версия **< 1.0.5.60** (её
updater не примет `.sa02m` — пороги `MIN_VERSION`/`MIN_UPDATER` в
`scripts/pack-offline-update.py`) или любой разрыв `etc/`/`opt/` с текущим
релизом, когда www-only недостаточно. Почему такая плата не обновляется сама:
её старая проверка обновлений сравнивается с **собственной** версионной веткой,
а не с `main` (исправлено в 1.0.5.75 — см. `CHANGELOG.md`), а старый apply несёт
только `www/` + helper-скрипты, без установщика. Лечение — полный деплой
(этот раздел), упакованный в один скрипт: после него плата ходит за обновлениями
в `main` и дальше обновляется через UI.

**Архив на рабочей машине** (имя несёт версию; `git fetch` обязателен — шаг 2):
```
git fetch origin && git archive --format=tar.gz -o SA-02m-full-<ver>.tar.gz origin/main
```
Архив кладётся в `/tmp` платы любым транспортом (SFTP, шаг 3; WinSCP).

**На плате (root), одной строкой:**
```
mkdir -p /tmp/sa02m-upd && tar xzf /tmp/SA-02m-full-<ver>.tar.gz -C /tmp/sa02m-upd && bash /tmp/sa02m-upd/scripts/offline-full-update.sh
```
Скрипт выполняет шаги 1, 5–7 этого раздела (шаг 4 — распаковка — это сама строка выше): проверяет, что дерево полное
(www-only архив отклоняется), показывает установленную и целевую версию
(откат ниже установленной — только `--force`), снимает бэкапы
`/root/etc-backup-<ts>.tgz` и `/root/www-backup-<ts>.tgz` (`--no-backup`
отключает), делает `bash -n install.sh`, запускает `install.sh` **в фоне**
с логом `/root/install-offline-<ver>.log` и каждые 15 с печатает новые строки
лога; по завершении — таблица пост-проверок PASS/FAIL (шаг 7: `systemctl
--failed`, nginx :9999, VERSION, `visudo -c`, строки `[ERR]`/`[WARN]`, core-службы,
и **проверка обновлений → main**: запускает `sa02m-web-update-check --manual` и
показывает `branch`/`remote_version`/`deployed_version`/`update_available` из
`check.json`). Код выхода 0 — все PASS. Ctrl-C во время ожидания установку
**не прерывает** — вернуться: `… offline-full-update.sh --status` (хвост лога,
ожидание PID, те же проверки). `--dry-run` — только проверки и план действий,
без бэкапов и без запуска. `--help` — полный список опций.

**Режим — обновление (refresh) по умолчанию** («Режим обновления» выше):
сторонние стеки не ставятся и не включаются сами (уже установленный и не
отключённый оператором обновляет только sa02m-надстройку), состояние служб
сохраняется; все стеки sa02m (веб, прошивальщик, облако, MQTT-мост,
устройства, шлюз, Алиса, updater) ставятся всегда. Пред-снимок
`/root/install-offline-<ver>.svc-before` позволяет таблице пост-проверок
отличить сохранённую операторскую остановку (PASS «состояние сохранено») от
уроненной обновлением службы (FAIL). Поставить/обновить и сторонние:
`--with-optional` (payload MPLC — Предусловия выше); пропустить модули:
`--skip MQTT,GATEWAY` (перечень — `install.sh`, раздел «Optional stacks»).

### ~~Мост самообновления для плат < 1.0.5.75~~ — ОТМЕНЁН

> **Не использовать.** Этот способ переписывал боевые version-ветки на origin
> force-push'ем и признан слишком рискованным для флота — решение
> `docs/decisions/no-force-push-version-branches.md`. Старые платы обновляйте
> **офлайн-архивом** («Офлайн-вариант» выше), НЕ модификацией веток. Раздел ниже
> оставлен как исторический; команды `publish-bridge.sh --push` запускать НЕЛЬЗЯ.


Плата < 1.0.5.75 **с интернетом** может обновиться до текущего `main` через
собственную вкладку «Обновление веб», без SSH. Почему нужен мост: её проверка
обновлений целится в свою версионную ветку, не в `main` (`CHANGELOG.md`
1.0.5.75), а её apply не несёт установщик. Рычаг — поведение самого старого
apply: он клонирует свою ветку, копирует `www/` + helper-скрипты и **сразу
выполняет** `/usr/local/sbin/sa02m-repair-web-env` из клона. Мост = на
версионной ветке публикуется **коммит `origin/main` с ровно одним заменённым
файлом** — `etc/sa02m-repair-web-env.sh` ← лаунчер
`tools/update-bridge/repair-web-env-launcher.sh`, который запускает в отдельном
systemd-юните (`sa02m-bridge-full-update-<ts>`, свой cgroup) полную установку
из `main`: `git clone main` → `scripts/offline-full-update.sh --unattended`
(пишет `running|done|error` в legacy `update_status` и ведёт `update.log`,
которые показывает старый UI). Установка через мост — это refresh: обёртка
запускает `install.sh --refresh` («Режим обновления» выше). Настоящий `repair-web-env` переустанавливает
`scripts/03-webserver.sh` в ходе этой установки — лаунчера на плате не остаётся.
Лаунчер и скрипт живут в `tools/update-bridge/` на `main`; в `etc/` `main`
лаунчер **никогда** не попадает.

**Кого мост обновляет полностью, а кого — нет.** Полная установка через
лаунчер срабатывает только на платах с **legacy apply — < 1.0.5.66** (он сам
исполняет `/usr/local/sbin/sa02m-repair-web-env`). Платы **1.0.5.66–1.0.5.74**
(apply передаёт работу shared runner'у до helper-цикла) лаунчер **не
исполняют**: runner кладёт его в `/usr/local/sbin/sa02m-repair-web-env`
инертно и делает обычный OTA-overlay мост-коммита (= `main`: www, helper'ы,
`opt/`, юниты; проверка обновлений — по `main`) — полной установки нет, но
такой плате она и не нужна; инертный лаунчер заменится настоящим файлом при
следующем apply/install (других вызывающих у него нет). `--all-stale` это
учитывает: ветки обеих групп получают один и тот же мост-коммит.

**Порядок:** изменения (обёртка + лаунчер) сначала смержены в `main` — лаунчер
клонирует именно `main`. Затем на рабочей машине (по умолчанию DRY-RUN: печатает
старый tip → новый sha и точные push-команды, ничего не пушит):
```
bash tools/update-bridge/publish-bridge.sh 1.0.3.34          # план
bash tools/update-bridge/publish-bridge.sh 1.0.3.34 --push   # выполнить
bash tools/update-bridge/publish-bridge.sh --all-stale --push # все версионные ветки < main
```
Скрипт тегирует старый tip ветки `archive/<ветка>` (дом отката; существующий
тег не трогает), пушит тег, затем переносит ветку на мост-коммит
(`--force-with-lease` на старый tip) и проверяет: `git ls-remote` tip == новый
sha, `raw.githubusercontent.com/…/<sha>/www/network_config/VERSION` == версия
`main`. Ветки с версией ≥ `main` (релиз в работе) не трогаются. Сначала одна
плата Оператора (`1.0.3.34`) → проверка → потом `--all-stale`.

**Что видит Оператор на плате:** «Проверить» → «доступна <версия main>» →
«Применить» → старый UI пишет `done` (www и helper-скрипты уже новые) → в
`update.log` строка «мост: запускаю полную установку … подождите 10–15 мин» →
фон 10–15 мин (статус `running`) → обновить страницу → новый UI, статус `done`,
проверка обновлений теперь по `main` (`check.json`: `branch=main`). Лог моста
на плате: `/root/install-bridge-<ts>.log`, лог установки
`/root/install-offline-<ver>.log`. Бэкапы — как в офлайн-варианте, с одной
оговоркой: старый apply переписал `www/` **до** запуска лаунчера, поэтому
`/root/www-backup-<ts>.tgz` в мост-случае уже содержит www `main`; настоящий
снимок «до» — только `/root/etc-backup-<ts>.tgz`.

**Честно о рисках:** в окне 10–15 мин UI уже новый, а backend ещё старый —
часть вкладок отвечает ошибками, это ожидаемо; плату в это окно **не
обесточивать**; неудачная установка оставляет `VERSION` = новая при частичном
backend (статус `error`, детали в `/root/install-bridge-<ts>.log`) — лечится
повтором через офлайн-вариант (архив в `/tmp` + `offline-full-update.sh`) или
по SSH `… --status`; **`systemd-run` есть, но не сработал** — установка НЕ
запущена (лаунчер намеренно не запускает её в cgroup fcgiwrap: рестарты
nginx/fcgiwrap внутри `install.sh` убили бы её на полпути), статус `done`, в
`update.log` строка «systemd-run не удался — установка НЕ запущена …», веб
новый / backend старый — лечится офлайн-вариантом по SSH; **перезагрузка или
обесточивание внутри окна** — статус может остаться `running` (UI показывает
его до следующего apply), backend частичный — лечится тем же офлайн-вариантом;
**повторное «Применить» во время окна** не нажимать: лаунчер второй установки
не запустит, но старый apply перепишет статус на `done` посреди установки.
Без интернета на плате мост не сработает (clone `main` упадёт → `error`).
**Откат ветки** на форже: `git push --force-with-lease origin
archive/<ветка>:refs/heads/<ветка>` — вернёт прежний tip; уже обновлённая плата
от этого не откатывается (её откат — бэкапы `/root/*-backup-*.tgz`, раздел
«Шаги», п. 1).

---

## Подготовка золотого образа (мастер для клонирования)

Единственный дом процедуры санитизации платы-мастера перед снятием образа eMMC
для клонирования. Проверена и выполнена на стенде 192.168.1.136 (2026-08-18).
Задача — убрать с мастера идентичность и данные конкретной платы, **сохранив
установленный софт и службы**, чтобы образ размножался на новые платы чистым.

### Предупреждения (прочитать до первой команды)

- **Необратимо.** Удаление ключей, сертификатов и данных проекта отменить
  нельзя — сначала бэкап (шаг 1).
- **Образ размножается на клоны — ошибка тоже.** Всё, что осталось на мастере
  (чужой проект, включённая привязка к облаку, забытый лог), приедет на каждую
  плату из этого образа.
- **НЕ удалять установочные файлы служб:** systemd-юниты, пакеты в `/opt/*`,
  `start_mplc4.sh`, init.d-скрипты, `.so`-библиотеки. Санитизация убирает
  *данные и идентичность*, а не *установленный софт* — иначе клон не запустится.
- **СНАЧАЛА бэкап ключей/идентичности ЛОКАЛЬНО (off-device).** До любого
  удаления скачать файлы шага 1 на машину оператора: с затёртого мастера
  восстановить их будет уже неоткуда.

### 0. Отключить запись истории в текущей сессии

Первой же командой процедуры (до шага 1):

```
unset HISTFILE
```

Это останавливает перезапись `.bash_history` интерактивной SSH-сессией: иначе
сами команды санитизации (шаги 9–11 и flush при logout) снова окажутся в
истории и уедут в образ.

### 1. Бэкап ключей и идентичности локально (off-device)

До любого удаления скачать по SFTP на машину оператора и убедиться, что копии
на месте и ненулевого размера:

```
/opt/mplc4/server/mplc.key
/root/mplc_key_backup/*
/var/lib/sa02m-alice/ca.crt.pem
/var/lib/sa02m-alice/device.crt.pem
/var/lib/sa02m-alice/device.key.pem
/var/lib/sa02m-alice/pending_claim.json
/etc/sa02m-cloud/agent.conf
/etc/sa02m_web.env
/etc/sa02m-alice/sa02m-alice-server.conf
```

Транспорт — тот же SFTP (`tools/ssh/sa02m_remote.py`), что и в деплое.

### 2. Alice — отвязать

Одной командой (тот же список, идемпотентно, с блоком `=== VERIFY ===`):

```
bash tools/imaging/reset-alice-enrollment.sh
```

Или по шагам:

```
systemctl stop sa02m-alice-client
systemctl disable sa02m-alice-client
rm -f /var/lib/sa02m-alice/device.crt.pem \
      /var/lib/sa02m-alice/device.key.pem \
      /var/lib/sa02m-alice/pending_claim.json
rm -f /run/sa02m-alice/status.json
# привязки: uuid4-идентификаторы устройств уезжают в клон так же, как сертификат
printf '%s\n' '{' '  "rooms": [],' '  "devices": []' '}' \
      > /etc/sa02m-alice/sa02m-alice-devices.conf
sed -i 's/^[[:space:]]*client_enabled[[:space:]]*=.*/client_enabled = false/' \
      /etc/sa02m-alice/sa02m-alice-client.conf
```

`ca.crt.pem` **оставить** — это общий CA шлюза, не идентичность платы.
`sa02m-alice-server.conf` (адреса шлюза) и остальные ключи `client.conf`
(`mqtt_host`, `mqtt_port`, `log_level`) — тоже конфигурация, их не трогают.
Если на плате есть легаси-раскладка (`/etc/sa02m-alice-devices.conf`,
`/etc/sa02m-alice-client.conf`) — те же две правки применяют и к ней.
Проверка: `ls /var/lib/sa02m-alice` — остаётся только `ca.crt.pem`;
`systemctl is-enabled sa02m-alice-client` = `disabled`; в
`sa02m-alice-devices.conf` пустые `rooms`/`devices`; `client_enabled = false`.

С 1.0.6.20 **пайплайн снятия образа делает это сам** (`stream-after-cleanup.sh`
до `dd`, плюс безусловная фатальная проверка в `patch-firstboot-image.sh` —
`docs/contracts/image-identity-reset.md`). Поэтому шаг остаётся обязательным не
как единственная защита, а ради двух вещей: off-device бэкапа (шаг 1) и ручного
`dd` мимо `make-image.sh`, куда автоматика не дотягивается.

### 3. Облако — отвязать

```
systemctl stop sa02m-cloud-agent sa02m-cloud-frpc sa02m-cloud-heartbeat
systemctl disable sa02m-cloud-agent sa02m-cloud-frpc sa02m-cloud-heartbeat
```

В `/etc/sa02m-cloud/agent.conf` выставить `enrolled = false` и очистить
`device_id` (пустое значение). Адреса `api_url` / `server_host` **оставить** —
это конфигурация, а не идентичность. Файл уже сохранён off-device в шаге 1,
поэтому правится на месте: локальную `.preimage`-копию на плате не оставлять
(она несёт старый `device_id` в образ). Проверка:
`grep -E 'enrolled|device_id' /etc/sa02m-cloud/agent.conf` → `enrolled = false`
и пустой `device_id`; три службы `disabled`.

### 4. MQTT — очистить устройства и retained

В `/etc/sa02m-modbus-mqtt.yaml` заменить список устройств на `devices: []`
(секцию `mqtt:` **не трогать**). Перед правкой снять локальную `.preimage`-копию
и удалить её в конце (шаг 10 — иначе старый список приедет в образ):

```
cp -a /etc/sa02m-modbus-mqtt.yaml /etc/sa02m-modbus-mqtt.yaml.preimage
# отредактировать: devices: []
systemctl stop sa02m-modbus-mqtt mosquitto
rm -f /var/lib/mosquitto/mosquitto.db
rm -f /run/sa02m-modbus-mqtt/*.json
systemctl start mosquitto sa02m-modbus-mqtt
```

`rm mosquitto.db` сбрасывает retained-сообщения; `/run/*.json` — оперативный
кэш моста. Проверка: в yaml `devices: []`; `/var/lib/mosquitto/mosquitto.db`
отсутствует.

**Честно:** собственная телеметрия платы `sa02m-<hostname>` появится снова
после старта моста — это самоотчёт службы о себе, а не «след» удалённого
устройства.

### 5. MPLC — снять проект и ключ, служба остаётся запущенной

```
systemctl stop mplc4
rm -f /opt/mplc4/server/mplc.key
rm -rf /root/mplc_key_backup
rm -f /opt/mplc4/server/cfg/config.bin \
      /opt/mplc4/server/cfg/ProjInfo.json \
      /opt/mplc4/server/cfg/VMInfo.json \
      /opt/mplc4/server/cfg/_files.xml
rm -f /opt/mplc4/server/EventsData.db \
      /opt/mplc4/server/session.bin \
      /opt/mplc4/server/session2.bin \
      /opt/mplc4/server/pid
rm -rf /opt/mplc4/host_monitor_temp/*
systemctl start mplc4
```

**НЕ трогать** `.so`-плагины, бинарники сервера и `start_mplc4.sh` — это
установленный софт. Итог: `mplc4` работает, показывает «не активирована», без
загруженного проекта.

**Честно:** ключ MPLC привязан к железу (SystemKey из MAC), поэтому на клоне он
всё равно невалиден — каждую новую плату активируют заново. Удаление ключа тут
— гигиена мастера, а не то, что «чинит» активацию клона.

Проверка: `systemctl is-active mplc4` = `active`; `mplc.key` отсутствует; в
`cfg/` нет `config.bin` / `ProjInfo.json`.

### 6. Финальное состояние служб

| Службы | Состояние |
|---|---|
| mplc4, mosquitto, nginx, sa02m-flasher, sa02m-modbus-mqtt, sa02m-devices-api | running + enabled |
| docker (+ docker.socket), nodered, klogic, codesyscontrol, codemeter (+ webadmin/logger), sa02m-alice-client, sa02m-cloud-agent/-frpc/-heartbeat | stopped + disabled |
| regen-ssh-host-keys, sa02m-rootfs-expand | enabled (helper'ы первого старта клона) |

Проверка: `systemctl is-active <svc>` и `systemctl is-enabled <svc>` по каждой
строке таблицы.

### 7. Мусор в /root и данных

```
rm -f  /root/*.tgz /root/deploy-*.tar.gz
rm -rf /root/sa02m-deploy-* /root/dep-all
rm -rf /root/opt-cloud*backup*
rm -rf /root/.node-red
rm -f  /root/install-*.log /root/apt-*.log
rm -f  /var/lib/sa02m-stand/*.db
```

**Оставить** `.ssh`, `.zshrc`, `.oh-my-zsh`. **НЕ удалять** `.so` и
установочные файлы. `/var/lib/sa02m-stand/*.db` — история устройств стенда, на
мастере не нужна. Проверка: `ls -a /root` — только рабочие dotfiles, без
деплой-архивов и логов.

### 8. Логи и история — полная очистка

```
journalctl --rotate
journalctl --vacuum-time=1s
rm -rf /var/log/journal/*
rm -rf /var/log/dumps/* /var/log/mplc4/*
find /var/log -type f -name '*.log' -exec truncate -s 0 {} +
: > /var/log/wtmp
: > /var/log/btmp
: > /var/log/lastlog
cat /dev/null > ~/.bash_history
history -c
```

Очистка `.bash_history` — **последнее действие с данными** (после неё в сессии
не выполняют новых команд, кроме проверок). Благодаря `unset HISTFILE` из шага 0
интерактивная сессия не перезапишет файл при logout, поэтому пустым он и уедет в
образ. Проверка: `journalctl --disk-usage` минимален; `~/.bash_history` пуст.

### 9. Идентичность клона

```
truncate -s 0 /etc/machine-id
```

**SSH host-ключи вручную НЕ удалять** из активной сессии: их регенерирует
enabled-служба `regen-ssh-host-keys` на первом старте клона, а удаление на
живой плате оборвёт текущую SSH-сессию.

**Честно:** и `machine-id`, и SSH host-ключи заново создаются на первом старте
клона — их «отсутствие» в образе это норма, а не потеря. Проверка:
`stat -c %s /etc/machine-id` = `0`.

### 10. Проверочный чек-лист (перед снятием образа)

- [ ] Службы в целевом состоянии (таблица шага 6).
- [ ] **Сеть и DNS после холодной загрузки.** Обязательный прогон
      **FR-CABLE** ×3: включить питание с ОТКЛЮЧЁННЫМ кабелем LAN, воткнуть его
      примерно на T+40 с. Процедура, полный список проверок после каждого
      прогона и критерии — `docs/contracts/boot-network-dns.md` §9;
      **здесь они сознательно не повторяются**, чтобы две копии не разъехались.
      Дополнительно к §9, специфично для снятия образа:
      `systemd-analyze` до и после — прирост ≤ 2 с с кабелем, ≤ 10 с без.
      Если хотя бы одна **загрузка с кабелем** израсходовала весь бюджет
      ожидания, значение `IFUP_CARRIER_WAIT_SECS` (по умолчанию 10 с) поднимают
      **по этому измерению**, а не «на глаз»; потолок — 120 с, дальше скрипт
      обрежет сам. Пять обычных холодных загрузок с кабелем — покрытие
      регрессий, а не доказательство.
- [ ] `/opt/mplc4/server/mplc.key` отсутствует.
- [ ] alice device-сертификаты отсутствуют, `ca.crt.pem` на месте.
- [ ] `sa02m-alice-devices.conf` — пустой документ (`"rooms": []`,
      `"devices": []`), ни одной привязки донора (обе раскладки, если есть).
- [ ] `client_enabled = false` в `sa02m-alice-client.conf`, служба
      `sa02m-alice-client` = `disabled`.
- [ ] `enrolled = false` и пустой `device_id` в `agent.conf`.
- [ ] в `sa02m-modbus-mqtt.yaml` — `devices: []` (0 устройств).
- [ ] `stat -c %s /etc/machine-id` = `0`.
- [ ] `~/.bash_history` пуст.
- [ ] локальные `.preimage`-копии удалены (`sa02m-modbus-mqtt.yaml.preimage`
      нет; `agent.conf` на плате не бэкапилась — её off-device копия в шаге 1).
- [ ] установочные файлы НА МЕСТЕ: systemd-юниты, `/opt/*`, `start_mplc4.sh`,
      `.so`-библиотеки.
- [ ] **`frpc --version` возвращает `0.61.x`** — мастер обязан нести
      `/usr/local/bin/frpc`, чтобы клоны унаследовали его (иначе агент клона
      рапортует `frpc_missing`, туннель не поднимается). Бинарник переживает
      санитайз и factory-reset: `/usr/local/bin/frpc` **не** входит в
      `etc/sa02m-factory-defaults/lists/wipe.list` (там только конфиги `/etc`),
      а служба `sa02m-cloud-frpc` лишь останавливается/отключается (шаг 6) —
      сам файл не трогается. Источник истины по frpc:
      `docs/vendor-integrations.md → frpc`.

### 11. Снятие образа

Образ eMMC снимает оператор своим инструментом (PiShrink — отсюда служба
`sa02m-rootfs-expand`, разжимающая rootfs на первом старте клона). Внешняя SD
(`/media/sdcard`) в образ eMMC **не входит**.

---

## Доставка vendor-payload Node-RED и обновление стека

Единственный дом этой процедуры. Ни один шаг на устройстве не выполняется в
обход неё (PROTOCOL.md инвариант 4). Форма payload'а и его сборка — в
`docs/vendor-integrations.md` → «Node-RED (оффлайн payload)»; здесь — что
делать с устройством.

**Когда нужна:** плата без интернета, где Node-RED нужно поставить; или плата
с установленным Node-RED 3.x, который надо перевести на 4.1.13. Свежая плата
без Node-RED — это просто установка (фазы A, D, E без бэкапа).

**Почему отдельная процедура, а не кнопка.** Кнопка «Установить» намеренно
**отказывается** перезаписывать установленный Node-RED через мажор
(`major_upgrade_refused` в панели; причина и что делать вместо этого — в
`/var/log/sa02m_install.log`): первый старт 4.x мигрирует потоки и
перешифровывает учётные данные **на месте**, и без бэкапа это необратимо.
Кнопка не умеет делать бэкап — процедура умеет.

**Порядок шагов принципиален: сначала Node.js 22, потом Node-RED 4.1.13.**
У каждого шага ровно один подозреваемый. Оракул первого шага — «то, что
работало вчера, работает и сейчас»: прежний Node-RED без изменений на новом
Node. Сломалось — виноват Node, и откат сводится к удалению распакованных
файлов. Между шагами вы несколько минут живёте на EOL-версии Node-RED — это
осознанная плата. Один и тот же payload годится для обоих шагов: дерево —
чистый JS и не привязано к мажору Node.

**Окно работ согласуется с владельцем стенда.** Плата 192.168.1.136 — общий
ресурс (через неё идут прогоны RS-485, см.
`.ai-dev/notes/bench-136-bridge-divergence.md`), рестарт службы посреди чужого
прогона не делается молча.

**Простой:** 4–5 коротких окон по ≤2 мин (репетиция отката, шаг 1, шаг 2 и
любой откат). «Простой» = `nodered.service` остановлен, значит **:1880
недоступен и все потоки стоят**, включая периодические и промышленный I/O.
Худший случай с полным откатом — ~15 мин. Первую же измеренную длительность
записать сюда вместо этой оценки.

Доступ к плате — по `docs/AGENTS_SSH_AND_DEVICE_ACCESS.md` /
`tools/ssh/sa02m_remote.py`; доставка payload'а — тем же транспортом, что и
остальные vendor-каталоги (`docs/vendor-integrations.md` → «Ручная загрузка на
боевое устройство»), в `/opt/vendor-installers/nodered/`.

### Фаза A — предполётная проверка (без простоя, пока интернет ещё есть)

```
df -h / /usr                         # место под дерево + бэкап
ldd --version | head -1              # official armv7l Node 22 требует современный glibc
node -v; npm -v; which -a node npm; dpkg -l nodejs | tail -1
npm ls -g --depth=0                  # что ещё живёт на этом Node
sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9.]*\)".*/\1/p' \
    /usr/lib/node_modules/node-red/package.json | head -1
systemctl cat nodered.service; systemctl is-enabled nodered
```

Инвентаризация потоков — половина «до» в проверке миграции:

```
grep -o '"type":"[^"]*"' ~nodered/.node-red/flows*.json | sort -u   # набор типов нод
ls ~nodered/.node-red/node_modules                                  # доп. ноды
find ~nodered/.node-red/node_modules -name '*.node'                 # НАТИВНЫЕ бинды
```

**Любая находка в третьей команде — конкретная поломка на Node 22** (бинд,
собранный под ABI Node 20, не загрузится). Решать её надо ДО шага 1, пока на
плате есть интернет для пересборки. Это самая ценная строка всей фазы.

```
apt-get download nodejs              # кэшируем .deb текущего Node 20, пока сеть есть
```

### Фаза B — бэкап (данные Оператора)

В `/root/nodered-backup-<ts>/`, затем **скопировать с платы**:

1. `tar czf node-red-tree.tgz -C /usr/lib/node_modules node-red` — живое дерево.
2. `tar czf nodered-home.tgz -C /home/nodered .node-red` — потоки,
   `flows_cred.json`, `settings.js`, `environment`, `node_modules` **и
   dotfiles** (`.config.runtime.json`, `.config.nodes.json`,
   `.config.users.json`).
   **Проверить, что dotfiles действительно в архиве:**
   `tar tzf nodered-home.tgz | grep '\.config'` — не «должны быть», а есть.
   Потеря `.config.runtime.json` делает зашифрованные учётные данные
   **невосстановимыми** (в нём лежит сгенерированный `credentialSecret`, если
   в `settings.js` он не задан явно). Это худший необратимый исход всей работы.
3. Unit и drop-in'ы: `systemctl cat nodered.service > unit.txt`, сами файлы,
   `/etc/systemd/system/nodered.service.d/` при наличии, `systemctl is-enabled`.
4. `readlink -f /usr/bin/node-red`.
5. `.deb` Node 20 из фазы A, строка `dpkg -l nodejs`,
   `/etc/apt/sources.list.d/nodesource.list`.
6. `tar -tf node-v22.*-linux-armv7l.tar.xz > node22-filelist.txt` — **точный
   список путей**, которые распаковка создаст в `/usr/local`. Распаковка
   сливается с существующим `/usr/local`, поэтому этот список — единственная
   точная «распаковка назад». Составить ДО распаковки.
7. `sha256sum` всего перечисленного, записать.

**Архив — секрет.** В нём `flows_cred.json` и `credentialSecret`. Хранить как
секрет, не коммитить, не оставлять в общей папке, удалить по завершении работ;
кто удаляет — назвать поимённо при согласовании окна.

### Фаза C — репетиция отката (так откат становится проверенным)

Непроверенный откат — не откат. Репетиция идёт **до** обновления, пока система
заведомо исправна: только в этот момент у теста есть правильный ответ для
сравнения.

1. `systemctl stop nodered`
2. `mv /home/nodered/.node-red /home/nodered/.node-red.pretest`
3. Восстановить из `nodered-home.tgz` **той же командой, что и в откате**
   (+ `chown -R nodered:nodered`).
4. `mv /usr/lib/node_modules/node-red /root/nr-tree.pretest`, восстановить из
   `node-red-tree.tgz`.
5. `systemctl start nodered` и проверить ВСЁ: unit active; `:1880` отдаёт
   редактор; версия из `package.json` = исходная; **набор типов нод тот же,
   что в фазе A**; узел с учётными данными по-прежнему настроен; в журнале нет
   ошибки загрузки учётных данных (`Error loading credentials` /
   «Ошибка при загрузке учетных данных»).
6. Удалить `.pretest`-копии только после того, как п.5 прошёл.

**Критерий прохождения:** восстановленная система неотличима от исходной по
всем пяти проверкам. Меньше — обновление не начинается. **Если времени мало,
защищать надо именно этот шаг:** всё остальное восстановимо, а восстановимым
его делает он.

### Фаза D — шаг 1: только Node.js 22

1. Положить `node-v22.<patch>-linux-armv7l.tar.xz`, сверить sha256 с
   `SHASUMS256.txt`.
2. `systemctl stop nodered`
3. `tar -C /usr/local --strip-components=1 --no-same-owner -xf node-v22.*.tar.xz`
   — сосуществует с apt'шным Node 20, тот остаётся на месте (откат = удаление
   путей из `node22-filelist.txt`).
4. Убедиться, что на плате наш unit и он выбирает интерпретатор явно
   (`ExecStart` предпочитает `/usr/local/bin/node`): `systemctl cat nodered`.
5. `systemctl start nodered`

**Проверка шага 1 — прежний Node-RED должен работать как раньше:**

```
systemctl show nodered -p ExecStart -p Environment
ls -l /proc/$(pgrep -f 'red\.js' | head -1)/exe     # реальный бинарник → 22.x
node -v                                              # что видит shell root — может отличаться!
```

- версия Node-RED (из `package.json`) — прежняя, в Node-RED ничего не меняли;
- `:1880` отдаёт редактор; **набор типов нод тот же, что в фазе A**; в журнале
  есть строка о запуске потоков и нет строк об ожидании отсутствующих типов,
  об ошибке учётных данных и об ошибках загрузки нативных модулей (точные
  формулировки — во врезке «Язык журнала» ниже);
- записать RSS (`systemctl status` / `ps`) — первая реальная точка по Node 22
  под `--max-old-space-size=256` на 512 MB.

**Если шаг 1 не прошёл:** откатывается **только Node** — удалить пути из
`node22-filelist.txt` из `/usr/local`, перезапустить, убедиться, что прежний
Node-RED снова работает на Node 20. Node-RED не трогали, радиус поражения —
один компонент.

### Фаза E — шаг 2: Node-RED 4.1.13

1. Положить полный payload в `/opt/vendor-installers/nodered/`.
2. Кнопка «Установить» **откажет** (`major_upgrade_refused`) — так и задумано.
   Чтобы перейти через мажор осознанно, дерево снимается вручную, ПОСЛЕ того
   как фаза B подтверждена:
   ```
   systemctl stop nodered
   mv /usr/lib/node_modules/node-red /root/nr-tree-3x-$(date +%Y%m%d-%H%M%S)
   ```
   Домашний каталог `/home/nodered/.node-red` **не трогаем** — потоки и
   учётные данные должны мигрировать.
3. Установка идёт **через панель** (Управление → Службы → Node-RED →
   «Установить») — это тот самый путь, которым пользуется наладчик; проверять
   надо его, а не запуск ctl-скрипта по SSH.
4. Первый старт 4.1.13 мигрирует потоки и учётные данные **на месте** — без
   архивов фазы B это необратимо.

**Проверка шага 2 — все пять пунктов, а не «служба поднялась»:**

1. строка службы в панели: pending → **running** (не `staging_missing`, не
   `no_internet`, не `major_upgrade_refused`);
2. `http://<ip>:1880/` отдаёт редактор Node-RED;
3. версия из `/usr/lib/node_modules/node-red/package.json` = **4.1.13**;
4. интерпретатор процесса — **22.x**, прочитанный из unit'а и `/proc/<pid>/exe`,
   а не из shell'а root;
5. **потоки живы:** набор типов нод совпадает с фазой A, узел с учётными
   данными по-прежнему настроен, и в журнале есть строка запуска потоков —
   `Started flows` **или** «Запущены потоки» (см. врезку ниже), а строки
   `Waiting for missing types to be registered:` / «Ожидание регистрации
   отсутствующих типов:» нет.

> **Язык журнала — почему проверять надо обе формулировки.** Node-RED
> переводит свой рантайм-лог по системной локали: на стенде 192.168.1.136 он
> пишет «Запущены потоки», а не `Started flows`. Наш unit
> (`etc/systemd/system/nodered.service`) закрепляет локаль (`LC_ALL=C.UTF-8`),
> поэтому плата, установленная оффлайн-путём, ведёт журнал по-английски; но
> онлайн-путь оставляет unit официального инсталлятора, и там язык — системный.
> Скрипты проверяют оба варианта; глазами проверяйте так же. Точные строки
> берутся из каталогов `@node-red/runtime/locales/{en-US,ru}/runtime.json`.

Пункт 5 — не украшение. Открытый порт 1880 — ложно-зелёный признак: рантайм,
поднявшийся с остановленными потоками, слушает ровно так же, и панель покажет
зелёное, пока автоматика Оператора стоит.

**Проверка install-time пути (по желанию, на той же плате):**

```
SA02M_NODERED_DIR=/root/nodered-payload bash scripts/07-nodered.sh
```
завершается без `[ERR]`, а `install.sh` — без
`[WARN] 07-nodered.sh завершился с ошибкой`.

**Негативная проверка:** payload убран, сети нет ⇒ `07` завершается кодом **0**
с `WARN`, панель отвечает `staging_missing`. Пропуск должен оставаться чистым
пропуском.

### Откат после шага 2

1. `systemctl stop nodered`
2. `rm -rf /usr/lib/node_modules/node-red`, восстановить дерево из
   `node-red-tree.tgz` (или из копии, снятой на шаге E.2).
3. `rm -rf /home/nodered/.node-red`, восстановить из `nodered-home.tgz`,
   `chown -R nodered:nodered /home/nodered`.
4. При необходимости откатить и Node (пути из `node22-filelist.txt`).
5. `systemctl start nodered` и те же пять проверок, что в фазе C.
6. Убрать временные мутации устройства, если они делались — например строку
   `127.0.0.1 registry.npmjs.org` в `/etc/hosts`, которой на плате с сетью
   принудительно проверяют оффлайн-путь.
