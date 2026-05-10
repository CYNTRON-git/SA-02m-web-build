# Сборка `mplc_cyntron.so` на устройстве

Этот документ фиксирует **рабочий порядок сборки и замены драйвера `mplc_cyntron.so` прямо на устройстве СА-02м** для установленного `MPLC 4` в каталоге `/opt/mplc4`.

Документ основан на реально выполненной процедуре на устройстве:

- устройство: `192.168.1.136`
- целевой runtime: `mplc4.service`
- целевой бинарник: `/opt/mplc4/mplc_cyntron.so`

## Область применения

Инструкция относится **только к драйверу `mplc_cyntron.so`** для `MPLC 4`, установленного в `/opt/mplc4`.

Она **не относится** к:

- web-интерфейсу `/var/www/network_config`
- split-примерам `mplc_protocol_ca02m.so` / `mplc_fb_ca02m.so`
- сборке образа Buildroot
- другим конфигурациям `MPLC`

## Почему нужен именно такой путь

На устройстве реально используется **один модуль**:

```bash
/opt/mplc4/mplc_cyntron.so
```

Новый GitHub-репозиторий `PCA9536-driver-for-MasterPLC` содержит актуальный SDK и примеры, но на устройстве runtime загружает именно `mplc_cyntron.so`.

Поэтому для рабочего обновления нужно:

1. взять **исходники** старого `mplc_cyntron`
2. взять **заголовки SDK** из `PCA9536-driver-for-MasterPLC`
3. адаптировать старый код под **текущий API SDK**
4. собрать новый `mplc_cyntron.so`
5. заменить `/opt/mplc4/mplc_cyntron.so`

## Что было исправлено в коде

Перед сборкой были нужны такие правки:

1. `gpio_helper.cpp`
   - добавить `#include <stdexcept>`

2. `cyntron_protocol.h/.cpp`
   - адаптировать под текущий API `MPLC`
   - убрать старый вызов `Register(...)`
   - заменить `BaseInit(channel)` на `BaseInit(channel, LuaProvider())`
   - заменить прямые `InVar.Update(...)` на `ch->Write(LuaProvider(), ...)`
   - вместо старой `unordered_map` использовать список созданных каналов

Без этой адаптации драйвер не собирается на актуальном SDK.

## Исходные каталоги

### Исходники драйвера

Локальный каталог со старыми исходниками:

```text
C:\Users\admin\YandexDisk\ЦИНТРОН\Сборка линукс\MasterSCADA\Драйвер микросхемы РСА9536\От разработчиков 4D\mplc_cyntron
```

### SDK-заголовки и библиотеки

Локальный каталог нового репозитория:

```text
C:\Users\admin\YandexDisk\ЦИНТРОН\Сборка линукс\MasterSCADA\Драйвер микросхемы РСА9536\PCA9536-driver-for-MasterPLC
```

## Что нужно на устройстве

Минимально:

```bash
gcc
g++
make
bash
python3
unzip
```

Проверка:

```bash
which gcc g++ make bash python3 unzip
```

## Главные проблемы, которые были на реальном устройстве

Во время реальной сборки обнаружились важные ограничения:

1. `/tmp` почти полностью расположен на `tmpfs` и быстро заполняется
2. на устройстве было **0 MB swap**
3. `cc1plus` падал по памяти при прямой сборке
4. SSH-соединение было нестабильным, поэтому большие деревья файлов лучше переносить архивами
5. старый `mplc_cyntron` использовал **старый API SDK**, поэтому требовалась адаптация `cyntron_protocol.*`

Из-за этого **рабочий вариант** был таким:

- сначала подготовить дерево SDK во временном каталоге
- затем перенести build workspace из `/tmp` в `/root`
- включить временный swap
- собирать **последовательно по объектам**

## Рабочий порядок сборки

Ниже приведён именно тот порядок, который реально сработал.

---

## 1. Подключение к устройству

Пример из PowerShell:

```powershell
$K = "C:\Users\admin\Downloads\SA-02m-web-build\private\.ssh\sa02m_sa02"
ssh -i $K -o StrictHostKeyChecking=accept-new root@192.168.1.136
```

## 2. Проверка целевого драйвера на устройстве

