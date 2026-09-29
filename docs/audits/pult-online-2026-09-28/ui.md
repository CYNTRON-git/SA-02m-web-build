# Pult.online server — аудит UI/UX и SCADA-модели (для SA-02m)

Дата: 2026-09-29. Только чтение. Источник: локальное зеркало
`YandexDisk/ЦИНТРОН/Сборка линукс/Пульт Сервер/pult_server` (далее `PS/`), версия
`version_number 3234 / 06.06.2025` (`PS/services/server/server.js`). Живая плата
192.168.1.136 — только страница входа, без логина. Секреты не выводились
(`config.cfg`, `license/`, `ssl/`, `db.sq3` не открывались; из `server.js` читались
только имена ключей).

---

## 1. Архитектура фронтенда

### 1.1 Одна собранная страница
- `PS/html/index/index.html` (1.8 МБ) — шаблон с плейсхолдерами
  `/*--SCHEMA_JSON--*/`, `/*--SERVER_JS--*/`, `/*--BRAND-SCRIPT--*/`; сервер собирает
  из него `PS/services/server/temp/index.html` (3 006 213 Б).
- Внутрь инлайнятся: шрифт JetBrains Mono в base64 (`<style ssg-file='_fonts/fonts.css'>`),
  вся object-model `schema.js` (451 КБ), фреймворк `frame.js` (663 КБ, дублирован в
  index.html), CodeMirror (1.07 МБ), SimpleMDE (270 КБ), colorpicker, tableexport (478 КБ).
- Замер на живой плате (Performance API, страница входа): `transferSize 3 006 513`
  == `decodedBodySize 3 006 213` → **без gzip**, HTTP/1.1, DCL ≈ 1.0 с по LAN;
  дополнительно всего 2 ресурса (11 КБ). То есть 3 МБ грузит даже анонимный экран входа.
- Роутинг — hash: `#interface/<widget>?path=…`, `#editor/<widget>?path=…`
  (`schema.json` `sysmenu[].href`). Виджеты интерфейса — `PS/html/widgets/interface/*`
  (alarms, journal, trends, trendlog, trendslice, trendreport, vars, schedules,
  simple_schedule, universal_schedule, calendar, node_report, users, sessions, logs,
  ddos, remote, license, time, reboot…), редактора — `widgets/editor/*` (mimic 381 КБ,
  prototype, prototype_script, access, notification, dictionary, backups, buildlog…).
  Каждый `script.js` начинается с `/*--PSEUDO-CODE--*/` и двух наборов строк
  `if(locale("ru")){var $x="…"}else{…}`.

### 1.2 Доставка данных — WebSocket + JSON-RPC 2.0 с подписками
- `index.html:6064` `function WS(params)` — обёртка WebSocket: reconnect 1 с, очередь
  отправки на время обрыва, ping `"PING"/"PONG"`.
- `index.html:6310` `function RPC(params)` — JSON-RPC 2.0 поверх WS: `request()`,
  `requests()` (батч), `subscribe/unsubscribe/resubscribe`, таймаут запроса 10 с,
  ping 5 с, клиентский кэш ответов до 10 МБ (ключ = hash запроса), таймаут-ошибки
  с кодами `RPC_ERROR_*`.
- Методы, видимые во фронте: `var_update` (подписка), `var_set`, `trends_read`,
  `alarms`, `log`, `gateway_log`, `builder`, `license`, `ddos`, `session_update`,
  `user_update`, `user_notifications`, `refresh_interface`, `system_time`,
  `system_ready`, `user_login/logout/tokin/get/active`, `async`.
- Формат изменения: `var_update` → `p.data.changes[] = [var, ?, value, error]`;
  `error` ≠ 0 ⇒ контрол считается offline (`index.html:31904`
  `pult.subscribeStatusVar`). Подписка `only_last:true`.
- После «Сборки» проекта сервер шлёт `refresh_interface`; настройка
  `force_refresh_interface` перезагружает открытые браузеры
  («Страница обновлена после Сборки»).

