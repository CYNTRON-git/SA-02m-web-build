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

## Плагин `mplc_cyntron.so` — отдельно, в git

Дроп `MPLC4/cyntron/` **не содержит** `.so`. Плагин ЦИНТРОН
`firmware/mplc4/mplc_cyntron.so` (483 KB) **отслеживается в git** отдельно и
подставляется в `/opt/vendor-installers/mplc4/` при сборке образа.

## Staging при сборке образа

`tools/debian-rootfs/create-sa02m-rootfs.sh` копирует `MPLC4/cyntron/.` →
`/opt/vendor-installers/mplc4/` и добавляет туда же
`firmware/mplc4/mplc_cyntron.so`. На устройстве `scripts/09-mplc.sh`
запускает vendor `install.sh`. Fallback-кандидат для сборки —
`$REPO/MPLC4/cyntron` (был `$REPO/vendor/mplc4`).
