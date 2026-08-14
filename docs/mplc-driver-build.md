# Сборка драйверов MPLC4 / MasterSCADA4D (mplc_cyntron, fast_modbus)

Единый дом знаний по сборке нативных драйверов (`.so` функциональных блоков и
протоколов) для исполнительной системы **MPLC4 / MasterSCADA4D RT** на
SA-02m (Allwinner A40i, `armv7hf`, Cortex-A7). Машинно-читаемое (команды, пути,
флаги, имена символов) — на английском; пояснения — по `docLanguage` (ru).

Устройство грузит драйвер `mplc_cyntron.so` в RT; крэш драйвера роняет весь RT
(SIGSEGV на загрузке ФБ). Веб-фича «Установка/обновление MPLC» и стенд зависят
от корректного драйвера — поэтому рецепт живёт здесь.

---

## 1. Первопричина крэша (изучено на стенде 192.168.1.136)

**Симптом:** RT крэш-циклил (SIGSEGV, `LastCode:11`; иногда SIGABRT `LastCode:6`)
сразу после `Loaded FB : … CyntronProtocol …`, ~11 c, пачка core-дампов в
`/var/log/mplc4/dumps/report-*.zip`. Сток-драйвер (142 КБ) — стабилен; правленый
(483 КБ) — крэш.

**Причина — НЕ код, а НЕПРАВИЛЬНЫЙ КОМПИЛЯТОР (ABI C++):**
правленый `.so` был собран **generic**-тулчейном Ubuntu
(`arm-linux-gnueabihf-g++` → линкует `libstdc++.so.6`), а весь RT и его SDK
собраны **вендорным тулчейном mpssoft** (→ `libmpsc++.so`). У двух стандартных
C++-рантаймов несовместимый ABI (`std::string`/`std::vector`/RTTI/исключения),
поэтому при регистрации ФБ первый же обмен объектом через границу драйвер↔RT
разыменовывает мусор → SIGSEGV (SIGABRT — когда исключение переходит границу).

**Доказательство (на любой сборке проверять так):**

```bash
readelf -d  mplc_cyntron.so | grep NEEDED     # ДОЛЖНО: libmpsc++.so ; НЕ ДОЛЖНО: libstdc++.so.6
readelf -p .comment mplc_cyntron.so           # ДОЛЖНО: GCC: (GNU) 13.3.0 ; НЕ: (Ubuntu …)
```

RT-библиотеки подтверждают требование: `readelf -d /opt/mplc4/masterplc.so` (и
`mplcshare.so`) содержат `NEEDED libmpsc++.so` (лежит `/opt/mplc4/libmpsc++.so`).

> Итог: **любой драйвер для MPLC4 ДОЛЖЕН быть собран вендорным тулчейном и
> линковать `libmpsc++.so`.** generic `arm-linux-gnueabihf-g++` даёт крэш.

### 1b. Вторая первопричина — ПОКОЛЕНИЕ SDK-API (изучено 2026-08-14)

Правильный тулчейн — необходимое, но НЕ достаточное условие. Крэш даёт и
**рассинхрон поколения SDK-заголовков с исходником драйвера.** Исходник 4D
`mplc_cyntron` существует в двух несовместимых поколениях API:

| | Старое поколение (крэшит) | Новое поколение (рабочее) |
|---|---|---|
| CA_02m базовый класс | `api::ScadaFB` (`MPLC_In`/`SetEnO`) | `api::ScadaProtocol` (канальная карта) |
| `BaseInit` | 2-арг `(channel, LuaProvider)` | 1-арг `(const vm::Channel*)` |
| запись в канал | `ch->Write(LuaProvider(), v)` / `InVar->m_var` | `ch->InVar.Update(v)` / `OutVar.Get<T>()` |
| регистрация | — | `Register(ch)` на `ScadaProtocol` |

Собрать НОВЫЙ исходник против СТАРЫХ заголовков нельзя (нет символов
`Register`/`Update`/1-арг `BaseInit`); собрать СТАРЫЙ против рантайма нового
поколения → рассинхрон vtable → порча кучи в load-пути → SIGSEGV на регистрации
ФБ (ровно то, что видно в core: `LastCode:11`, мусор в vtable). **Проверять
поколение заголовков ОБЯЗАТЕЛЬНО** — они должны совпадать с рантаймом на
устройстве (символы `ScadaProtocol::Register`, 1-арг `ScadaChannel::BaseInit`
экспортируются в `/opt/mplc4/mplcshare.so` целевого устройства).