Убедиться, что runtime использует именно `mplc_cyntron.so`:

```bash
ls -l /opt/mplc4/mplc_cyntron.so
strings /opt/mplc4/mplc_cyntron.so | grep -i -E 'cyntron|CA_02m' | head
systemctl status mplc4.service --no-pager
```

## 3. Подготовка временного каталога

На устройстве:

```bash
rm -rf /tmp/mplc_cyntron_build
mkdir -p /tmp/mplc_cyntron_build/examples
mkdir -p /tmp/mplc_cyntron_build/include
mkdir -p /tmp/mplc_cyntron_build/lib
```

## 4. Что нужно перенести на устройство

Нужно перенести:

### Из старого `mplc_cyntron`

Весь каталог:

```text
mplc_cyntron/
```

### Из нового `PCA9536-driver-for-MasterPLC`

Минимальный рабочий набор:

- `include/core`
- `include/share`
- `include/mplc`
- `lib/boost`
- `lib/msgpack`
- `lib/msgpack.hpp`
- `lib/opcua`
- `lib/rapidjson`
- `lib/lua.hpp`
- `lib/lua.h`
- `lib/luaconf.h`
- `lib/lualib.h`
- `lib/lauxlib.h`

На практике переносить удобнее не деревьями по `scp -r`, а **zip-архивами**.

## 5. Рабочий способ переноса SDK

На Windows/PowerShell в каталоге `PCA9536-driver-for-MasterPLC`:

```powershell
Compress-Archive -Path .\include\mplc -DestinationPath .\include_mplc_full.zip -Force
Compress-Archive -Path .\lib\boost -DestinationPath .\boost_headers.zip -Force
Compress-Archive -Path .\lib\msgpack, .\lib\msgpack.hpp -DestinationPath .\msgpack_headers.zip -Force
Compress-Archive -Path .\lib\opcua -DestinationPath .\opcua_headers.zip -Force
Compress-Archive -Path .\lib\rapidjson -DestinationPath .\rapidjson_headers.zip -Force
```

Далее на устройство:

```powershell
$K = "C:\Users\admin\Downloads\SA-02m-web-build\private\.ssh\sa02m_sa02"
$H = "root@192.168.1.136"

scp -i $K .\include_mplc_full.zip "${H}:/tmp/mplc_cyntron_build/"
scp -i $K .\boost_headers.zip "${H}:/tmp/mplc_cyntron_build/"
scp -i $K .\msgpack_headers.zip "${H}:/tmp/mplc_cyntron_build/"
scp -i $K .\opcua_headers.zip "${H}:/tmp/mplc_cyntron_build/"
scp -i $K .\rapidjson_headers.zip "${H}:/tmp/mplc_cyntron_build/"
```

И отдельно маленькие Lua-заголовки:

```powershell
scp -i $K .\lib\lua.hpp    "${H}:/tmp/mplc_cyntron_build/lib/lua.hpp"
scp -i $K .\lib\lua.h      "${H}:/tmp/mplc_cyntron_build/lib/lua.h"
scp -i $K .\lib\luaconf.h  "${H}:/tmp/mplc_cyntron_build/lib/luaconf.h"
scp -i $K .\lib\lualib.h   "${H}:/tmp/mplc_cyntron_build/lib/lualib.h"
scp -i $K .\lib\lauxlib.h  "${H}:/tmp/mplc_cyntron_build/lib/lauxlib.h"
```

Старые исходники:

```powershell
scp -r -i $K "C:\Users\admin\YandexDisk\ЦИНТРОН\Сборка линукс\MasterSCADA\Драйвер микросхемы РСА9536\От разработчиков 4D\mplc_cyntron" "${H}:/tmp/mplc_cyntron_build/examples/"
```

## 6. Распаковка SDK на устройстве

На устройстве:

```bash
cd /tmp/mplc_cyntron_build

unzip -oq include_mplc_full.zip -d include
unzip -oq boost_headers.zip -d .
unzip -oq msgpack_headers.zip -d lib
unzip -oq opcua_headers.zip -d lib
unzip -oq rapidjson_headers.zip -d lib
```

После распаковки должны существовать:

