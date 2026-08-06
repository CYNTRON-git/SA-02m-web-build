# Развёртывание веб-интерфейса SA-02m

Единственный дом процедуры деплоя. Деплой выполняется **строго по этому
документу**, не импровизируя (PROTOCOL.md инвариант 4; `.ai-dev/procedures/
deployment.md`). Изменения репозитория попадают на устройство только через
описанные здесь пути — правка файла на устройстве в обход git запрещена.

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

## Три пути деплоя

| Путь | Когда | Чем |
|---|---|---|
| **www-only** | изменения только в `www/` (frontend + CGI) | `scripts/update-www-only.sh` |
| **full install** | новое устройство, или менялись `etc/`/`opt/`/systemd/демон | `install.sh` |
| **web-update (OTA)** | штатное самообновление устройства | вкладка «Обновление веб» (`web_update_*.cgi`, semver) |

`update-www-only.sh` синхронизирует `www/` → `/var/www/network_config`, чинит
права (CGI 755, static 644, owner `www-data`), пишет маркер коммита,
перезапускает fcgiwrap. nginx перезапуска не требует. Идемпотентен.

**Важно:** `update-www-only.sh` разворачивает `www/` и — если `etc/` есть в
дереве рядом — часть helper-скриптов/юнитов из `etc/` (идемпотентно, по
`[ -f … ]`-гардам). Он НИКОГДА не разворачивает демон прошивальщика
(`opt/sa02m-flasher/`) и его tmpfiles-юнит. Если релиз менял демон/`opt/` —
нужен `install.sh` (полный) ИЛИ отдельная доставка `opt/` + рестарт
`sa02m-flasher`. Для чисто-www деплоя `etc/` намеренно не несут (см. шаг 2).

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
  Node-RED упал на `registry.npmjs.org`, остальная установка прошла до конца
  (см. Предусловия полного деплоя).

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
- **Исходящий интернет на устройстве.** Опциональные стеки ставятся из сети.
  Проверить до запуска:
  ```
  ip route            # должен быть default-маршрут
  cat /etc/resolv.conf  # непустой
  ```
  Без них модуль Node-RED падает с `[ERR] Нет доступа к registry.npmjs.org —
  для Node-RED нужен интернет`, а остальная установка при этом доходит до
  конца — отказ виден только в логе (шаг 7).
  Если интернета на устройстве нет (как на стенде) или стек не нужен —
  пропустить модуль явно, а не ловить ошибку: `SA02M_SKIP_NODERED=1 bash
  install.sh`. Так же отключаются остальные опциональные стеки:
  `SA02M_SKIP_MQTT`, `SA02M_SKIP_GATEWAY`, `SA02M_SKIP_CODESYS`,
  `SA02M_SKIP_MPLC`, `SA02M_SKIP_DOCKER` (перечень — `install.sh`).
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
     их через `|| log WARN`), и установка при этом идёт дальше. Node-RED
     виден и по `ERR` лишь потому, что модуль печатает собственную строку.