> **Где новый SDK:** `Downloads/PCA9536-driver-for-MasterPLC/API/API/`
> (`include/` — заголовки нового поколения; `platform/linux/api/makedrv.sh` —
> Linux-сборка; `lib/` — сторонние заголовки: boost, lua, rapidjson, msgpack,
> sqlite3, mosquitto, lz4, gpiod, uv). Рантайм-`.so` (6 штук, §3) — С УСТРОЙСТВА
> `/opt/mplc4`, чтобы совпасть с боевым рантаймом. Заголовки из старого scaffold
> (или вложенные в старые копии драйвера) — старого поколения, НЕ использовать
> для нового исходника.

---

## 2. Вендорный тулчейн

- **Где взять:** `arm-mpssoft-linux-gnueabihf.zip` —
  `C:/Users/admin/YandexDisk/ЦИНТРОН/Сборка линукс/MasterSCADA/Драйвер микросхемы РСА9536/arm-mpssoft-linux-gnueabihf.zip`
  (полный кросс-GCC 13.3.0 + sysroot, ~144 МБ).
- **Куда ставить:** распаковать в `/opt` на Linux-хосте сборки так, чтобы
  существовал `/opt/arm-mpssoft-linux-gnueabihf/bin/arm-mpssoft-linux-gnueabihf-g++`:

  ```bash
  sudo unzip arm-mpssoft-linux-gnueabihf.zip -d /opt
  /opt/arm-mpssoft-linux-gnueabihf/bin/arm-mpssoft-linux-gnueabihf-g++ --version   # GCC (GNU) 13.3.0
  ```

- Для aarch64-целей есть `/opt/aarch64-mpssoft-linux-gnu` (не наш случай — A40i это armv7hf).
- Хост сборки: **WSL Ubuntu** подходит (`sudo apt-get install -y build-essential`).

---

## 3. Рецепт сборки (общий для всех драйверов)

Целевая платформа для SA-02m — **`linux-armv7hf`** (`-mcpu=cortex-a7`, EABI5, 32-bit ARM).

1. **Исходники** драйвера скопировать на native FS хоста (не drvfs — быстрее и без
   проблем с правами): напр. `~/drvbuild/<driver>`.
2. **SDK-библиотеки:** взять 6 `.so` С УСТРОЙСТВА `/opt/mplc4/` и положить в
   `<project>/platform/linux/api/mplc_lib_so/` (создать каталог):
   `masterplc.so mplc_archive.so mplcshare.so opcua.so liblua.so mplc_events.so`.
   **Важно:** брать именно с целевого устройства (`/opt/mplc4`), а не из копий,
   вложенных в репозиторий драйвера — вложенные могут быть от более старого SDK и
   не совпасть по символам. Забрать по SSH (без SFTP на некоторых прошивках):
   `ssh root@<dev> 'base64 /opt/mplc4/<f>' | base64 -d > mplc_lib_so/<f>`.
3. **Собрать** вендорным тулчейном из `platform/linux/api/`:
   ```bash
   chmod +x build-driver.sh platform/linux/api/makedrv.sh
   ./build-driver.sh linux-armv7hf            # makedrv.sh сам выставит вендорный PATH/CXX/CC
   ```
   `makedrv.sh` для `linux-armv7hf` выставляет:
   `PATH=/opt/arm-mpssoft-linux-gnueabihf/bin`, `CXX=arm-mpssoft-linux-gnueabihf-g++`,
   `CC=arm-mpssoft-linux-gnueabihf-gcc`, `CXXFLAGS+=-mcpu=cortex-a7 -Wno-psabi -Wno-narrowing`.
4. **Проверить ABI** каждого собранного `.so` (см. §1) — до деплоя. Собранный с
   generic-компилятором `.so` НЕ разворачивать.
