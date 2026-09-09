# Офлайн-пакет обновления SA-02m (`.sa02m` v1)

Единственный дом формата контейнера и процедуры упаковки на release-машине.
На устройстве runtime-allowlist — только `manifest.deploy[]` (файл
`scripts/offline-update-deploy-map.json` на плате **не читается**).

Machine-facing имена (footer, ключи JSON, команды) — на английском; пояснения —
по `docLanguage` (ru).

---

## Что это

Подписанный файл полной поставки веб-интерфейса и сопутствующих overlay-путей
(`www/`, `opt/sa02m-*`, helpers в `/usr/local`, юниты `sa02m-*`, ключи updater).

**Не** является:

- прошивкой модулей MR-02m / DTV / CE;
- образом eMMC / self-flash rootfs;
- запуском `install.sh` / `apt-get` на устройстве.

---

## Байтовый layout

```
[0 .. tar_size)                POSIX ustar (outer), tar_size % 512 == 0
[tar_size .. tar_size+21)      FOOTER = b"SA02M_UPDATE_END_V1" (19) + b"\0\0" (2) = 21
file_size = tar_size + 21      # file_size % 512 == 21 — это нормально
```

(В черновике плана фигурировало «+20» — это опечатка длины magic; на проводе
всегда `len(FOOTER) == 21`.)

Outer members (по **имени**, порядок в tar не важен для verify):

| Name | Content |
|---|---|
| `manifest.json` | schema v1 (UTF-8 JSON) |
| `manifest.sig` | base64(Ed25519 sig of domain-separated canonical JSON) + `\\n` |
| `payload.tar.gz` | gzip(ustar overlay) |
| `payload.sha256` | `{hex64}  payload.tar.gz\\n` |

Sidecar на ПК: `SA-02m-update-<version>.sa02m.sha256` →
`{hex64}  SA-02m-update-<version>.sa02m\\n`.

---

## Подпись (domain-separated Ed25519)