```bash
test -f /tmp/mplc_cyntron_build/include/mplc/api.h
test -f /tmp/mplc_cyntron_build/boost/type_traits/is_abstract.hpp
test -f /tmp/mplc_cyntron_build/lib/msgpack.hpp
test -f /tmp/mplc_cyntron_build/lib/opcua/opcua.h
test -f /tmp/mplc_cyntron_build/lib/rapidjson/document.h
test -f /tmp/mplc_cyntron_build/lib/lua.hpp
```

## 7. Почему сборка не должна идти в `/tmp`

На реальном устройстве было так:

```bash
free -m
swapon --show
df -h /tmp /root /opt
```

Фактически:

- RAM около `492 MB`
- swap отсутствовал
- `/tmp` был почти заполнен (`tmpfs`)

Поэтому рабочий вариант:

1. включить временный swap
2. перенести build workspace в `/root`

## 8. Включение временного swap

На устройстве:

```bash
fallocate -l 1024M /root/cursor_build.swap || dd if=/dev/zero of=/root/cursor_build.swap bs=1M count=1024
chmod 600 /root/cursor_build.swap
mkswap /root/cursor_build.swap
swapon /root/cursor_build.swap

free -m
swapon --show
```

Если swap уже существует:

```bash
swapon --show
```

## 9. Перенос рабочего дерева из `/tmp` в `/root`

На устройстве:

```bash
rm -rf /root/mplc_cyntron_build
mkdir -p /root/mplc_cyntron_build
cp -a /tmp/mplc_cyntron_build/. /root/mplc_cyntron_build/
```

С этого момента собирать нужно **только** в `/root/mplc_cyntron_build`.

## 10. Рабочий build script

Это рабочий вариант, который реально собрал `mplc_cyntron.so` на устройстве:

```sh
#!/bin/sh
set -e
ROOT=/root/mplc_cyntron_build
cd "$ROOT"
mkdir -p build/obj

CXXFLAGS="-std=gnu++17 -Wno-narrowing -fpermissive -O0 -g0 -fPIC -D_DEFAULT_SOURCE -DLINUX -D_GNUC_ -I$ROOT -I$ROOT/lib -I$ROOT/lib/opcua -I$ROOT/include -I$ROOT/include/core -I$ROOT/examples/mplc_cyntron"

g++ $CXXFLAGS -c examples/mplc_cyntron/CA_02m.cpp -o build/obj/CA_02m.o
g++ $CXXFLAGS -c examples/mplc_cyntron/cyntron_protocol.cpp -o build/obj/cyntron_protocol.o
g++ $CXXFLAGS -c examples/mplc_cyntron/gpio_helper.cpp -o build/obj/gpio_helper.o
g++ $CXXFLAGS -c examples/mplc_cyntron/i2c_system.cpp -o build/obj/i2c_system.o
g++ $CXXFLAGS -c examples/mplc_cyntron/mplc_cyntron.cpp -o build/obj/mplc_cyntron.o

g++ -shared -fPIC \
  build/obj/CA_02m.o \
  build/obj/cyntron_protocol.o \
  build/obj/gpio_helper.o \
  build/obj/i2c_system.o \
  build/obj/mplc_cyntron.o \
  -Wl,-Map,build/mplc_cyntron.map \
  -Wl,-rpath,/opt/mplc4 \
  -Wl,-rpath-link,/opt/mplc4 \
  /opt/mplc4/masterplc.so \
  /opt/mplc4/mplc_archive.so \
  /opt/mplc4/mplcshare.so \
  /opt/mplc4/opcua.so \
  /opt/mplc4/liblua.so \
  /opt/mplc4/mplc_events.so \
  -latomic -lm -ldl -lrt -lpthread \
  -o build/mplc_cyntron.so
```

Почему именно так:

- `-O0 -g0` уменьшает потребление памяти компилятора
- сборка по объектам не убивает `cc1plus`
- `-I$ROOT/lib/opcua` нужен для `#include <opcua.h>`
- `-I$ROOT/lib` нужен для `msgpack`, `rapidjson`, `lua.hpp`
- `-I$ROOT` нужен для `boost/...`, так как `boost` был распакован в корень build-каталога

## 11. Запуск сборки

На устройстве:

```bash
chmod 755 /root/mplc_cyntron_build/build.sh
/root/mplc_cyntron_build/build.sh
```

