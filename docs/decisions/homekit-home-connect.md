# Решение: Phase 0 (go/no-go) для моста Apple HomeKit и BSH Home Connect

**Дата:** 2026-09-27. **Статус:** настольная часть Phase 0 выполнена (G1 без
замера на плате, G5 полностью, G7 по публичным источникам); стендовые строки
G2, G3, G4, G6 **не выполнены** — процедуры ниже; G8 — позиция Оператора; G9
открыт. Ни одна невыполненная строка не засчитана как пройденная.

Что решено: мост HomeKit — собственный демон на HAP-python 5.0.0 в отдельном
venv (`opt/sa02m-homekit/requirements.lock`), все нужные доработки поведения
библиотеки делаются подклассами без её правки. По плану G2–G4 — условия
go/no-go для Phase 1; код Phase 1 собирается на этой ветке по решению
Оператора (Q-C, Q-G), но пока эти три строки не пройдены на стенде, выпуск
Phase 1 не проверен на железе — выпускать ли его до стенда, решает Оператор.

Все версии, команды, пути и идентификаторы — в английском виде (машинные
факты). Проза русская (`docLanguage: ru`).

## Сводка G1–G9

| # | Проверка | Статус |
|---|---|---|
| G1 | Зависимости и `requirements.lock` | десктоп: выполнено (хэши 16/16, отрицательные тесты); установка на плате — НЕ ВЫПОЛНЕНО |
| G2 | Нагрузка 50/149 аксессуаров на плате | НЕ ВЫПОЛНЕНО |
| G3 | mDNS / порт 5353 на обоих вариантах | НЕ ВЫПОЛНЕНО |
| G4 | Сопряжение с реальным iPhone | НЕ ВЫПОЛНЕНО |
| G5 | Хуки HAP-python без патча библиотеки | выполнено: 4 хука без патча, стенд 19/19 PASS, RED на каждой мутации |
| G6 | Home Connect из России | НЕ ВЫПОЛНЕНО |
| G7 | Home Connect: неизвестные из публичных источников | частично (публичные источники); аккаунт и симулятор — НЕ ВЫПОЛНЕНО |
| G8 | Юридическая позиция | позиция Оператора записана: некоммерческое, необязательная установка, «не сертифицировано Apple» |
| G9 | Проба спроса | ОТКРЫТО (действие Оператора) |

## G1 — набор зависимостей и `requirements.lock`

**Статус: выполнено на десктопе (x86_64 + скачанные armv7l-колёса); установка на
самой плате — НЕ ВЫПОЛНЕНА** (замер «< 5 мин на плате» остаётся за стендом, см.
процедуру в конце раздела).

**Цель платы** (`README.md`, раздел про образ; `RES §D`): Armbian 25.11.2 noble
(Ubuntu 24.04), `armv7l`/`armhf`, Python 3.12.3, glibc 2.39.

### Полное замыкание

HAP-python 5.0.0 — последняя версия на PyPI на 2026-09-27; её `requires_dist`
(PyPI JSON `https://pypi.org/pypi/HAP-python/5.0.0/json`):
`cryptography`, `chacha20poly1305-reuseable`, `orjson>=3.7.2`,
`zeroconf>=0.36.2`, `h11`, `async_timeout; python_version < "3.11"` (на 3.12
не нужен), extra `qrcode` (`base36`, `pyqrcode`) — не ставим: QR-матрицу строит
наш код через `segno` (`opt/sa02m-homekit/sa02m_homekit/setup_payload.py`).