### 1.3 Service worker `cache_worker.js`
- Стратегия **cache-first для любого GET с ответом 200 / type basic**, два кэша:
  `projects` (URL под `<referrer>projects/`) и `system` (всё прочее). Сброс — только
  `postMessage({type:'reset_cache'})`, выключение `enable_cache`.
- Нет версионирования кэша, нет stale-while-revalidate; регистрация управляется
  скрытой настройкой `js_cache_worker` (`config.shema`, `hidden=1`). На живой плате
  `navigator.serviceWorker.controller` = null — SW не активен.

### 1.4 Контролы = web components без Shadow DOM
- Библиотеки: `PS/html/controls/<lib>/lib.json` (каталог: имя ru/en, иконка dark/light
  data-URI, `tag`, `requires`) + `lib.js` (реализация). `attachShadow` не используется
  ни разу; `Util.registerCustomElement(class X extends aNode)`, стили — `<template>` с
  `<style>:host{…}` + параметр `css` («Содержание внутреннего тега <style>
  web-компонента», `schema.json` `mimic_controls.params`).
- Базовый класс `ControlCommonProto1` (`controls/common/lib.js:144`) объявляет
  свойства **декларативно**: `type`, `options`, `default`, `category`, `update`,
  `visibleIf:"click=gotoScreen,gotoLink"`, `hint` (ru/en), `dictionary`,
  `dictionary_link`. Отсюда редактор строит панель свойств автоматически.
- Состояния контрола — атрибуты хоста: `_undefined` (пульсация opacity 1 с — нет
  данных), `_inactive` (серый + contrast 50%, pointer-events none), `_grayscale`,
  `_disabled`, `_blink`.
- Общие click-действия любого контрола: `gotoScreen`, `gotoLink`, `showWidget`
  (модальное окно с виджетом по `#…`-ссылке), `clickValue`, `setValue`
  (press/release значения — «кнопка без фиксации»), `toggleValue`,
  `increaseValue/decreaseValue` (+ `click_maximum/minimum`), `enterValue`
  (модальный ввод с подсказкой `click_help`). `click_action: release|press` с
  предупреждением о ложных срабатываниях при прокрутке на мобильном.
- Инвентарь библиотек (lib.json):
  - **Общие (27):** переключатель, прямоугольник, круг, лампа, аварийная лампа, лампа
    с надписью, изображение, SVG, SVG(d), стрелка, ползунок, поле ввода, строковое
    поле, прогресс-бар, надпись, раскрывающийся список, список, переключатель щитовой,
    кнопка, микрочарт, тренд, стрелочный индикатор, стрелочный индикатор КМ, уровень,
    интервал трендов, **фейсплейт**, виджет.
  - **Мобильные (8):** заголовок, надпись, переключатель, поле ввода, список,
    вкладки, лампа, ползунок.
  - **Вентиляция (18):** пустая секция, вентилятор (иконка/центробежный/осевой/с
    приводом/крышный), заслонка, фильтр, направление потока, калорифер, газовый
    нагреватель, рекуператоры (жидкостной/пластинчатый/роторный), увлажнитель,
    переходы прямой/загнутый, кондиционер.
  - **Водоснабжение (8):** труба, отвод, тройник, насос, клапан 2/3-ходовой, бак,
    теплообменник.
  - **Датчики (7):** перепада давления, дискретный, аналоговый, аналоговый
    дифференциальный, капиллярный термостат, процентный, стандартный.
  - **Технология (8):** теплообменник, фильтр, клапан (статика), насос, клапан,
    индикатор состояния, трубопровод, стрелка.
  - **Планировка (9):** стены (наружная, периметр, прямая, угол, ответвление,
    пересечение, дуга), окно, дверь.
  - Размеры: common 313 КБ, ventilation 281 КБ, technology 208 КБ… всего 1.1 МБ.

### 1.5 Мобильный режим
- `Util.isMobile = screen.width < 640` (`index.html:113`), `Util.isTouch`.
- Не адаптивная вёрстка, а **отдельная мнемосхема на форм-фактор**: у объекта `screen`
  поля `desktop_mimic` / `mobile_mimic` (`schema.json types.screen`), плюс отдельная
  библиотека `control-mobile-*` (крупные строки «надпись/значение», вкладки, select).