5. **Проверить символы:** `nm -DC <so> | grep -i cyntron` — продакшн cyntron
   экспортирует `mplc::cyntron::CA_02m`, `mplc::cyntron::i2c_terminate`.

---

## 4. Ловушки (стоили времени)

- **`fast_modbus_MasterSCADA4D_driver/build.sh` жёстко зашивает generic
  `arm-linux-gnueabihf-g++`** — это ровно и даёт крэшащийся бинарь. У проекта нет
  `makedrv.sh`. Обход: вызвать `make` напрямую с вендорным тулчейном:
  ```bash
  make -C platform/linux/api all \
    CXX=arm-mpssoft-linux-gnueabihf-g++ CC=arm-mpssoft-linux-gnueabihf-gcc \
    CXXFLAGS="-Wno-psabi" LDFLAGS="-latomic"
  ```
- **Пример ≠ продакшн.** `PCA9536-driver-for-MasterPLC/examples/mplc_fb_ca02m`
  регистрирует ФБ `CA_02m` через КЛАСС `TestFB` (публичный пример), а боевой драйвер
  — `mplc::cyntron::CA_02m` (+ протокол `CyntronProtocol`). Пример проверяет
  тулчейн/ABI/путь загрузки ФБ, но **байт-в-байт не продакшн**. Боевой исходник —
  `mplc_cyntron` от разработчиков 4D (`cyntron_protocol.cpp`, `CA_02m.cpp`,
  `i2c_system.cpp`). Всегда проверять `nm -DC` на `mplc::cyntron::CA_02m`.
- **PCA9536 корректно переносит отсутствие чипа I2C:** `CA_02m::InitDevice`
  проверяет `i2c_fd < 0` и не падает — «тестовая плата без PCA9536» НЕ причина
  крэша (это была ложная гипотеза; причина — тулчейн).
- **soname у fast_modbus:** SDK-`.so` без `DT_SONAME`, поэтому в `NEEDED`
  собранного `.so` попадают путевые имена (`mplc_lib_so/masterplc.so`). Для
  ABI-проверки это косметика, но перед рантайм-деплоем fast_modbus может
  потребоваться, чтобы эти библиотеки резолвились по такому имени, либо ре-линк с
  `-soname`.
- **Баги нового SDK (передать разработчикам 4D):**
  1. `mplc_cyntron/gpio_helper.cpp` использует `std::runtime_error` без
     `#include <stdexcept>` → не собирается против нового SDK. Добавить include.
  2. Там же `struct gpiohandle_request req;` используется без инициализации
     (заполняется только `lineoffsets[0]`) → занулить `req{}` (эталон
     `ca02m_hw.h` делает `memset`). Латентный дефект.
  3. `API/API/platform/linux/api/makedrv.sh:36` — bash-баг:
     `export CXXFLAGS+=-Wno-psabi -Wno-narrowing` разбирает `-Wno-narrowing` как
     второй аргумент `export` и теряет флаг → безобидный narrowing в boost
     становится hard error на `linux-armv7hf`. Фикс:
     `export CXXFLAGS="$CXXFLAGS -Wno-psabi -Wno-narrowing"`.

---

## 5. Деплой и проверка на устройстве

```bash
# бэкап текущего
ssh root@<dev> 'cp -a /opt/mplc4/mplc_cyntron.so /opt/mplc4/mplc_cyntron.so.bak'
# доставить собранный (пример через base64, если SFTP недоступен)
base64 mplc_cyntron.so | ssh root@<dev> 'base64 -d > /opt/mplc4/mplc_cyntron.so && chmod 755 /opt/mplc4/mplc_cyntron.so'
ssh root@<dev> 'readelf -d /opt/mplc4/mplc_cyntron.so | grep NEEDED'   # снова подтвердить libmpsc++
ssh root@<dev> 'systemctl restart mplc4'
```

**Критерий PASS (проект «Тест» на стенде):** worker `mplc` держит один PID
`State:3`/`LastCode:0` > 60 c, и в `/var/log/mplc4/dumps/` НЕТ новых
`report-*.zip` после рестарта:

```bash
ssh root@<dev> "ps -eo pid,etimes,args | grep '[/]opt/mplc4/./mplc '"     # etimes растёт, PID стабилен
ssh root@<dev> "ls -t /var/log/mplc4/dumps/report-*.zip | head -1"         # не новее рестарта
ssh root@<dev> "grep -oE 'State:[0-9]|LastCode:[0-9]+' /var/log/mplc4/monitor/mplc_monitor.log | tail"
```

**FAIL-safe:** при любом крэше вернуть бэкап:
`cp -a /opt/mplc4/mplc_cyntron.so.bak /opt/mplc4/mplc_cyntron.so && systemctl restart mplc4`.

**Внимание:** много SSH-подключений подряд ловят PAM/sshd-троттлинг (~13 мин
lockout) — устройство при этом пингуется и mplc4 работает; не долбить, дождаться
снятия окна.

---

## 6. Диагностика core-дампов (если понадобится точный бэктрейс)

Дампы: `/var/log/mplc4/dumps/report-*.zip` = `Main.core` (~257 МБ) + `sysinfo.txt`
+ `log.txt` (лог инстанса на момент креша — там видно, на каком ФБ упало). Для
бэктрейса нужен вендорный gdb:

```bash
arm-mpssoft-linux-gnueabihf-gdb /opt/mplc4/mplc Main.core \
  -ex "set sysroot ./sysroot" -ex "bt" -ex "info sharedlibrary"
```
При ABI-крэше верхние фреймы — в `libstdc++.so.6` (string/`type_info`), вызванные
из `mplc_cyntron.so`, при этом в карте библиотек виден и `libmpsc++.so` — «дымящийся
пистолет» смешанных рантаймов.

---

## 7. Где боевой проект (побочно)

- Путь проекта MasterSCADA на устройстве: `/opt/mplc4/server/cfg/`
  (`config.bin` + `ProjInfo.json` + `VMInfo.json` + `_files.xml`; `config.bak`/
  `cfg_files.dat` RT создаёт сам). WorkDirectory RT = `/opt/mplc4/server`.
- Драйверные проекты (исходники): `fast_modbus_MasterSCADA4D_driver`,
  `PCA9536-driver-for-MasterPLC`, и оригинал 4D `mplc_cyntron` — объявлены как
  компоненты в `.ai-dev/components.json`. Их собственная краткая инструкция сборки
  — в их `BUILD.md` (см. эти репозитории).

---

## 8. Боевой `mplc_cyntron.so` в этом репозитории (2026-08-14)

`firmware/mplc4/mplc_cyntron.so` — то, что ставит на устройство кнопка
«Установить MPLC» (`sa02m-web-service-ctl.sh`) и сборка образа
(`create-sa02m-rootfs.sh`, `09-mplc.sh`). **Здесь лежит featured-билд**, а НЕ
чистый вендорный baseline: базовый API нового поколения (Oct-2025 исходник против
SDK `API/API/`) ПЛЮС перенесённые проектные правки — план
`.ai-dev/plans/mplc-driver-project-features.md`:

- **i2c-лок** `/run/lock/sa02m-pca9536.lock` (координация разделяемой шины i2c-2
  с boot-indication `sa02m-pre-start.sh` и веб-путём) + EINTR-retry + проверка
  полной записи;
- **веб-оверрайд беззера** — драйвер сам читает `/run/sa02m-hw-override/beeper.env`
  (bit2, приоритет, TTL); веб-кнопка «беззер» (`hw_set.cgi`→`lib_hw.sh
  sa02m_hw_beeper_override_write`) работает через драйвер, а не через shell-костыль
  `sa02m-beeper-override.sh`;
- **горячее восстановление i2c** — retry раз в 1 с вместо baseline-«умер навсегда»;
- фолт-репорт через `SetFaultState`.

Проверено на стенде 1.136: ABI-гейт (все 3), MPLC4 State:3/LastCode:0 стабильно
>4 мин без дампов, беззер из веба гоняет i2c-2 @0x41 reg 0x01 `0x0b`(вкл)→`0x0f`
(TTL истёк). Сток-бэкап на устройстве `.stockbak*` (`b9e9c618`, 141804 B).
**При замене драйвера пересобирай ОБА слоя (правильный SDK + проектные правки);
голый вендорный исходник теряет функционал.**