| Пакет | Версия | Зачем | Колесо для платы (armv7l) |
|---|---|---|---|
| `hap-python` | 5.0.0 | HAP-сервер | `py3-none-any` |
| `cryptography` | 50.0.1 (последняя) | Ed25519/X25519/HKDF | `cp311-abi3-manylinux_2_31_armv7l` |
| `chacha20poly1305-reuseable` | 0.13.2 | шифрование сессии (`cryptography>=43.0.0`) | `py3-none-any` |
| `orjson` | 3.12.0 | JSON HAP | `cp312-cp312-manylinux2014_armv7l` |
| `zeroconf` | 0.151.3 | mDNS (`ifaddr>=0.1.7`) | `cp312-cp312-manylinux2014_armv7l…manylinux_2_31_armv7l` |
| `ifaddr` | 0.2.0 | зависимость zeroconf | `py3-none-any` |
| `h11` | 0.16.0 | HTTP/1.1 HAP | `py3-none-any` |
| `segno` | 1.6.6 | QR для карточки | `py3-none-any` |
| `_cffi_backend` | apt `python3-cffi-backend` 1.16.0-2build1 | C-модуль, нужный `cryptography` в рантайме | — (из apt, не из PyPI) |
| `paho-mqtt` | apt `python3-paho-mqtt` 1.6.1-1 (noble universe, `all`) | MQTT-клиент демона (план D1: переиспользуется через `--system-site-packages`) | — (из apt, в lock не входит) |

Почему apt, а не PyPI для cffi: `cryptography 50.0.1` объявляет
`cffi>=2.0.0`, но у `cffi` на PyPI **нет ни одного armv7l-колеса ни в одном
релизе** (просмотр всех `releases` в `https://pypi.org/pypi/cffi/json`: 0 файлов
с `armv7`; последняя версия 2.1.1) — без компилятора его не поставить. В рантайме
`cryptography` нужен только C-модуль `_cffi_backend`, а он в noble есть:
`python3-cffi-backend 1.16.0-2build1`, `Architecture: armhf`
(`http://ports.ubuntu.com/ubuntu-ports/dists/noble/main/binary-armhf/Packages`).
Поэтому контракт установки — **`--system-site-packages` + `pip install
--no-deps`**, он записан в шапке lock-файла. Почему не откат на
`cryptography 46.0.0` (там `cffi>=1.14` для Python < 3.14): 46.0.0 вышла
2025-09-16, 50.0.1 — 2026-08-25; держать криптобиблиотеку HAP на год старее
ради метаданных pip хуже, чем одна строка `--no-deps`, которую ниже проверяет
отрицательный тест.

### Откуда lock и как он проверен (команды, 2026-09-27)

Все хэши сверены с PyPI JSON API (`https://pypi.org/pypi/<name>/<ver>/json`,
поле `urls[].digests.sha256`): **16 из 16 хэшей lock-файла найдены на PyPI и
указывают на ожидаемые файлы** (armv7l-колесо для платы + x86_64-колесо для
CI/разработки там, где колесо не pure-Python). Требования `requires_dist` всех
восьми пакетов замкнуты lock-файлом, кроме `cffi` (см. выше).

```sh
# 1. Скачать колёса ИМЕННО для платы по lock-файлу (хэши проверяет pip):
pip download --no-deps --only-binary=:all: \
  --platform manylinux_2_31_armv7l --platform manylinux2014_armv7l \
  --platform manylinux_2_17_armv7l --python-version 3.12 \
  --implementation cp --abi cp312 --abi abi3 --abi none \
  -d wa -r opt/sa02m-homekit/requirements.lock      # exit 0, 8 колёс
sha256sum wa/*                                       # совпало с lock
# 2. Что требуют бинарники от glibc платы:
readelf -V <каждый .so> | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1
#    -> GLIBC_2.30 (cryptography/_rust.abi3.so), остальные <= GLIBC_2.4;
#    у платы glibc 2.39 — подходит. NEEDED у _rust.abi3.so: только
#    libgcc_s, libpthread, libdl, libc, ld-linux-armhf (OpenSSL статически).
# 3. Установка по контракту (Python 3.12.3 + apt python3-cffi-backend 1.16.0,
#    как на плате; x86_64-колёса):
python3.12 -m venv --system-site-packages vsys
vsys/bin/pip install --no-deps -r opt/sa02m-homekit/requirements.lock
#    -> exit 0 за 4,2 с (из кэша прокси, не показатель для платы)
vsys/bin/python smoke.py   # Ed25519 sign/verify, X25519, HKDF-SHA512,
#    ChaCha20Poly1305Reusable round-trip, import pyhap/zeroconf/orjson/segno
#    -> "SMOKE OK", _cffi_backend взят из /usr/lib/python3/dist-packages
```