- Мнемосхема `ПВ/Настройки ПВ.mimic` собрана из `control-mobile-select2` со списками
  `list='0=Лето (насос откл.),1=Зима (насос работает)'`.

### 1.6 Темы и брендирование (OEM)
- `PS/html/themes/{dark,light}/default_colors.css` — ~150 CSS-токенов
  `--app-*` (header/topline/subwidget/panel/button/online/offline…).
- Ключи брендинга в `config.shema` (группа «Брендирование», часть `advanced=1`):
  `brand_logo`, `brand_show_logo`, `brand_link`, `brand_text`, `brand_icon`,
  `brand_editor_icon`, `brand_login_form_image`, `brand_login_back_image`,
  `brand_login_bottom_link_text/href`, `brand_project_back_image`,
  `template_projects_link/icon`, `buy_license_link`, а также **`brand_script`,
  `brand_head`, `brand_body`, `brand_common_style`, `brand_theme_style`,
  `brand_theme_colors`** — пути к произвольным JS/HTML/CSS.
- Паттерн «не правьте default_*, создайте свой файл и укажите путь в настройках»
  (шапка `themes/default_head.html`) — обновление дистрибутива не затирает бренд.
  `custom_body.html` содержит маркер `<!--SCADA-WORKSPACE-->` — точка вставки рабочей
  области в пользовательский каркас.
- `%theme%` подстановка в путях (`/html/themes/%theme%/default_logo.png`).

### 1.7 i18n
- Три механизма: (а) `{ru,en}`-объекты прямо в данных (`schema.json`, `lib.json`,
  `config.shema` help), (б) `lang.json` — ключи сервера (типы тревог, шаблоны
  уведомлений, `access_action_*`), (в) в каждом JS-модуле
  `if(locale("ru")){var $x=…}else{…}`. Язык по умолчанию — `config.language`.
- Нет единого словаря и нет runtime-перевода DOM; перевод выбирается при загрузке.

---

## 2. SCADA-модель

### 2.1 Иерархия объектов (`schema.json types`, 62 типа; `fdb.json`)
`projects → project → {nodes, groups, prototypes, mimics, screens, schedules, files,
alarms, journal, vars, dictionaries, notifications}`; `node`/`gateway` могут
вкладываться (`nodedir`, `gatewaydir`). Хранение — файловая «БД» (fdb): каталог на
объект с `@prop.json`, `@content.*`, `@access.json`, `resources/`.

- **Прототип** (`prototypes/*.prototype/@content.json`): `params[]` (name, type
  string/list/number/checkbox, default, help, `application:"drv_mbrtu.drv_node_port"`
  — куда параметр подставляется, group, caption), `drivers{drv_mbrtu:[vars…],
  drv_virt:[…], drv_mbtcp, drv_gateway}`, `reports[]`, `scripts[]`.
  Переменные драйвера ссылаются на параметры `%metric_period%`.
- **Узел** (`nodes/Узел1.node/@prop.json`) = прототип + значения параметров
  (`param_port:/dev/COM1`, `param_speed`, `param_slave`…). Узел получает переменные,
  отчёты и скрипты прототипа. Префикс `P<proj>_N<node>_` → глобальное имя
  переменной (`P1_N10_var1`).
- **Встроенные метрики связи** узла Modbus RTU: `metric_quality`, `metric_art`
  (ср. время ответа), `metric_up`, `metric_down` + виртуальная `status =
  status($metric_up,…)`. `skip_if_timeout`, `ignore_errors` (N подряд ошибок
  игнорируются), `grouping` запросов (без пробелов / с пробелами / по переменным).
