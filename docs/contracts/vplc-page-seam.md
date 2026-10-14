# Контракт: страница vPLC на origin панели (`/vplc/`)

Дом шва между панелью СА-02м и сервером страницы vPLC (`vplc-plant-ui.service`,
код команды vPLC): маршрут nginx, CSRF, раскладка юнита и сокета, поле
`svc_vplc_ui` и ссылка «vPLC ↗», пути со страницы. Сторона панели — этот
репозиторий; юнит, сервер и сама страница — репозиторий `vPLC`, его копия
бывшего плана интеграции (`docs/SA02M_INTEGRATION_PLAN.md`) указывает сюда.
Угрозы и принятые остатки — `docs/threat-model.md` §3 «Страница vPLC».
Машинная грамматика (директивы, пути, поля, юниты) — латиницей (`PROTOCOL.md`
invariant 5); пояснения — по `docLanguage: ru`.

## 1. Маршрут nginx

Блок `location ^~ /vplc/` в `etc/nginx/network_config.conf`. Каждая строка ниже —
условие безопасности, а не настройка; меняется только по согласованию со
стороной vPLC.

| Директива | Что гарантирует |
|---|---|
| `auth_request /_auth_check;` | страница открывается только с живой сессией панели; нет сессии — `@vplc_login` (`302 ../login.html`, относительный — работает и под облачным префиксом) |
| `proxy_set_header Cookie "";` | cookie панели (и `session_token`) в процесс vPLC не уходит |
| `proxy_hide_header Set-Cookie;` | сервер vPLC не ставит cookie на origin панели |
| `proxy_set_header X-Forwarded-Prefix /vplc;` | страница знает, где смонтирована |
| `proxy_pass http://unix:/run/vplc-plant-ui/ui.sock:/;` | единственный upstream — unix-сокет (TCP-порта нет); `:/` срезает префикс `/vplc` |
| `if ($vplc_csrf_bad) { return 403; }` | изменяющий запрос без `X-SA02M-CSRF` до сервера vPLC не доходит (§2) |
| `map "$request_method:$http_x_sa02m_csrf" $vplc_csrf_bad` (контекст http): `"~^(GET\|HEAD):" 0;` раньше `"~^[^:]+:$" 1;` | GET/HEAD проходят, любой другой метод без заголовка — `403` |
| `location = /vplc { return 301 vplc/; }` | адрес без слэша ведёт на страницу (относительный редирект) |

Остальное в блоке (`proxy_buffering off`, `proxy_read_timeout 3600s` — поток SSE
рантайма, `proxy_connect_timeout 2s`) — эксплуатационные настройки, не условия
шва. `^~` не даёт regex-локациям перехватить путь `/vplc/…`. Маршрут проходит
и облачным туннелем (`/devcfg/<id>/vplc/`) за той же сессией
(`docs/contracts/cloud-panel-proxy.md`).

## 2. CSRF — условия стыка на стороне vPLC

Сервер vPLC сам никого не аутентифицирует; `SameSite=Lax` не мешает странице с
того же IP платы на другом порту (Node-RED `:1880` и т. п.) слать POST с сессией
оператора (`docs/decisions/selective-csrf-policy.md`, «Демоны»). nginx значение
заголовка не сверяет: защищает само наличие — кросс-origin-страница ставит свой
заголовок только после CORS-preflight, а preflight (OPTIONS без заголовка и без
cookie) nginx отбивает `403`, CORS origin панели не выдаёт.

Сторона vPLC держит два условия:

1. Страница шлёт `X-SA02M-CSRF` = значение cookie `sa02m_csrf` (читается с того
   же origin) в **каждом** не-GET запросе — иначе её POST/PUT/DELETE получат 403.
2. GET/HEAD к серверу vPLC **никогда не меняют состояние**: кросс-сайт-ссылка
   приходит GET-ом с cookie сессии, проходит `auth_request` и доходит до сервера
   vPLC; панель закрывает это своим правилом «GET не мутирует», сервер vPLC
   держит то же правило сам.

## 3. Юнит и сокет — условие стыка на стороне vPLC

Раскладка (решение Оператора 2026-10-07, подтверждена командой vPLC):

- `vplc-plant-ui.service`: `User=root`, `Group=www-data`,
  `RuntimeDirectory=vplc-plant-ui`, `RuntimeDirectoryMode=0750` ⇒ каталог
  `/run/vplc-plant-ui` `root:www-data` 0750; `NoNewPrivileges`,
  `ProtectSystem=full`, `PrivateTmp`; `Restart=on-failure` через 5 с.
- Процесс от root удаляет старый файл, делает bind
  `/run/vplc-plant-ui/ui.sock`, затем `chown root:www-data` и `chmod 0660`.
- **Без `RuntimeDirectoryPreserve`.** По умолчанию systemd удаляет каталог
  `RuntimeDirectory=` при остановке юнита (systemd.exec(5); сохраняет его только
  `restart` — между перезапусками). Значит, сокет есть ⇔ юнит работает и сделал
  bind: «сокет-сирота» остановленного юнита существовать не может. На этом
  держится проба §4 — включение `RuntimeDirectoryPreserve=yes` на стороне vPLC
  ломает шов и требует согласования.