Отрицательные проверки (каждая ОБЯЗАНА упасть — и упала):

| Мутация | Результат |
|---|---|
| установка **без** `--no-deps` | exit 1: `In --require-hashes mode, all requirements must have their versions pinned with ==. These do not: cffi>=2.0.0` — pip закрывается, а не тянет непроверенный пакет |
| venv **без** `--system-site-packages` | установка проходит, но импорт падает: `ModuleNotFoundError: No module named '_cffi_backend'` |
| один символ хэша `hap-python` изменён | exit 1: `THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE` |

Чего это **не** доказывает: что armv7l-бинарники реально грузятся на Cortex-A7
(контейнер x86_64, эмуляции нет) и сколько длится установка на плате.

### Процедура для стенда (Оператор; плата 192.168.1.136 и одна SA-02m-2)

```sh
scp opt/sa02m-homekit/requirements.lock root@192.168.1.136:/tmp/
ssh root@192.168.1.136 '
  set -e
  apt-get install -y python3-venv python3-cffi-backend
  python3 -m venv --system-site-packages /tmp/hk-venv
  time /tmp/hk-venv/bin/pip install --no-deps -r /tmp/requirements.lock
  /tmp/hk-venv/bin/python -c "import pyhap.accessory_driver, cryptography, zeroconf, orjson, segno; print(\"import OK\", cryptography.__version__)"
  du -sh /tmp/hk-venv
  rm -rf /tmp/hk-venv'
```

Критерий прохода: `time` < 5 мин, строка `import OK 50.0.1`. Записать сюда
время, `du -sh` и `uname -m`.

## G2 — нагрузка 50 / 149 аксессуаров на плате

**Статус: НЕ ВЫПОЛНЕНО** — нужна плата (Cortex-A7, 492 MiB); в контейнере
только x86_64. Числа x86 (RSS 38–40 MiB, `RES §D`) — ориентир, не результат.

Пороги go/no-go (план, D8): установившийся RSS ≤ 48 MiB при 50 и ≤ 56 MiB при
149 аксессуарах; средний CPU ≤ 2 % ядра в простое; pair-setup (SRP 3072 бит на
чистом Python) ≤ 10 с; venv на диске ≤ 60 MB.

Процедура (плата 192.168.1.136; одноразовый скрипт, в репозиторий не входит;
печатает код в терминал — только для стенда):

```sh
# на плате, venv из процедуры G1 (не удалять в конце G1)
cat > /tmp/hkload.py <<'PY'
import os, sys, threading, time
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
N, IFACE_IP = int(sys.argv[1]), sys.argv[2]
drv = AccessoryDriver(address=IFACE_IP, port=21064, persist_file="/tmp/hkload.state",
                      interface_choice=[IFACE_IP])
br = Bridge(drv, "SA02m-load-%d" % N)
for i in range(N):
    a = Accessory(drv, "Relay %d" % i)
    a.add_preload_service("Switch").configure_char("On", setter_callback=lambda v: None)
    br.add_accessory(a)
drv.add_accessory(br)
def probe():
    hz = os.sysconf("SC_CLK_TCK"); prev = None
    while True:
        f = open("/proc/self/stat").read().split(); cpu = (int(f[13]) + int(f[14])) / hz
        rss = [l for l in open("/proc/self/status") if l.startswith("VmRSS")][0].split()[1]
        if prev: print("%s RSS=%.1fMiB CPU=%.2f%%" % (time.strftime("%T"), int(rss)/1024,
                       100*(cpu-prev[1])/(time.time()-prev[0])), flush=True)
        prev = (time.time(), cpu); time.sleep(60)
threading.Thread(target=probe, daemon=True).start()
drv.start()
PY
IP=$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
rm -f /tmp/hkload.state; timeout 1900 /tmp/hk-venv/bin/python /tmp/hkload.py 50 "$IP"  | tee /tmp/hkload-50.log
rm -f /tmp/hkload.state; timeout 1900 /tmp/hk-venv/bin/python /tmp/hkload.py 149 "$IP" | tee /tmp/hkload-149.log
du -sh /tmp/hk-venv
```

