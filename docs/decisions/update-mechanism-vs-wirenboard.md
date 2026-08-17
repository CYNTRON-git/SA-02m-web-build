# Механизм обновления через файл: SA-02m против Wiren Board

Дом решения: сравнить наш файловый механизм обновления (`.sa02m`) с механизмом
Wiren Board (FIT-образ), оценить **корректность нашего** и решить, что (если
что-то) стоит перенять. Machine-facing имена (пути, команды, ключи) — English;
пояснения — ru (`docLanguage`).

Дата: 2026-08. Ветка на момент анализа: `1.0.5.69`. Источник по WB — публичные
репозитории и wiki (ссылки в конце). Источник по SA-02m — код в этом репозитории
(file:line ниже). Runtime-корректность нашего пути **не** подтверждена на
устройстве (см. §5) — только статический анализ кода.

---

## 1. Как это устроено у Wiren Board

WB обновляет **весь rootfs** контроллера через один контейнер **FIT**
(Flattened Image Tree, формат u-boot). Наблюдаемый в логе Оператора поток
(`wb-2606`, `wb7x/bullseye`) совпадает с тем, что описано в исходниках и wiki.

**Контейнер FIT.** Один файл `.fit` содержит метаданные (описание, версия,
модель) плюс несколько бинарных blob'ов: узел `install` (bash-скрипт
`install_update.sh`) и узел `rootfs` (tar.gz корневой ФС). FIT штатно
поддерживает **SHA1-хэш** и **RSA-подпись** для каждой части образа.

**Поток установки** (скрипты `wb-watch-update` и `wb-run-update` из пакета
`wb-utils`):

1. `.fit` кладётся в каталог загрузки (`/mnt/data/.wb-update/`, либо через
   `curl` на `/fwupdate/upload`, либо с USB).