Группа `www-data` (nginx, `status.cgi`) проходит в каталог и подключается к
сокету, но создать в нём файл не может; `/run` пишет только root — занять путь
может только root, ни пока юнит работает, ни пока остановлен
(`docs/threat-model.md` §3).

## 4. `svc_vplc_ui` и ссылка «vPLC ↗»

Поле части `services` статуса (`status.cgi?part=services`), строка, значения
`active | inactive | unknown`:

- `active` — unit-файл `vplc-plant-ui.service` установлен (`/lib`, `/usr/lib`
  или `/etc/systemd/system`) **и** сокет `/run/vplc-plant-ui/ui.sock`
  существует; иначе `inactive`. Состояние юнита у systemd не спрашивается: по
  условию §3 сокет есть только у работающего юнита. Проба не запускает ни
  одного внешнего процесса (три проверки файла и проверка сокета) — плата без
  vPLC за неё ничего не платит;
- `unknown` — значение по умолчанию в ответе, если проба не выполнялась.

Часть `services` кешируется до 45 с, поэтому только что остановленная страница
может ещё до 45 с давать 502 по ссылке.

Потребитель — `applyVplcNavLink` (`static/js/app/status.js`), ссылка
`#nav-vplc` в боковом меню (`index.html`, статичный узел вне контейнеров,
которые перерисовывают рендереры): `active`/`running`/`activating` ⇒ видна,
любое другое строковое значение ⇒ скрыта; поле отсутствует, не строка или
`unknown` ⇒ ссылка остаётся как есть (старый бэкенд, выключенная часть не мигают
ссылкой).

## 5. Пути со страницы

Страница запрашивает историю у nginx напрямую с cookie панели, путём **от корня
панели**: со страницы `/vplc/` — `../api/devices/history…`. Абсолютный `/api/…`
ломается на облачном префиксе `/devcfg/<id>/`
(`docs/contracts/cloud-panel-proxy.md`), `api/…` без `../` уйдёт в сам vPLC.

## 6. `nginx -t`

Site-файл с этим блоком проверяет только плата: при установке
(`scripts/03-webserver.sh`, `scripts/11-devices.sh`) и при OTA — проверка
здоровья после применения и откат (`etc/sa02m-update-runner.sh`). Ни дев-машина,
ни CI nginx не запускают (`.ai-dev/notes/quality-gate-environment.md`).

## 7. Плата без vPLC

Этот репозиторий рантайм vPLC не ставит (пакет — команды vPLC). Маршрут
`/vplc/` есть в site-файле на **каждой** плате: один шаблон для всех путей
доставки, неиспользуемая локация ничего не стоит, нет сокета — nginx отвечает
`502` за той же авторизацией. Ссылка «vPLC ↗» на такой плате скрыта
(`svc_vplc_ui` = `inactive`).

## 8. Смежные стыки (правила — там, здесь только указатель)

- Carel: пуск/останов только через `unit_on/on`, признаки `meta/error`,
  `alarm_reset`, неподключённые датчики — `docs/contracts/carel-ahu.md` §4–§5.
- Запись выходов из панели и агент-API — `docs/contracts/mqtt-set-endpoint.md`.
- История и сводка демона устройств (`/api/devices/history…`) —
  `docs/contracts/devices-history-api.md`.
- Энергия СЭ-02м-3, маркер `meta/energy_unit` — `docs/contracts/ce-energy-mqtt.md`.
- Клиент с токеном вместо сессии (`devices.history`, `devices.summary`,
  `mqtt.config.get`) — `docs/contracts/agent-api.md`.
- Мост не читает саму плату по TCP (локальный vPLC — только режим MQTT) —
  `docs/contracts/bridge-modbus-tcp.md` §5.
- Ростер RS-485 — не реестр устройств (у Carel и сторонних только счётчики) —
  `docs/contracts/rs485-roster.md`.

## 9. Проверка контракта

- §4, сторона производителя — строка реестра `status-vplc-probe`
  (`scripts/dev/test-status-vplc-probe.sh`): проба, извлечённая из `status.cgi`,
  в песочнице отвечает `active`/`inactive` по четырём сочетаниям «unit-файл ×
  сокет» и не запускает ни одного внешнего процесса; вызов и строка JSON живы.
- §4, сторона потребителя — строка `js-unit-vplc-nav`
  (`scripts/dev/test-vplc-nav-link.mjs`): `applyVplcNavLink` из поставляемых
  `app.js`/`app/status.js` показывает, скрывает и не трогает ссылку по правилу
  §4, без элемента не падает, `applyServicesStatus` её вызывает.
- §1 — строка реестра `vplc-route-contract`
  (`.ai-dev/quality/checks/vplc-route-contract.sh`): каждая директива таблицы,
  единственность блоков и `map`, порядок ключей `map`; случаи закомментированных
  строк — в `comment-mutation-proof`.
- **Не проверяется здесь:** что nginx принимает файл (§6 — только плата); что
  страница vPLC шлёт `X-SA02M-CSRF` и не мутирует по GET (§2 — код vPLC);
  владелец и права сокета и отсутствие `RuntimeDirectoryPreserve` (§3 — только
  на плате, `stat /run/vplc-plant-ui/ui.sock`, `systemctl cat vplc-plant-ui`).