Во время прогона на 149: добавить мост с iPhone и засечь секундомером время от
ввода кода до «Добавлено» (pair-setup). Записать сюда: RSS последней трети
прогона, средний CPU, время pair-setup, `du -sh`, а также загрузку CODESYS/MPLC4
в тот же момент (`top -bn1 | head -15`).

## G3 — mDNS и порт 5353 на обоих вариантах

**Статус: НЕ ВЫПОЛНЕНО** — нужен образ платы и iPhone/Mac в той же сети.
Из кода известно (`RES §D`): python-zeroconf открывает UDP 5353 с
`SO_REUSEADDR|SO_REUSEPORT` (`zeroconf/_utils/net.py`), поэтому уживается с
avahi; жёсткий конфликт — только владелец 5353 без `SO_REUSEPORT`.

```sh
ssh root@192.168.1.136 '
  ss -ulpn "sport = :5353"
  systemctl is-active avahi-daemon systemd-resolved
  resolvectl status | grep -i -E "mdns|multicast"
  grep -rhi MulticastDNS /etc/systemd/resolved.conf /etc/systemd/resolved.conf.d/ 2>/dev/null'
# затем скрипт G2 с N=5: на SA-02m по eth0, на SA-02m-2 по eth1
#   (IP=$(ip -4 -o addr show eth1 | awk ...))
# с Mac в той же сети:    dns-sd -B _hap._tcp
# или с Linux-ноутбука:    avahi-browse -rt _hap._tcp
```

Критерий: `_hap._tcp` мост виден с Mac/iPhone на обоих вариантах; на SA-02m-2
объявление приходит только с выбранного интерфейса; `ss` не показывает
владельца 5353, мешающего запуску. Записать вывод `ss` и `resolvectl`.

## G4 — сопряжение с настоящим iPhone

**Статус: НЕ ВЫПОЛНЕНО** — нужны плата, iPhone и (для удалённого доступа)
домашний центр Apple (Apple TV / HomePod).

Процедура (после установки сборки ветки на стенд штатным путём
`docs/deployment.md`, не правкой файлов на плате):

1. Карточка «HomeKit» на вкладке «Управление» → включить, интерфейс eth0.
   В «Умный дом» отметить «Показывать в HomeKit» у одного DO и одного ДТВ.
2. iPhone → «Дом» → «Добавить аксессуар» → QR с карточки →
   «Несертифицированный аксессуар» → «Всё равно добавить».
3. Переключить DO из «Дома»; на плате проверить публикацию:
   `mosquitto_sub -h 127.0.0.1 -t '/devices/+/controls/+/on' -v` и физический
   выход.
4. Переключить тот же DO с веб-интерфейса — состояние в «Доме» обновилось.
5. Температура ДТВ в «Доме» совпадает с вкладкой «Устройства».
6. Без домашнего центра: iPhone в той же Wi-Fi — управление есть; на
   мобильных данных — «Нет ответа» (ожидаемо).
7. С домашним центром: повторить п. 6 на мобильных данных — управление через
   центр. Этот удалённый путь (через iCloud) записать в модель угроз.
8. «Сбросить сопряжение» на карточке → мост снова показывает QR, в «Доме»
   аксессуар «Нет ответа».

Записать: версию iOS, модель центра, результат каждого шага, время п. 2.

## G5 — хуки HAP-python без патча библиотеки

**Статус: выполнено (десктоп, x86_64, Python 3.12.3, venv ровно по
`requirements.lock` — HAP-python 5.0.0, cryptography 50.0.1). Вердикт: все
четыре хука реализуемы подклассом или публичным аргументом конструктора;
библиотека не патчится.** Реализация — `opt/sa02m-homekit/sa02m_homekit/bridge.py`
(единственный модуль, импортирующий `pyhap`; его шапка перечисляет хуки).