- Драйверы (типы `prototype_drv_*`): alarm, bacnet, cam, cmd, gpsd, gateway, http,
  mbrtu, mbtcp, mbrot (RTU-over-TCP), mqtt, opcua, snmp, virt. У каждой переменной
  опционально: `trend`, `buffer`, «Размещение» (локально/на шлюзе), теги, описание,
  **Modbus-карта** (экспорт переменной как регистра Modbus TCP сервера/шлюза, форматы
  INT16…FLOAT_DCBA), **MQTT-карта** (топик, поля, readonly), **командная строка**
  (`service_local_exec__command` с `%var_value%`-подстановками).
- **Группы** (`group`): `users`, `tag_filter` — доступ к переменным/тревогам по тегам.
- **Экран** (`screen`): `desktop_mimic`, `mobile_mimic`, `html_file`,
  `load_js_env/css_env`, встроенный список тревог с фильтрами по узлам/тегам.
- **Проект**: раскладка из до 4 панелей (`layout`, `size1..4`, `content1..4`,
  `project_part`/`custom_screen`/`custom_widget`), звуки по классам тревог.
- **Справочники** (`dictionaries`) — для выпадающих списков.
- Шаблоны: `services/server/templates/{projects/{empty,segnetics},
  prototypes/standart_{bacnet,empty,gateway,mbrot,mbrtu,mbtcp,mqtt,opcua},
  notifications/default_{alarm,message}}`.

### 2.2 Мнемосхемы — как контрол связан с переменной
Пример `data/projects/Проект1.project/mimics/Вентсистема десктоп.mimic/@content.html`:
- Мнемосхема — HTML-документ из custom-элементов с атрибутами-свойствами,
  абсолютная геометрия (`left='174' top='86'`, дробные `width='170.00009301795606'`),
  центрирование `left='50%' margin_left='-370'`.
- **Параметры мнемосхемы** объявляются в `<head>`:
  `<meta name='pult:mimicproperty' content='node:object::'>`,
  `content='room:string::'`, `content='min_width:string::800px'`. Одна схема
  «Вентсистема» переиспользуется для любого узла — в неё передаётся объект `node`.
- **Привязка**: `value='$node.vars.damp_open'`, `alarm='$node.vars.alarm_filter_dirty'`,
  параметр схемы `name='$room'`.
- **Выражения** в `{{…}}`: `value='{{!$node.vars.fan_no_pressure && $node.vars.fan_on}}'`,
  `bottom='{{if($show_panel,70,-120)}}'`, `invisible='{{!$on_off}}'`. Вычислитель —
  парсер выражений (порт js-expression-eval, `index.html:~3500–3830`) с
  `toJSFunction` = `new Function(…"with(Parser.values)…")`.
- **Запись**: `control-common-input1 value='$node.vars.temp_setpoint' min_value='10'
  max_value='30' suffix='°'`; `control-common-switch1 value='$node.vars.system_on'`;
  `label click='toggleValue' click_var='$node.vars.fan_no_pressure'`.
- **Фейсплейт** (`control-common-faceplate1`): прозрачная область поверх элемента,
  левый клик → `l_link='#interface/trends?path=$node.path&vars={{$node.prefix}}_temp_outdoor&show_header=0'`
  (мини-тренд в модалке 500×300), правый → `trendlog` (таблица значений).
- **Вложенные мнемосхемы** — как custom-теги с именем-хэшем пути:
  `<mimic-b36-7rf7cmrl0p…  show_panel='$show_panel'>` (выезжающая панель
  управления `ПВ.mimic`).
- `@prop.json` мнемосхемы: `menu[]` (пункты меню схемы: `custom` с `href:"$trends_link"`,
  `nodes`, `settings`), `status_node/status_var`, `option_scale`, и `debug{}` —
  **в debug-словарь записан каждый промежуточный набор имени**
  (`"$n","$no","$nod","$node",…,"$node.vars.temp_ro"`) — мусор копится навсегда.
- Состав 5 схем проекта: label 35, rect2 30, pipe1 9, sensors-standart2 8,
  pressurediff1 7, straightcoupler1 6, input1 6, arrow1 6, mobile-label2 5, button1 5,
  damp1 4, mobile-input2 4, heater1 3, filter1 3, … faceplate1 3.