2. `wb-watch-update` детектирует полностью загруженный файл.
3. `wb-run-update` извлекает из FIT свойство `hash@1` для узла `install`,
   считает `sha1sum` этого blob'а и **сверяет SHA1** — при несовпадении
   прерывается («SHA1 of … doesn't match»). Это соответствует первому шагу
   лога Оператора («SHA1 hash check of install»).
4. Проверка совместимости `check_firmware_compatible`: наличие нужного DTB в
   образе, поддержка макета разделов (`single-rootfs` / A/B), extended-rootfs,
   динамический u-boot env. Это строка лога `Firmware compatible: +extended-rootfs
   +single-rootfs +fit-factory-reset +force-repartition +… +update-from-cloud` —
   набор **capability-флагов**, которые образ требует, а устройство подтверждает.
5. `install`-скрипт извлекается во временный файл и исполняется (`source`).
6. Скрипт проверяет **SHA1 rootfs** и распаковывает tar.gz в **неактивный**
   слот rootfs.
7. Перезагрузка — переключение на новый слот.

**A/B (двойной rootfs) + откат по bootcount.** Разметка eMMC/SD:
`p2` (rootfs A, ~1 ГБ) и `p3` (rootfs B, ~1 ГБ), общий `/mnt/data` (`p6`).
Обновление всегда пишется в **неактивный** слот; активный не трогается.
Переключение — через u-boot env: `mmcpart` (2 или 3), `upgrade_available=1`,
и **watchdog отката по счётчику загрузок**: `bootcount` растёт при каждом входе
в u-boot, при превышении `bootlimit` `altbootcmd` меняет `mmcpart` обратно и
сбрасывает счётчик — то есть неудачная новая прошивка **автоматически**
откатывается на прежний слот. Это даёт устойчивость к «кирпичу».

**Factory-reset** — отдельный режим: FIT с суффиксом `FACTORYRESET` (или
удержание кнопки), очистка data-раздела (`rsync --delete`), сохранение текущего
FIT как заводского. Флаг `fit-factory-reset` в capability-строке.

**update-from-cloud** — capability-флаг: образ умеет тянуться из облака WB, а не
только загружаться вручную.

Ключевые свойства WB: контейнер = **весь rootfs**; **immutable** активный
слот (обновляется всегда сосед); **A/B + автооткат по bootcount** на уровне
загрузчика; SHA1 обязателен, RSA-подпись опциональна; repartition и
factory-reset как режимы установки.

---

## 2. Как устроено у нас (SA-02m, `.sa02m` v1)

Фундаментальное отличие: мы поставляем **overlay из файлов** (`www/`, `opt/sa02m-*`,
helpers в `/usr/local`, юниты `sa02m-*`, ключи updater), **не** образ rootfs, не
ядро, не загрузчик. Это Armbian, обновляется только слой веб-стека и его сервисов.
Дом формата: `docs/OFFLINE_UPDATE_PACKAGE_V1.md`.

**Контейнер `.sa02m`** (`scripts/pack-offline-update.py:559-591`,
`opt/sa02m-update/lib/validate_package.py:112-134`): внешний POSIX-tar +
21-байтный footer `SA02M_UPDATE_END_V1\0\0`. Внутри четыре члена:
`manifest.json`, `manifest.sig`, `payload.tar.gz`, `payload.sha256`. `payload`
— gzip(ustar) с overlay-файлами.

**Целостность — двойной SHA-256**
(`validate_package.py:711-722`): длина payload == `manifest.payload.size`;
`sha256(payload)` == `manifest.payload.sha256` **и** == sidecar-член
`payload.sha256`. Плюс footer-magic и выравнивание `tar_size % 512`
(`:124-134`). Есть внешний sidecar `.sha256` на весь файл
(`pack-offline-update.py:594-598`).

**Аутентичность — Ed25519 подпись**
(`validate_package.py:392-449`): подпись считается над **domain-separated**
canonical JSON манифеста (`SIG_DOMAIN=b"SA02M-MANIFEST-V1\0"` + отсортированный
компактный JSON, `:137-145`), проверяется против `etc/sa02m-update/trusted-keys/*.pem`
(сначала по `signing_key_id`, потом до 8 запасных ключей). Приватный ключ —
только на release-машине (`private/sa02m-update-keys/`, gitignored). **Для
файлового пути подпись обязательна** (`sa02m-update-runner.sh:601-653` — путь
`allow_unsigned=0`, при провале `E_SIG`).

**Совместимость** (`validate_package.py:644-682`): жёстко `product==SA-02m`,
`model==A40i`, `arch==armv7l`; semver-гейты `installed >= min_version`,
`target > installed` (строго!), `runner >= min_updater`; `manifest.version` ==
первая строка `VERSION` внутри payload (`:670-682`). Значения `min_updater`,
`min_version` зашиты в packer (`pack-offline-update.py:44-45`).

**Атомарность / откат** — журнальный, per-file, **не A/B**
(`sa02m-update-runner.sh`):
- Каждый файл ставится атомарно: temp → `fdatasync` → `mv -f` → `fsync`
  каталога (`:793-813`).
- Перед перезаписью — резервная копия и запись операции в `journal.jsonl`
  (`:815-860`), плюс общий tar-архив прежних файлов (`build_rollback_archive`,
  `:751-784`).
- Любой сбой на deploy/delete/migrations/health → `rollback_from_journal`
  проигрывает журнал в обратном порядке (`:908-954`, `:1141-1162`).
- **Health-gate до коммита** (`:957-1028`): `nginx -t`, HTTP-проба
  `login.html`, активность юнитов, сверка `VERSION` == target; провал → откат.
- **Устойчивость к потере питания**: `imaging lock` + `RuntimeWatchdogSec=0` на
  окно применения (`:178-199`); self-copy re-exec раннера в staging до deploy
  (`:271-287`); команда `recover` до-водит по сохранённой `stage` в
  восстановленной `stage` в `transaction.json` (`:1174-1241`), юнит
  `sa02m-update-recover.service`.
- Сохраняются пользовательские данные — `PRESERVE_PATHS` (креды, `/etc/sa02m_*.conf`,
  сети, htpasswd, шаблоны, cloud, flasher/alice state; `:29-42`,
  `validate_package.py:42-55`); deploy в эти пути **запрещён** валидатором
  (`:284-285`).

**Веб-слой** (`www/network_config/cgi-bin/`): `web_update_upload.cgi` принимает
multipart → `upload_receive.py` → `sudo sa02m-update-inspect` (валидация без
установки, показывает версию/подпись/совместимость); `web_update_apply.cgi` при
`confirm_version` требует **CSRF** (`:176-181`), пишет `transaction.json` и
запускает `sudo systemctl start sa02m-update.service`. Auth-cookie проверяется
до всего (`:25-29`).

**Второй путь — OTA через GitHub** (`web_update_check.cgi`,
`etc/sa02m-web-update-check.sh`): сравнивает задеплоенный commit/версию с tip
ветки `main` на GitHub (semver, `:38-55`), вкладка «Обновление» показывает
доступность. Применение (`source=github`) идёт через тот же раннер, но
overlay строится из git-checkout, и **подпись НЕ проверяется**
(`sa02m-update-runner.sh:289-445` `prepare_github_overlay` → `signature_ok=false`,
`allow_unsigned=1`). Доверие здесь — TLS к GitHub + сам репозиторий, не крипто-подпись.

---

## 3. Сравнительная таблица

| Свойство | Wiren Board | SA-02m |
|---|---|---|
| Контейнер | FIT (u-boot Image Tree), 1 файл | tar + footer, 4 члена (`.sa02m`) |
| Что обновляется | **весь rootfs** + install-скрипт | **overlay файлов** (www/opt/helpers/units) |
| Целостность | SHA1 install-blob и rootfs | **SHA-256 ×2** (manifest + sidecar) payload |
| Аутентичность | RSA-подпись (опционально), SHA1 обяз. | **Ed25519 обязательна** (файл); OTA — без подписи |
| Совместимость | capability-флаги (DTB, rootfs-layout, …) | product/model/arch + semver-гейты + VERSION-in-payload |
| Атомарность/откат | **A/B слоты + автооткат по bootcount** (u-boot) | журнал per-file + health-gate + power-loss recover |
| Встроенный install-скрипт | **да** (`install_update.sh` в FIT, self-contained) | нет — логика в раннере на устройстве (manifest декларативен) |
| Factory-reset | да (режим FIT / кнопка) | отдельный `sa02m-factory-reset-runner.sh` (вне пакета обновления) |
| Repartition | да (capability) | неприменимо (overlay) |
| Cloud-trigger | `update-from-cloud` (тянет образ) | OTA-check к GitHub (тянет overlay из репо) |
| Offline-путь | `.fit` с USB/загрузки | `.fit`… нет — `.sa02m` через веб-загрузку |
| Сохранение данных | отдельный `/mnt/data` раздел | `PRESERVE_PATHS` (deploy туда запрещён) |

---

## 4. Что мы делаем правильно (и работает ли это корректно)

По **статическому анализу кода** файловый путь спроектирован строго и, по
логике, корректно для overlay-модели:

- **Целостность — сильнее, чем у WB.** SHA-256 (против SHA1 у WB), причём
  двойная сверка (манифест + sidecar), плюс проверка размера, footer и
  выравнивания. tar-bomb/traversal/symlink/device-защита при распаковке
  (`validate_package.py:541-594`). ✓
- **Аутентичность есть и она enforced для файла.** Ed25519 над
  domain-separated canonical JSON — правильная схема (нет signature-stripping,
  нет canonicalization-неоднозначности). Приватный ключ не в git. ✓
- **Откат честный и многослойный.** Per-file атомарная замена, журнал с
  бэкапами, health-gate до коммита, recover после потери питания. Для overlay
  это адекватная замена A/B: «плохой» релиз откатывается на прежние файлы, а не
  оставляет полу-применённое состояние. ✓
- **Гейты совместимости строгие.** product/model/arch + semver + сверка VERSION
  внутри payload с манифестом — исключает применение чужого/битого/older пакета. ✓
- **Данные пользователя защищены** декларативным запретом deploy в
  `PRESERVE_PATHS` на уровне валидатора, а не только раннера. ✓

**Работает ли корректно у нас?** По коду — **да, путь целостный и
безопасный**. Но **runtime-корректность из кода не доказать**; нужны
on-device проверки (стенд 192.168.1.136, сейчас SSH throttled — тест на потом):

1. **Полный e2e на стенде**: загрузить подписанный `.sa02m` N+1 через вкладку
   «Обновление» → inspect показывает version/signature_ok=true/compatible=true →
   apply → дойти до `stage=done`, `progress_pct=100`, версия в UI сменилась,
   nginx/fcgiwrap/devices-api живы. (В `.tmp/` уже есть черновики
   `test_web_update_offline_e2e.sh` — их и прогнать.)
2. **Негативные кейсы**: (a) битый payload (сломать байт) → `E_HASH`; (b)
   подмена подписи/чужой ключ → `E_SIG`; (c) `target <= installed` → `E_COMPAT`;
   (d) обрыв питания в `stage=applying` → после ребута `recover` откатывает,
   UI показывает прежнюю версию, сервисы живы.
3. **Проверить окружение устройства**: `openssl pkeyutl -verify` для Ed25519 и
   `tarfile … filter="data"` (Python ≥ 3.12) реально доступны на плате — обе
   ветки есть с fallback, но подтвердить на железе.

Пока эти три пункта не прогнаны на стенде, утверждать «обновление через файл
работает корректно» можно только на уровне **дизайна кода**, не runtime.

**Найденные слабые места / грабли (foot-guns):**

- **OTA-путь (GitHub) без крипто-подписи.** Асимметрия: файл — подписан Ed25519,
  а OTA деплоит **неподписанный** overlay из git-checkout
  (`sa02m-update-runner.sh:604-605`, `signature_ok=false`). Доверие держится на
  TLS к GitHub и целостности репо. Не «дыра» (HTTPS + приватный репо), но это
  **более слабый** trust-anchor, чем у файлового пути, и его стоит осознавать
  как таковой (не полагаться на «у нас всё подписано» — OTA не подписан).
- **`target > installed` строго** (`validate_package.py:667`,
  `_verify…:667-668`). Нельзя переустановить ту же версию файлом для «ремонта»
  повреждённого деплоя и нельзя откатиться на старую версию через файл. Это
  осознанный guard (перекликается с «версия ниже прячет обновление» из
  `sa02m-domain.md`), но одновременно и recovery-foot-gun: восстановление
  «поверх той же версии» через штатный путь невозможно.
- **После отката сервисы не перезапускаются повторно.** `rollback_from_journal`
  восстанавливает файлы, но не гоняет `restart_services_and_health` заново
  (`:908-954`). Для CGI это ок (nginx/fcgiwrap читают скрипт с диска на каждый
  запрос), но python-демоны (`sa02m-devices-*`) после отката продолжат работать
  на уже перезапущенных-и-откаченных файлах в памяти — до ручного/ребутного
  рестарта. Мелкий, но реальный гэп консистентности.
- **Класс CRLF/sudoers** (недавние коммиты `67b7e10`, `03898e4`,
  `e30c47b`): раннер деплоит файлы **verbatim** из payload; CRLF в sudoers или
  скрипте ломает исполнение. Сейчас закрыто нормализацией на LF (`text=auto
  eol=lf`) — держать инвариант при добавлении новых файлов в allowlist.
- **Много `python3 -c` форков на apply** — на shared ARM SoC это заметная
  нагрузка, но apply одноразовый, приемлемо.

---

## 5. Что перенять у WB (ранжировано по value/effort)

Наша модель — **overlay на Armbian**, а не FIT-rootfs, поэтому не всё
переносимо. Ранжирование для нашей реальности:

**Стоит рассмотреть:**

1. **Capability-флаги совместимости (высокий value / низкий effort).** У WB
   образ объявляет требуемые фичи, устройство их подтверждает
   (`+extended-rootfs`, `+single-rootfs`, …). У нас гейт бинарный
   (product/model/arch + semver). Добавить в manifest необязательное поле
   `requires: []` (например `requires-python-3.12`, `requires-openssl-ed25519`,
   `requires-hw-variant: any|1eth|2eth`) и проверять на устройстве до backup —
   даст внятный отказ вместо неудачи в середине apply. Малый объём, повышает
   диагностируемость. Схема манифеста уже additionalProperties:false — поле
   добавляется в валидатор без ломки формата (bump `schema_version` или
   optional-ветка).

2. **Подписать OTA-путь так же, как файловый (высокий value / средний effort).**
   Закрыть асимметрию §4: публиковать вместе с релизом на GitHub подписанный
   `manifest.sig` (или отдавать `.sa02m` как release-asset) и гонять OTA через
   тот же Ed25519-путь, а не `allow_unsigned=1`. Тогда «всё подписано» станет
   правдой для обоих путей. Это наш собственный gap, но WB (RSA-подпись частей
   FIT) — прецедент, что подпись уместна и для «облачного» апдейта.

3. **Авто-watchdog отката на уровне загрузки для критичных апдейтов (средний
   value / высокий effort).** У WB bootcount/bootlimit откатывает «кирпич»
   автоматически. У нас health-gate откатывает **файлы**, но если апдейт когда-то
   начнёт трогать что-то, что валит загрузку (сейчас — нет, только overlay),
   такой сети нет. Пока scope = overlay, это **избыточно**; заносить в бэклог
   только если появится обновление ядра/загрузчика.

**Не стоит копировать (over-engineering для overlay-модели):**

- **A/B двойной rootfs + repartition.** Мы не поставляем rootfs; два слота и
  переразметка eMMC не имеют смысла для файлового overlay. Наш журнальный откат
  покрывает тот же риск в нашем масштабе.
- **FIT-контейнер.** Формат заточен под u-boot загрузку образов; для набора
  файлов tar+manifest проще и уже работает. Менять контейнер = чистая стоимость
  без выгоды.
- **Встроенный self-contained install-скрипт в пакете.** У WB install-скрипт
  едет внутри FIT (гибкость под меняющийся rootfs-layout). У нас логика на
  устройстве в раннере, а manifest **декларативен** (deploy[]/services[]) — это
  **безопаснее** (нельзя протащить произвольный код установки в пакете), терять
  это ради «как у WB» не нужно.
- **Factory-reset как режим пакета обновления.** У нас это отдельный
  `sa02m-factory-reset-runner.sh`, и правильно, что отдельно — смешивать сброс с
  обновлением повышает риск.

---

## 6. Итог

Наш файловый путь по **дизайну** строже WB по целостности (SHA-256×2 против
SHA1) и по аутентичности файлового пакета (обязательная Ed25519 против
опциональной RSA), а журнальный откат + health-gate + power-loss recover
адекватно заменяют A/B для overlay-модели. Главное «но»: **runtime-корректность
не подтверждена на устройстве** (нужен e2e + негативные кейсы на стенде), и есть
**асимметрия подписи** (OTA-путь неподписан). Из WB реально полезны две дешёвые
вещи — **capability-флаги совместимости** и **подпись OTA-пути**; A/B, FIT,
repartition и встроенный install-скрипт для нашей overlay-модели избыточны или
менее безопасны.

---

## Будущая адаптация (не сейчас — наш вариант оставляем)

Статус: отложено (Оператор 2026-08-14) — текущий механизм рабочий; это чек-лист
к внедрению при следующей ревизии обновлений.

Ниже — две рекомендации из §5, доведённые до **implementation-ready** состояния
с привязкой к file:line. **Не реализовывать сейчас**, только как ТЗ для будущей
сессии.

### A. Capability-флаги совместимости — `requires: []` в manifest

**Проблема.** Сейчас гейт совместимости бинарный: `product==SA-02m`,
`model==A40i`, `arch==armv7l` + semver (`validate_package.py:644-667`,
`check_device_compat`). Если пакет требует чего-то от окружения (версия Python
для `filter="data"`, поддержка Ed25519 в openssl, конкретный HW-вариант), а его
нет — отказ случится **в середине apply**, а не до backup. WB решает это
capability-флагами (`+extended-rootfs +single-rootfs …`), которые образ требует,
а устройство подтверждает.

**Что добавить — опциональное поле `requires`.**

- **Схема (manifest v1 → v1.1).** Поле `requires: []` — список
  capability-токенов (строки из фиксированного словаря). **Опциональное**:
  отсутствует ⇒ принимаем (текущее поведение, backward-compat со всеми уже
  выпущенными и legacy-пакетами). Токены — из allow-list, напр.:
  `python>=3.12`, `openssl-ed25519`, `hw-variant:any|1eth|2eth`,
  `updater>=1.0.5.66`. Значения — декларативные, семантику каждого токена знает
  устройство.
- **Где править схему-валидатор.** `opt/sa02m-update/lib/validate_package.py`:
  1. добавить `"requires"` в `_MANIFEST_TOP_KEYS` (`:79-98`) — иначе
     `_reject_unknown` (`:152-155`, вызывается в `validate_manifest_object:196`)
     отвергнет пакет с новым полем как `E_MANIFEST` (additionalProperties:false).
     **Важно:** это значит, что пакет с `requires` НЕ пройдёт на устройстве со
     старым валидатором — поэтому поле вводить синхронно с bump `min_updater`,
     либо трактовать его как «мягкое» (см. ниже риск).
  2. в `validate_manifest_object` (`:192-343`) добавить типовую проверку:
     `requires` — список строк из словаря, каждая матчит регэксп токена.
  3. новую функцию `check_capabilities(manifest, device_caps)` рядом с
     `check_device_compat` (`:644-667`); вызывать из `validate_package(...)`
     под флагом `check_compat` (`:752-759`), сразу после `check_device_compat`.
- **Как устройство объявляет свои capabilities.** Собрать `device_caps` на
  устройстве в раннере до backup: HW-вариант из `/etc/sa02m_hw_variant.conf`
  (`sa02m-1eth|sa02m-2eth`), версия Python (`python3 -V`), наличие
  `openssl-ed25519` (проба `openssl pkeyutl`), версия раннера
  (`UPDATER_VERSION`, `sa02m-update-runner.sh:15`). Передать их в валидатор так
  же, как сейчас передаются `installed_version`/`runner_version`
  (`sa02m-update-runner.sh:483-490`).
- **Где авторится manifest.** `scripts/pack-offline-update.py:489-556`
  (`build_manifest`) — добавить `"requires": [...]` в возвращаемый dict (сейчас
  его там нет; поле поедет в подпись автоматически, т.к. подпись считается над
  всем canonical JSON, `:455-456`). Значение брать из нового необязательного
  аргумента packer'а или из константы рядом с `MIN_UPDATER`/`MIN_VERSION`
  (`:44-45`).
- **Fail-closed отказ ДО apply.** При несовпадении — `PackageError("E_COMPAT",
  "device missing capability: <token>")` из `check_capabilities`, что уже
  маппится раннером в `stage=error` до backup (`:1100-1119`, ветка
  `run_validate_and_extract`). Сообщение показывается в inspect
  (`web_update_upload.cgi` → `sa02m-update-inspect`), т.е. Оператор видит отказ
  **до** нажатия «Применить».
- **Effort / risk.** Effort низкий (одно опциональное поле + одна функция +
  сбор `device_caps`). Risk: **не сломать существующие пакеты** — отсутствие
  `requires` обязано означать «принять»; и помнить про additionalProperties —
  добавление ключа в `_MANIFEST_TOP_KEYS` не ломает старые пакеты (у них поля
  нет), но пакет С полем упадёт на старом валидаторе. Поэтому: либо релизить
  `requires` только когда все стенды на новом раннере, либо в первую итерацию
  packer вообще не эмитит поле, пока устройства не обновят валидатор (валидатор
  готов раньше, эмиссия — позже). Подпись/старые пакеты не затрагиваются.

### B. Подписать OTA/GitHub-путь тем же Ed25519 (закрыть асимметрию)

**Асимметрия (pin с file:line).**

- **Файловый путь — подпись обязательна.** `sa02m-update-runner.sh:460`
  (`run_validate_and_extract`: `[ "$allow_unsigned" != "1" ]` → идёт в
  `validate_package`, который **всегда** проверяет Ed25519,
  `validate_package.py:705-709`); bootstrap-ветка тоже требует подпись, иначе
  `E_SIG` (`sa02m-update-runner.sh:601-653`).
- **OTA/GitHub путь — БЕЗ подписи.** `sa02m-update-runner.sh:456-458`:
  `if [ "$(txn_get source)" = "github" ]; then allow_unsigned=1`. Overlay
  строится из git-checkout (`prepare_github_overlay`, `:289-445`) и жёстко
  проставляет `signature_ok=false` (`:440`, `:443`, и `:1106`
  `txn_patch "signature_ok=false"`). Легаси-запуск OTA идёт вообще мимо раннера:
  `web_update_apply.cgi:333` `nohup sudo -n /usr/local/sbin/sa02m-web-update-apply`.
- **Проверка «свежести» OTA — только по commit/semver** через GitHub API/raw
  (`etc/sa02m-web-update-check.sh:64-116`), без крипто-верификации содержимого.

**Угроза.** Компрометация или MITM канала GitHub (подменённый релиз/ветка,
угнанный токен, перехваченный TLS) позволяет протолкнуть **неподписанный**
overlay, который OTA-путь применит как доверенный — тогда как файловый путь тот
же overlay без валидной Ed25519-подписи отверг бы (`E_SIG`). Trust-anchor OTA =
«TLS + целостность репозитория», что слабее нашего же файлового пути.

**Фикс, по убыванию предпочтения.**

- **(a) Требовать тот же Ed25519-подписанный пакет и на OTA.** Публиковать на
  каждый релиз `SA-02m-update-<version>.sa02m` как GitHub release-asset (packer
  уже его собирает и подписывает, `pack-offline-update.py`). OTA-check скачивает
  asset, а apply гонит его через **тот же** `validate_package` (убрать
  `allow_unsigned=1` для `source=github`, `sa02m-update-runner.sh:456-458`).
  Переиспользовать те же `/etc/sa02m-update/trusted-keys/*.pem`
  (`validate_package.py:108`, `:416-427`) — новых ключей не нужно. Это делает
  «всё подписано» правдой для обоих путей одним изменением ветки.
- **(b) Подписанный release-манифест (hash-list) поверх TLS + pinned key.** Если
  тянуть не единый `.sa02m`, а per-file overlay: публиковать подписанный
  Ed25519 список `path→sha256`, проверять его тем же trusted-key на устройстве
  перед deploy. Больше кода (новый формат manifest'а для OTA), тот же ключевой
  материал.
- **Точная ветка к правке:** `sa02m-update-runner.sh:456-458` (снять
  `allow_unsigned` для github) + `prepare_github_overlay` (`:289-445`) — заменить
  сборку из checkout на скачивание+валидацию подписанного asset; и увести
  легаси `web_update_apply.cgi:318-344` на транзакционный путь раннера.
- **Effort / risk.** Effort средний (release-процесс: заливка asset + смена OTA
  apply-ветки на валидатор). **Миграция:** старые устройства и уже выпущенные
  legacy-релизы без asset — на время выката оставить fallback (если подписанного
  asset для коммита нет ⇒ OTA недоступен/помечен «unsigned, не применять
  автоматически», а не тихо применяется). Не ломать файловый путь: он уже
  подписан и не меняется.

### C. Что НЕ берём у WB и почему

- **A/B двойной rootfs + автооткат по bootcount.** Мы поставляем overlay
  файлов (`www/`, `opt/sa02m-*`, helpers, units), а не образ rootfs — двух
  слотов и переключения `mmcpart` в u-boot просто нет чему обслуживать. Тот же
  риск («плохой релиз») у нас закрыт журнальным откатом + health-gate +
  power-loss `recover` (`sa02m-update-runner.sh:908-954`, `:957-1028`,
  `:1174-1241`). Заносить A/B имеет смысл только если обновление начнёт трогать
  ядро/загрузчик — сейчас не трогает.
- **Repartition.** Прямое следствие A/B; для файлового overlay неприменимо —
  мы не управляем разделами eMMC.
- **FIT-контейнер.** Формат заточен под загрузку образов u-boot'ом. Для набора
  файлов наш `tar + footer + manifest` (`OFFLINE_UPDATE_PACKAGE_V1.md`) проще,
  уже работает и уже подписан; смена контейнера = стоимость без выгоды.
- **Встроенный self-contained install-скрипт (`install_update.sh` в FIT).** У WB
  install-логика едет **внутри** пакета — гибко под меняющийся rootfs-layout, но
  это **исполняемый код из пакета**. У нас manifest **декларативен**
  (`deploy[]`/`delete[]`/`services[]`, `validate_package.py:269-321`), а логика
  применения — на устройстве в раннере, фиксированная и проверяемая. Это
  **безопаснее**: пакет не может протащить произвольный install-код; поверхность
  атаки уже. Терять это ради «как у WB» не нужно.

---

## Источники

WB (публичные):

- https://github.com/wirenboard/wirenboard — сборка rootfs, `image/install_update.sh`
- https://github.com/wirenboard/wb-utils — `wb-watch-update`, `wb-run-update`
- https://github.com/wirenboard/wb-utils/blob/master/utils/bin/wb-watch-update
- https://wiki.wirenboard.com/wiki/index.php/WB_Firmware_Update_Details — FIT, A/B, bootcount, SHA1/RSA
- https://wiki.wirenboard.com/wiki/Wiren_Board_7_Firmware_Update
- https://github.com/wirenboard/wb-update-manager

SA-02m (этот репозиторий):

- `docs/OFFLINE_UPDATE_PACKAGE_V1.md`, `docs/deployment.md`
- `scripts/pack-offline-update.py`
- `opt/sa02m-update/lib/validate_package.py`
- `etc/sa02m-update-runner.sh`, `etc/sa02m-web-update-check.sh`
- `www/network_config/cgi-bin/web_update_{upload,apply,check,cancel}.cgi`
