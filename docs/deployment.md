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
2. **Доставка** `www/` и `scripts/` в staging на устройстве (например
   `pscp -r www scripts root@<dev>:/root/sa02m-deploy-<ver>/`).
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
- На стенде подключены два модуля MR-02m на COM4 @115200: адрес 6 (`6AO6AI`),
  адрес 8 (`4DO6DI`).

---

## Полный деплой (`install.sh`)

Нужен для нового устройства или когда релиз менял `etc/`/`opt/`/демона/tmpfiles.
Запускается на устройстве из полного чекаута репозитория:
`sudo bash install.sh`. Идемпотентен (повторный прогон не портит настроенное
устройство — установщик и есть путь обновления). Детали и порядок модулей —
`install.sh` и `scripts/0*.sh`.