- Редактор мнемосхем (`widgets/editor/mimic`, 381 КБ): undo/redo, copy/cut/paste/
  clone, дерево элементов (вверх/вниз, Ctrl — в край), выделить контейнер/всё в
  контейнере, параметры схемы (node/number/string), пункты меню, ресурсы,
  «экраны и прототипы, связанные с мнемосхемой» (обратные ссылки), клавиатура:
  стрелки 1 px, Shift 10 px, Ctrl — ресайз.

### 2.3 Тревоги / журнал / квитирование
- Драйвер `drv_alarm`: `drv_type` обычная/**сводная** (агрегат по классам и тегам,
  приоритет «неквитированные» / «активные»); опрос подписка/период/cron; условие
  `!=0, ==0, ==, !=, >, <, >=, <=, отклонение, скорость изменения (ROC, период,
  направление)`, `drv_setpoint`, `drv_delay`, `drv_deadband`; `drv_class`
  3 Авария / 2 Неисправность / 1 Предупреждение / 0 Уведомление; `drv_acknowledge`,
  `drv_priority`, звук, текст уведомления; «Недостоверно» → Ошибка/Ноль/Значение.
- UI `widgets/interface/alarms` (44 КБ): вкладки **Срочные** (активные И
  неквитированные), **Актуальные** (активные ИЛИ неквитированные), Активные,
  Неактивные, Квитированные, Неквитированные, **Недостоверные** (ошибки),
  **Заблокированные** (shelved), Все. Действия: Квитировать / Квитировать все /
  Снять квитирование / Заблокировать на 1 мин…30 дней или «до новой активации»
  (shelving). Подтверждение с формулировкой «будут подтверждены от вашего имени»;
  Ctrl+клик — без вопроса. 7 комбинированных состояний
  (Квитированная Активная, Неквитированная Неактивная…).
- Журнал (`interface/journal`): время записи vs время события, категория, контекст,
  проект/объект/шлюз, **исполнитель**, IP, код ошибки, подсветка тревог, поиск в
  результатах, миллисекунды.
- Рабочая область (`interface/workspace`): звук вкл/выкл, «Сбросить звук до следующей
  тревоги», отправка сообщения пользователям, всплывающие события
  «Авария: Активация/Нормализация/Квитирование/Блокировка…».

### 2.4 Тренды и отчёты
- `interface/trends` — до 8 переменных; «на одном графике несколькими линиями» или
  «на разных графиках»; **«Ссылка на текущий интервал» + Копировать**.
- `trendlog` (таблица), `trendslice` (срез всех переменных на момент), `trendreport`
  и `node_report` (показатель, начало/конец периода, разница, **тариф**, итог,
  формула) — отчёт по счётчикам.
- Хранение: `data/trends/` — 10 000 файлов, по одному на переменную (`P1_N10_varN`).

### 2.5 Расписания и календарь
- `simple_schedule` (почасовой план, N задаваемых значений, цвет/высота столбца,
  **задержка между записями «разнести пусковые токи мощных нагрузок»**, шаблоны).
- `universal_schedule` — задачи с CRON `с м ч ДМ М ДН`, задержка старта/между.
- `calendar` — рабочие/выходные дни по годам, импорт/экспорт дампа, синхронизация с
  календарём удалённого сервера (`sync_calendar_url`, api-key); используется всеми
  CRON-событиями (расписания, скрипты, опрос).

### 2.6 Скрипты
- `prototype_script`: запуск/период/cron/**триггер**/таймаут; редактор CodeMirror
  (`editor/prototype_script`, 14 КБ). Скрипты привязаны к прототипу → исполняются на
  каждом узле.

### 2.7 Права доступа
- `@access.json` на любом объекте: `{view|edit|exec: {mode: allow|deny, groups:[…]}}`;
  по умолчанию **наследуется** от родителя («Унаследовано от <путь>» — ссылка) или
  задаётся явно. Пример корня проекта: `{"view":{"mode":"deny"}}`.
