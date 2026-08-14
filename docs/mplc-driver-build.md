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