| Хук (план) | Точка расширения HAP-python 5.0.0 | Что делает наш код |
|---|---|---|
| D6 блокировка перебора кода | подкласс `HAPServerHandler` (`LockoutHandler`), подставляется через подклассы `HAPServerProtocol.connection_made` и `HAPServer.async_start`; `SafeDriver` кладёт свой `SafeHAPServer` в `self.http_server` | считает только неверное SRP-доказательство pair-setup (M3→M4); после `PAIR_SETUP_MAX_FAILS` (=100, `constants.py`) отвечает `kTLVError_MaxTries` (0x05) до перезапуска демона. В библиотеке этого требования HAP нет |
| D4 надёжная запись хранилища сопряжений | переопределение `AccessoryDriver.persist()` | `fsutil.atomic_write`: tmp `.hk-*.tmp` в том же каталоге → fsync файла → rename → fsync каталога. Библиотечный `persist()` делает `os.replace` без fsync (`pyhap/accessory_driver.py:637-663`) |
| D4 (продолжение) код не меняется после рестарта | публичный параметр `encoder=` (`SafeEncoder`, подкласс `AccessoryEncoder`) | в хранилище дописываются `pincode` и `setup_id`; иначе рестарт выдал бы новый код при старом QR у iOS |
| D5 код не печатается в журнал | переопределение `Bridge.setup_message()` (`SafeBridge`) | no-op. Библиотека печатает код и QR в stdout (`pyhap/accessory.py:251`), вызов — `accessory_driver.py:408`, когда мост не сопряжён |
| §5.3 пересборка без потери сопряжений | штатная загрузка `persist_file` в `AccessoryDriver.add_accessory()` + `State.set_accessories_hash()` | пересборка = остановка драйвера и новый `BridgeRunner` на том же `state.json`; сопряжения и ключ аксессуара сохраняются, `c#` растёт только при изменении набора аксессуаров |

### Как доказано

Скретч-стенд (в репозиторий не входит): минимальный HAP-контроллер
(«iPhone») говорит с **немодифицированным** HAP-python по настоящему TCP на
127.0.0.1 — HTTP, TLV8, SRP-6a pair-setup, Ed25519/X25519 pair-verify идут
библиотечным кодом. Подменён только mDNS: пустой `AsyncZeroconf` через
публичный аргумент `async_zeroconf_instance` (mDNS — строка G3). Порог
блокировки снижен до 3 публичным аргументом `max_pair_setup_fails`
конструктора `SafeDriver`. Команда:

```sh
TMPDIR=<scratch> <venv-по-lock>/bin/python g5_real.py        # GREEN
MUT=<hook> TMPDIR=<scratch> <venv-по-lock>/bin/python g5_real.py   # RED
```

Результат GREEN: **19/19 PASS, exit 0**:

| # | Проверка | Итог |
|---|---|---|
| P0a | ни один из 1000 атрибутов модулей `pyhap` не переприсвоен после импорта `bridge.py` (нет monkeypatch) | PASS |
| P0b | 23 файла `pyhap/` совпадают с sha256 из `RECORD` колеса (библиотека не правлена) | PASS |
| P1 | старт несопряжённого моста: stdout пуст — ни кода, ни `X-HM://` | PASS |
| P2a | `state.json` — режим 0600, содержит `pincode` и `setup_id` | PASS |
| P2b | порядок вызовов: `fsync(.hk-*.tmp)` → `replace(→ state.json)` → `fsync(каталог)` | PASS |
| P2c/P2e | после записи и после остановки сайдкаров `.hk-*.tmp` не остаётся (сайдкар, видимый в момент старта, — параллельная запись самого драйвера в executor; он исчезает, как только запись завершается) | PASS |
| P2d | рестарт с другим «новым» кодом сохраняет прежний код и `setup_id` | PASS |
| P3a | 5 неудачных pair-verify от неизвестного контроллера не увеличивают счётчик | PASS |
| P3b | 3 неверных кода → 3 ответа M4 «authentication» (0x02), мост заблокирован | PASS |
| P3c | в блокировке даже **верный** код получает M2 `kTLVError_MaxTries` (0x05) | PASS |
| P3d | в блокировке никто не сопрягся | PASS |
| P4a | блокировка переживает внутрипроцессную пересборку, если счётчик передан | PASS |
| P4b/P4c | после рестарта демона верный код сопрягает; pair-verify проходит | PASS |
| P4d/P4e | пересборка с другим набором аксессуаров: та же пара проходит pair-verify (тот же ключ аксессуара), `pairings`=1, `c#` 2→3 | PASS |
| P4f | сопряжённый мост отвергает второй pair-setup (M2 0x06) | PASS |
| P4g | пересборка без изменения набора: `c#` не растёт | PASS |