- Пользователи → группы; группа — `tag_filter` (видимость переменных/тревог по тегам).
- Сессии: `logout_interval 600` с, **`logout_fadetime 10`** (затемнение экрана перед
  авто-выходом), `offline_interval`, `password_min_size 8`, `allow_anonymous`,
  `editor_hold_time 10` (блокировка объекта на время редактирования другим).

### 2.8 Язык шаблонов уведомлений (`notifications/*.ntf/@content.txt`)
- Директивы строк: `#comment`, `#if <выражение>` (блок до следующего `#if`),
  `#telegram_parse_mode MarkdownV2`, `#if 0` — выключенный блок.
- Каналы-флаги: `%channel_interface%`, `%channel_telegram%` (в бинаре ещё
  `channel_email`, `channel_sms`, `channel_cmd`).
- Подстановки: `%var__alarm_type_name%`, `%var__alarm_html_link%` / `_md_link%`,
  `%var__node_html_link%`, `%var__time_str%`, `%var__alarm_repeated%`,
  `%var__alarm_action_act%`, `%sender%`, `%message%`; префикс `var__` заменяется
  на любое имя переменной (`P1_N1_var1__`). **`%debug%` выводит список всех доступных
  подстановок** — встроенная самодокументация.
- Типы событий: {alarm,fault,warning,notice} × {activate,normalize,ack,unack,shelve,
  unshelve}, system_login/error/test_message, user_message, var_change.

---

## 3. Help / UX-паттерны
- **Каждое свойство несёт ru/en help** (961 из 1319 свойств в `schema.json`),
  `caption{ru,en}`, `tooltip`, `groupHelp`, `placeholder`.
- Атрибуты свойства (частота в `schema.json`): `control` input 589 / edit 412 /
  select 117 / checkbox 59; `group` 1035; `def` 723; `visibleIf` 524 (условная
  видимость, напр. `"!inactive"`, `"click=setValue"`); `undef` 350 (значение
  «не задано» ≠ пусто); `validation` 50 (`notEmpty`, `tags`, `latinNum`,
  `pathNoDot`, `path`, `mac`, `attrName`); `unique`, `trim`, `reload`, `invert`
  (чекбокс «Активный» хранит `inactive`), `table{caption,tooltip}` (колонка в
  табличном представлении), `bottom` (в конец формы), `hide`, `dictionary` +
  `dictionary_link` (переход к справочному объекту).
- `config.shema` (INI с JSON-значениями): 108 help, 103 group, **62 `advanced=1`**
  (скрыты под «дополнительно»), 48 `visibleif`, `width`, `sort`, `hidden`.
  `config.default` рядом с `config.cfg` — база для **сброса к умолчаниям**.
- Каталог кодов ошибок `errors.json` (`{name, code}`, напр.
  `NS_ERROR_CONNECT_TIMEOUT 2307`, `NS_MBRTU_ERROR_BAD_CRC 2420`) пробрасывается
  во фронт (`backend_errors` в `server.js`) и локализуется.
- Мелочи: «Нажмите чтобы открыть фейсплейт» как подпись на схеме; кнопка
  «Сгенерировать/сбросить аварию» для пусконаладки прямо на мнемосхеме; горячие
  клавиши редактора показываются в строке подсказки.

---

## 4. Наше (SA-02m) — что уже есть
- Фронт: вкладки `dashboard, network, time, devices, mqtt, flasher, gateway, system`
  (`www/network_config/index.html`), 18 `<script>` (~1.0 МБ JS суммарно, раздельные
  файлы, `?v=&r=` cache-bust), polling-планировщик `status.cgi?part=` (6/12 с),
  `devices.js` — polling 5 с, `STALE_BORDER_S = 30`, модалка истории на canvas с
  зумом колесом, пресеты 1ч…30д, Excel-экспорт, события пиковых нагрузок, тариф ₽/кВт·ч.
- i18n: единый `DICT` RU→EN + MutationObserver + `uiT()` (`static/js/i18n.js`).
- Модель: `docs/contracts/sh-model.md` — `Device → Function → Point`, **`quality`
  обязателен, fail-closed**; шаблоны устройств `docs/contracts/template-device.md`
  (аналог прототипов); сценарии `docs/contracts/cloud-scenarios.md` (триггеры
  state/time/sun/boot/every, restricted Python, http-действия, лимиты).
