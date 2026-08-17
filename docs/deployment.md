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
| **full install** | новое устройство, или менялись `etc/`/`opt/`/systemd/демон | `install.sh` |
| **web-update (OTA)** | штатное самообновление с интернетом | вкладка «Обновление» → GitHub (`web_update_*.cgi`, semver); apply через shared runner при наличии |
| **offline package** | обновление без интернета (с релиза N+1) | вкладка «Обновление» → файл `.sa02m`; packer на ПК: `python scripts/pack-offline-update.py` |
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
