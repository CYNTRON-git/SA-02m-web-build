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