Каждая проверка **видела RED** на откате ровно одного хука к библиотечному
поведению (правится копия нашего класса в памяти стенда, не библиотека):

| Мутация `MUT=` | Что упало |
|---|---|
| `setup` (библиотечный `setup_message`) | P1 — в stdout `Enter this code in your HomeKit app…` с кодом |
| `persist` (библиотечный `persist`) | P2b — только `replace('tmp…', 'state.json')`, ни одного fsync |
| `lockout` (штатный обработчик) | P3b, P3c (верный код после 3 ошибок **сопряг**), P3d |
| `encoder` (штатный энкодер) | P2a, P2d (после рестарта код стал `999-99-999`), P4b–P4f |
| `freshstore` (пересборка без старого `state.json`) | P4d, P4e (`c#` 2→2, пары нет), P4f |

### Модульные тесты демона (`opt/sa02m-homekit/tests/`)

```sh
python -m unittest discover -s opt/sa02m-homekit/tests -t opt/sa02m-homekit -v
```

Прогон 2026-09-27, два venv на Python 3.12.3 с HAP-python 5.0.0 — с
`cryptography` 46.0.0 и ровно по lock (50.0.1): **оба — Ran 191 tests, OK
(skipped=1)**; пропуск — `test_without_segno_returns_none` (путь «segno
отсутствует» проверяется там, где segno нет). Хуки `bridge.py` закреплены в
наборе постоянно, это не только скретч-стенд:

| Хук | Тесты |
|---|---|
| блокировка | `test_bridge_glue`: `test_default_limit_is_the_hap_spec_100`, `test_every_connection_gets_the_lockout_handler`, `test_wrong_codes_lock_pair_setup_until_restart`, `test_the_count_survives_an_in_process_rebuild` |
| `persist()` | `test_bridge_glue`: `test_persist_is_fsynced_0600_via_hk_tmp_and_passes_our_store_check` |
| `setup_message()` | `test_no_code_leak`: `test_setup_message_is_overridden`, `test_building_and_announcing_the_bridge_prints_no_code`, `test_lifecycle_never_emits_the_code` |
| пересборка | `test_bridge_glue`: `test_a_rebuild_keeps_pairings_code_and_keys`, `test_pairing_changes_are_signalled` |

Чего G5 **не** доказывает: поведение с настоящим iOS (G4), mDNS (G3),
ресурсы на armv7l (G2).

## G6 — Home Connect из России

**Статус: НЕ ВЫПОЛНЕНО.** Из контейнера сервисы Home Connect недоступны вовсе:
`curl -X POST https://api.home-connect.com/security/oauth/device_authorization`
и то же для `simulator.home-connect.com` → `CONNECT tunnel failed, response 403`
(исходящий прокси контейнера, 2026-09-27). Контейнер к тому же не в РФ, так что
даже успешный ответ не был бы ответом на G6.

Процедура (со стенда в России, 192.168.1.136):

```sh
ssh root@192.168.1.136 '
  curl -sS -m 20 -o /tmp/hc.json -w "%{http_code}\n" -X POST \
    https://api.home-connect.com/security/oauth/device_authorization \
    -d client_id=dummy; cat /tmp/hc.json; echo
  curl -sS -m 20 -o /dev/null -w "simulator %{http_code}\n" \
    https://simulator.home-connect.com/'
```

Критерий 1: HTTP 4xx с JSON-ошибкой (`invalid_client` или подобной) — API
доступен с российского IP; таймаут/сброс/403 от CDN — недоступен.
Критерий 2 (руками): создать аккаунт SingleKey ID **из России** в приложении
Home Connect, привязать один прибор или симулятор, затем пройти Device Flow с
карточки «Home Connect» на плате. Записать страну аккаунта, что показало
приложение, и прошёл ли вход. Отрицательный результат не отменяет Phase 2
(решение Оператора, Q-G), но должен попасть в описание карточки.

