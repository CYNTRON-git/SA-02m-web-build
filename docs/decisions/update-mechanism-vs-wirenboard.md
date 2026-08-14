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