```
SIG_MESSAGE = b"SA02M-MANIFEST-V1\0" + canonical_utf8_json
canonical   = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

Ключ: `signing_key_id` (по умолчанию `release-2026-08`).

- Private (только build-host): `private/sa02m-update-keys/<id>.ed25519`
  или env `SA02M_UPDATE_SIGNING_KEY`.
- Public (в git / на устройстве): `etc/sa02m-update/trusted-keys/<id>.pem`.

---

## Упаковка (как собрать пакет)

Предусловия:

1. Чистая git-ветка версии `M.M.P.S` (имя ветки = версия).
2. Версии согласованы: `py -3 scripts/sync-app-version.py --check`.
3. Есть ключ подписи (один раз на key-id):

```powershell
# Windows: openssl из Git for Windows должен быть в PATH
$env:PATH = "C:\Program Files\Git\usr\bin;" + $env:PATH
py -3 scripts/gen-update-signing-key.py --key-id release-2026-08
git add etc/sa02m-update/trusted-keys/release-2026-08.pem
git commit -m "chore: add offline-update trusted public key"
```

Сборка:

```powershell
py -3 scripts/sync-app-version.py --check
git status --porcelain   # must be empty
py -3 scripts/pack-offline-update.py
# → out/SA-02m-update-<version>.sa02m
# → out/SA-02m-update-<version>.sa02m.sha256
```

Опции:

| Flag / env | Meaning |
|---|---|
| `--out-dir DIR` | Output directory (default `out/`) |
| `--key-id ID` | Manifest `signing_key_id` (default `release-2026-08`) |
| `--signing-key PATH` | Private key PEM |
| `SA02M_UPDATE_SIGNING_KEY` | Same as `--signing-key` |
| `--skip-validate` | Skip import of `opt/sa02m-update/lib/validate_package.py` |

Жёсткие отказы packer:

| Condition | Exit |
|---|---|
| `sync-app-version.py --check` fails | 1 |
| Dirty git worktree | **2** |
| Missing private key | 1 (печатает команду gen-update-signing-key) |

Allowlist путей overlay: `scripts/offline-update-allowlist.txt`.
Таблица src→dst (CI/review + packer): `scripts/offline-update-deploy-map.json`.

---

## Manifest v1 (кратко)

Обязательные поля: `schema_version=1`, `product=SA-02m`, `model=A40i`,
`arch=armv7l`, `version`, `repo_commit` (40 hex), `built_at`, `signing_key_id`,
`min_updater`, `min_version`, `payload{size,sha256,uncompressed_size_max}`,
`preflight`, `deploy[]`, `services`, `delete[]`, `migrations[]`.

`deploy[].dst` обязан совпадать с runtime regex (§2.2 плана). `preserve[]` в
manifest **нет** — константа runner `PRESERVE_PATHS`.

`services` — действия над службами после деплоя (исполнитель —
`restart_services_and_health` в `etc/sa02m-update-runner.sh`; тот же набор
ключей пишет онлайн-генератор манифеста в том же файле):

| Key | Обязательный | Семантика |
|---|---|---|
| `daemon_reload` | да | `systemctl daemon-reload` перед рестартами |
| `stop_before_apply[]` | да | остановить ДО деплоя (`sa02m-flasher`) |
| `restart[]` | да | безусловный bounded `restart \|\| start` — core-службы (nginx/fcgiwrap, `sa02m-devices-*`, `mplc4`, `sa02m-telemetry`, `sa02m-rules`) |
| `health{http_url,units_active,version_file}` | да | гейт после рестартов; провал ⇒ rollback |
| `enable[]` | нет | включить автозапуск (пережить перезагрузку), без start |
| `restart_if_active[]` | нет | рестарт **только активного** юнита, остановленный никогда не стартуется (never-widen; opt-in Alice-семейство: `sa02m-alice-client`, `sa02m-alice-config`, `sa02m-cloud-control` — держат в памяти код `/opt/sa02m-rules`/`sa02m_alice`) |
| `restart_if_changed{unit: prefix}` | нет | то же + только когда журнал apply записал изменённый `dst` под `prefix` (`sa02m-modbus-mqtt` → `/opt/sa02m-modbus-mqtt/`: port-lease RS-485 — не бить активный мост, когда пакет моста не менялся; журнал отсутствует ⇒ считается изменённым) |

Совместимость ключей: неизвестный ключ старый валидатор отклоняет
(`additionalProperties: false` — осознанная позиция forward-compat).
Пакер **не пишет** `enable` / `restart_if_*`, пока `MIN_UPDATER` < 1.0.6.37
(замороженный v1: только обязательные ключи) — иначе валидаторы релизов
**1.0.5.69–1.0.6.36** отвергают пакет с `E_MANIFEST` (runner < 1.0.5.69
валидатора не имеет и проверку пропускает; `opt/sa02m-update/` и runner
родились в одном коммите `b9f3ad4` = 1.0.5.69 — окно названо в комментарии
`validate_package.py`, это его единственный дом). `sa02m-rules` остаётся в
обязательном `restart[]` (старый runner его исполняет). Условные рестарты
Alice/моста и `enable[]` пакер начнёт писать после поднятия `MIN_UPDATER` до
валидатора, который эти ключи принимает — и пакер печатает об этом одну строку
в stderr при каждой сборке (`note: services block is frozen v1 …`, 1.0.6.39).
Пока это так, оффлайн-пакет **не включает ни одного юнита**: `sa02m-dns-ensure.service`
на обновлённой оффлайн плате включает одноразовый bootstrap-шим в
`sa02m-eth-coldboot.sh` (`docs/contracts/boot-network-dns.md`).
Поднять `MIN_UPDATER` стало возможно с 1.0.6.39: runner сообщает
`UPDATER_VERSION` из развёрнутого файла `VERSION`, а не штампом `1.0.5.66`
(до этого любое поднятие порога отвергало бы пакет на **каждой** плате с
`E_COMPAT`). Поднимать — только когда весь парк, который нужно обновлять,
уже на ≥ 1.0.6.39.
Онлайн-генератор в `etc/sa02m-update-runner.sh` по-прежнему пишет полный
набор: манифест собирается уже установленным runner'ом на плате.
Ключ `enable` генераторы пишут с 1.0.5.69, а валидатор научился принимать его
только в 1.0.6.37 — оффлайн-пакеты релизов 1.0.5.69–1.0.6.36 (до заморозки
пакера) отклонялись на плате с `E_MANIFEST`.

Gates на устройстве до backup: версия payload `VERSION` == `manifest.version`;
semver installed ≥ `min_version`, runner ≥ `min_updater`, target > installed;
подпись + три совпадения hash/size payload.

---

## Связанные пути

| Path | Role |
|---|---|
| `scripts/pack-offline-update.py` | Packer |
| `scripts/offline-update-allowlist.txt` | git-archive allowlist |
| `scripts/offline-update-deploy-map.json` | src→dst (not on device) |
| `scripts/gen-update-signing-key.py` | Keygen |
| `opt/sa02m-update/lib/validate_package.py` | Device/host validator |
| `etc/sa02m-update/trusted-keys/*.pem` | Trusted public keys |