Проверка результата:

```bash
ls -l /root/mplc_cyntron_build/build/mplc_cyntron.so
ls -l /root/mplc_cyntron_build/build/mplc_cyntron.map
```

Ожидаемый результат:

- `mplc_cyntron.so`
- `mplc_cyntron.map`

## 12. Backup текущего драйвера перед заменой

На устройстве:

```bash
ts=$(date +%Y%m%d_%H%M%S)
b=/root/backup/mplc_cyntron_$ts
mkdir -p "$b"
cp -f /opt/mplc4/mplc_cyntron.so "$b/"
echo "$b"
```

Пример реально созданного backup:

```text
/root/backup/mplc_cyntron_20260504_115736
```

## 13. Замена драйвера

На устройстве:

```bash
cp -f /root/mplc_cyntron_build/build/mplc_cyntron.so /opt/mplc4/mplc_cyntron.so
chmod 755 /opt/mplc4/mplc_cyntron.so
sync
```

Проверка:

```bash
ls -l /opt/mplc4/mplc_cyntron.so
```

## 14. Перезапуск `mplc4.service`

На устройстве:

```bash
systemctl restart mplc4.service
sleep 3
systemctl is-active mplc4.service
systemctl status mplc4.service --no-pager
```

Ожидаемый статус:

```text
active
```

## 15. Проверка зависимостей

После замены:

```bash
ldd /opt/mplc4/mplc_cyntron.so
```

Важно:

- на этом устройстве `ldd` может показывать предупреждения из-за окружения runtime и системных библиотек
- ключевая практическая проверка здесь — **`mplc4.service` должен успешно стартовать и остаться `active`**

## 16. Rollback

Если нужно откатить:

```bash
cp -f /root/backup/mplc_cyntron_YYYYMMDD_HHMMSS/mplc_cyntron.so /opt/mplc4/mplc_cyntron.so
chmod 755 /opt/mplc4/mplc_cyntron.so
systemctl restart mplc4.service
```

## 17. Очистка после сборки

Если драйвер проверен и больше не нужен build workspace:

```bash
rm -rf /tmp/mplc_cyntron_build
rm -rf /root/mplc_cyntron_build
```

Если swap больше не нужен:

```bash
swapoff /root/cursor_build.swap
rm -f /root/cursor_build.swap
rm -f /root/.cursor_mplc_swap_enabled
```

## 18. Краткий чек-лист

### Перед сборкой

```bash
which gcc g++ python3 unzip
free -m
swapon --show
ls -l /opt/mplc4/mplc_cyntron.so
```

### После переноса SDK

```bash
test -f /root/mplc_cyntron_build/include/mplc/api.h
test -f /root/mplc_cyntron_build/boost/type_traits/is_abstract.hpp
test -f /root/mplc_cyntron_build/lib/msgpack.hpp
test -f /root/mplc_cyntron_build/lib/opcua/opcua.h
test -f /root/mplc_cyntron_build/lib/rapidjson/document.h
test -f /root/mplc_cyntron_build/lib/lua.hpp
```

### После сборки

```bash
ls -l /root/mplc_cyntron_build/build/mplc_cyntron.so
```

### После deployment

```bash
systemctl is-active mplc4.service
ls -l /opt/mplc4/mplc_cyntron.so
```

## 19. Что оказалось критично

Итог по реальной процедуре:

1. Нельзя просто собрать старый `mplc_cyntron` "как есть" против нового SDK
2. Нельзя надёжно собирать в `/tmp` на этом устройстве
3. Без swap компилятор убивается по памяти
4. Надёжнее переносить SDK архивами, а не большими `scp -r`
5. Рабочая целевая точка deployment — **только** `/opt/mplc4/mplc_cyntron.so`

## 20. Фактически применённый результат

Во время рабочей процедуры были получены:

- собранный файл:

```text
/root/mplc_cyntron_build/build/mplc_cyntron.so
```

- установленный драйвер:

```text
/opt/mplc4/mplc_cyntron.so
```

- backup старого драйвера:

```text
/root/backup/mplc_cyntron_20260504_115736
```

- состояние сервиса после замены:

```text
mplc4.service = active
```