- Нет: модели тревог с квитированием, журнала действий «кто/что/IP», ролей,
  мнемосхем, календаря праздников, idle-logout (сессия 10 дней —
  `lib_web_auth.sh:12`), deep-link на интервал графика (hash = только вкладка,
  `app.js:299`), уведомлений в Telegram с платы.

---

## 5. Идеи для SA-02m (ранжировано)

| # | Что делает Пульт | Что у нас | Зачем нашим пользователям | Трудоёмкость |
|---|---|---|---|---|
| 1 | Тревоги ISA-18.2-lite: классы (Авария/Неиспр./Предупр./Увед.), условие >,<,отклонение,ROC + задержка + мёртвая зона, квитирование, блокировка на время, вкладки «Срочные/Актуальные/Недостоверные» (`schema.json prototype_drv_alarm`, `widgets/interface/alarms`) | `devices.js` `alertBadgeHtml`, stale-рамка, события пиков; `sa02m-rules` без понятия «тревога» | Оператор объекта видит один список «что не так сейчас» и подтверждает; наладчик задаёт пороги без скриптов | M–L (оценка в существующем Python-демоне, UI-список, CGI ack) |
| 2 | Журнал действий: исполнитель, IP, время записи/события, фильтр (`interface/journal`) | нет; `product.md §7` прямо называет отсутствие аудита риском | «Кто перезапустил службу / прошил модуль / сменил сеть» — разбор инцидентов, требования ИБ | S–M (append-only капнутый лог из мутирующих CGI) |
| 3 | Декларативная форма: свойства с `help{ru,en}`, `visibleIf`, `validation`, `advanced`, `def/undef`, `config.default` для сброса | ручные формы в `index.html`, 8 `field-hint` | Подсказка у каждого поля вместо звонка в поддержку; меньше ошибок ввода; «Сбросить к заводским» по полю/секции | M (JSON-дескриптор формы + общий рендерер; allow-list валидации шарится с CGI) |
| 4 | Единый визуальный словарь качества: `_undefined` (пульсация), `_inactive` (серый), offline по `error` в `var_update` | `quality` в `sh-model` (контракт), stale-рамка только в devices | «0» и «нет связи» различимы везде одинаково | S (CSS-атрибут `data-q="bad|uncertain"` + токены обеих тем) |
| 5 | Метрики связи на узел: quality %, среднее время ответа, up/down, `ignore_errors`, `skip_if_timeout` (`proto_mbrtu1.prototype`) | ростер `online`, flasher-health | Наладчик сразу видит «плохой» адрес/кабель на COM-линии | S–M (мост уже знает таймауты — выдать счётчики) |
| 6 | Фейсплейт/клик по значению → мини-тренд в модалке; «Ссылка на текущий интервал» | история открывается из карточки, ссылки нет | Отправить коллеге ссылку «вот провал температуры 14:05–14:20» | S (hash `#devices?id=&metric=&from=&to=`) |
| 7 | Шаблоны уведомлений: `#if`, каналы interface/telegram/email/sms, `%подстановки%`, `%debug%` — список подстановок | сценарии с http-действием, Алиса | Аварийное сообщение в Telegram без облака; `%debug%` — самодокументация | M (+ хранение токена бота 0600) |
| 8 | Параметризованная мнемосхема: одна схема на тип установки, `node:object` параметр, `$node.vars.x`, `{{выражения}}`, click-действия с min/max | нет; `sh-model` `Function` с `layout`; Carel AHU (`carel-ahu.md`) | Оператор ПВУ видит установку «картинкой» — вент., калорифер, фильтр, уставка | L (начать с 1 SVG-шаблона Carel без редактора, привязка к Point'ам) |
| 9 | Календарь рабочих/выходных дней для cron + «задержка между записями» чтобы разнести пусковые токи (`simple_schedule`) | триггеры `time/sun/every` в `sa02m-rules` | Выходные/праздники без ручного отключения сценариев; мягкий пуск нагрузок | M |
| 10 | Авто-выход по бездействию с затемнением за N с (`logout_interval`/`logout_fadetime`) | TTL 10 дней | Панель на объекте не остаётся открытой | S |
| 11 | Права view/edit/exec, наследование от родителя, группы | одна учётка | Минимум: роль «оператор» (смотреть + квитировать) vs «наладчик» | M–L |
| 12 | Обратные ссылки «где используется» (схема → экраны/прототипы) | нет | Перед удалением устройства видно сценарии/Алису/шлюз, что его используют | S–M |
| 13 | Каталог кодов ошибок `errors.json` {name,code} + локализация | ad-hoc строки в CGI | Одинаковые понятные тексты ошибок, код для поддержки | S |
| 14 | Брендинг через конфиг (логотип, картинки входа, файл цветов, ссылка внизу; «не трогайте default_*») | CYNTRON зашит | Партнёрские поставки без форка | S (только декларативные ключи, без JS) |

## 6. Чего НЕ копировать
1. **3 МБ одна страница без сжатия** даже на экране входа (замер: transfer 3 006 513 Б
   = decoded; HTTP/1.1), base64-шрифт, CodeMirror/SimpleMDE/tableexport грузятся
   оператору, который никогда не откроет редактор. Наш раздельный JS + `?v=&r=` лучше;
   держим ленивую загрузку тяжёлых модулей.
2. **SW cache-first на всё без версии** (`cache_worker.js`) — классический «стухший
   бандл против нового бэкенда»; сброс только вручную.
3. **`eval` / `new Function(… with(…))`** (`frame.js:4082`, `frame.js:15316`,
   `onmenu eval(f+'("'+a+'")')`) — несовместимо с CSP; выражения — только через
   белый список парсера без `Function`.
4. **Произвольный JS/HTML из настроек** (`brand_script`, `brand_head`, `brand_body`)
   и **командная строка на переменную** (`drv_cmd`, `service_local_exec__command` с
   `%var_value%`) — RCE-поверхность; всё запущено как root через SysV-скрипт
   (`register` генерирует `/etc/init.d/pult_server`, `cd ${home}; ./start`, без
   смены пользователя) и `register` **отключает SELinux** (`sed SELINUX=disabled`).
5. **Предавторизационная утечка конфига**: `server.js` инлайнится в страницу входа с
   ключами `demo_login/demo_password`, пороги DDoS и т. п. (значения не выводились).
6. **Мусор редактора**: `@prop.json` → `debug{}` сохраняет каждое промежуточное
   нажатие клавиши при наборе имени переменной.
7. **Дубли в склеенных библиотеках** (`ControlCommonProto1` объявлен дважды:
   `controls/common/lib.js:144` и `:6993`) — признак отсутствия сборки/гейта.
8. **Абсолютная пиксельная геометрия** с дробями и отрицательными margin — схемы не
   адаптивны, поэтому понадобилась отдельная мобильная схема; у нас — адаптив.
9. **Файл на переменную** в `data/trends` (10 000 файлов) — тяжело для SD/inode;
   наш SQLite-архив (`sa02m-devices-logger`) правильнее.
10. **Три разных механизма i18n** и `locale()` при загрузке — наш единый `DICT` проще
    держать полным.
11. Ctrl+клик «без вопросов» для квитирования — допустимо, но для необратимых
    действий (прошивка, перезагрузка) у нас подтверждение остаётся.
12. Непрозрачные глобальные имена `P1_N10_var1` — у нас осмысленные MQTT-топики.

## 7. Ограничения аудита
- Живой интерфейс после входа не смотрелся (логин запрещён). Поведение RPC выведено из
  кода клиента и строк бинарей (`pult_study/_strings`), не из трафика.
- «Всё под root» подтверждено только скриптом `register` (SysV без смены
  пользователя); владельцев файлов на плате с Windows-зеркала проверить нельзя.
