# MPLC4 — vendor staging source

`MPLC4/cyntron/` — **единый** источник staging дистрибутива MasterSCADA MPLC 4D
Runtime (ЦИНТРОН) для образов SA-02m. Retired: `vendor/mplc4/` (тот же дроп,
переехал сюда — история в `docs/vendor-integrations.md`).

Полное описание подготовки payload, портов, лицензирования и того, как
`install.sh`/rootfs-builder его подхватывают — **одно место**:
`docs/vendor-integrations.md` (раздел «MasterSCADA MPLC 4D Runtime»). Здесь —
только манифест каталога.

## Что лежит в `MPLC4/cyntron/` (в git НЕ коммитится)

Vendor-дроп, версия payload **`1.3.10.34421`** (`version.txt`), 5 файлов:

| Файл | Назначение |
|---|---|
| `install.sh` | штатный vendor-инсталлятор (флаги `--use-systemd --http-port --enable-log`) |
| `mplc4.tar.gz` | runtime MPLC 4D (armv7hf, ~18.6 MB) |
| `nginx.tar.gz` | nginx-фронтенд MPLC UI |
| `admin.tar.gz` | admin-панель |
| `version.txt` | строка версии payload |

Бинарники (`*.tar.gz`, `install.sh`) **не в git** — они большие и под
vendor-EULA (`.gitignore`: `/MPLC4/` + исключение `!/MPLC4/README.md`). Кладутся
локально на build-host по инструкции из `docs/vendor-integrations.md`.

## Плагины — отдельно, в git (`firmware/mplc4/`)

Дроп `MPLC4/cyntron/` **не содержит** `.so`. Оба плагина ЦИНТРОН
**отслеживаются в git** по пути `firmware/mplc4/` (правильный ABI, `libmpsc++`):

| Плагин | Размер | md5 (первые 8) |
|---|---|---|
| `mplc_cyntron.so` | 180356 B | `bf412755` |
| `mplc_protocol_fast_modbus.so` | 226276 B | `9eba65e3` |

(Прежняя запись «483 KB» относилась к старому, битому драйверу с ABI
`libstdc++` → SIGSEGV — он больше не поставляется.)

## Staging при сборке образа

`tools/debian-rootfs/create-sa02m-rootfs.sh` копирует `MPLC4/cyntron/.` →
`/opt/vendor-installers/mplc4/`. На устройстве `scripts/09-mplc.sh` запускает
vendor `install.sh`, а плагины ставит **из `firmware/mplc4/`** (авторитетный
git-источник; vendor-каталог — только fallback). Кандидат runtime выбирается
по **новизне** `version.txt` (не первый попавшийся); ничья/непарсибельная
версия → копия из репозитория (`$REPO/MPLC4/cyntron`, был `$REPO/vendor/mplc4`).