## G7 — Home Connect: открытые вопросы по публичным источникам

**Статус: частично — только по публичным источникам.** Аккаунт разработчика
CYNTRON и сквозной Device Flow на симуляторе **НЕ ВЫПОЛНЕНЫ**: аккаунта нет, а
`developer.home-connect.com`, `api-docs.home-connect.com` и сам API из
контейнера закрыты прокси (`.ai-dev/plans/homekit-home-connect-research.md`
§G; проверка G6 выше). Ниже — что удалось установить и откуда.

| Вопрос (`RES §G`) | Ответ | Источник | Вывод для кода |
|---|---|---|---|
| Время жизни `device_code`/`user_code`: 5 или 10 мин? | Приходит в поле `expires_in` ответа `/security/oauth/device_authorization`; по истечении — ошибка `expired_token`, нужен новый запрос. Числа 5 и 10 мин — из разных версий CHANGELOG homebridge-homeconnect (строки 856 и 620) | [snippet] <https://api-docs.home-connect.com/authorization/>; `homebridge-homeconnect@f568d07` CHANGELOG | не зашивать срок: брать `expires_in` и `interval` (по умолчанию 5 с) из ответа |
| Время жизни access-токена | `expires_in` 86400 с (24 ч) | [snippet] та же страница; `RES §C` | обновлять заранее (homebridge — за 1 ч, `api-ua-auth.ts:58`) |
| Время жизни refresh-токена | **не найдено** ни в документации (закрыта), ни в поиске; в трекере HA — жалобы на невалидный refresh-токен без указания срока | <https://github.com/home-assistant/core/issues/140121>, <https://github.com/home-assistant/core/issues/69959> | обрабатывать отказ refresh как «нужен повторный вход» на карточке, не как сбой демона |
| Ограничено ли неопубликованное приложение своим тестовым аккаунтом | **не проверено**; вывод из шагов настройки homebridge-homeconnect (в приложении указывается «Home Connect User Account for Testing») | `RES §C` «Client ID registration» | для v1 снято решением Оператора Q-B: интегратор вводит СВОЙ Client ID со своим аккаунтом |
| Лимиты | 1000 вызовов/сутки на client+user; обновление токена ≤ 10/мин; HTTP 429 с `Retry-After` | `RES §C` (HA issue #139328, homebridge `platform.ts:36`, CHANGELOG:636, `api-ua.ts:287-292`) | SSE вместо опроса; уважать `Retry-After` |

Что остаётся Оператору: завести аккаунт разработчика CYNTRON
(<https://developer.home-connect.com>), создать приложение с OAuth Flow =
Device Flow, пройти вход на симуляторе с карточки платы и записать сюда
фактические `expires_in`/`interval` из ответа.

## G8 — юридическая позиция

**Статус: позиция Оператора записана (2026-09-27).** Это позиция Оператора, а
не юридическое заключение.

Факты (`RES §D` «Certification for HAP»): спецификация HAP Non-Commercial
лицензируется «solely … for own personal, non-commercial use … not for
distribution or sale»; коммерческие аксессуары HomeKit проходят MFi/HomeKit-
сертификацию; несертифицированный мост сопрягается после «Несертифицированный
аксессуар → Всё равно добавить». HAP-python — Apache-2.0.

Позиция Оператора (дословно: «Используем не для коммерческого использования»),
принятые последствия:

- использование заявлено как **некоммерческое**;
- мост HomeKit — **необязательная установка**, в заводской образ не входит,
  по умолчанию выключен;
- карточка прямо пишет **«не сертифицировано Apple»**; «HomeKit» употребляется
  описательно, без логотипов Apple.

Остаточный риск: если продукт с мостом начнут продавать или предустанавливать,
позицию нужно пересмотреть до этого (MFi или отказ от HAP).

## G9 — проба спроса

**Статус: ОТКРЫТО.** Действие Оператора, вне кода: опросить отдел продаж и
двух интеграторов, нужны ли HomeKit и Home Connect и кому. По решению Оператора
(Q-G) Phase 2 строится без ожидания этого ответа; ответ записать сюда, когда он
будет.
