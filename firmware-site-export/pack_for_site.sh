#!/usr/bin/env bash
# Сборка пакета для сайта: копирует *.fw и пишет index.json в каталог скрипта.
# Каталог сборки: аргумент, или FW_PACK_SCAN_DIR, или defaultPackScanDir в site-deploy.config.json
set -euo pipefail
WEB_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$WEB_ROOT/opt/sa02m-flasher/scripts/prepare_firmware_for_site.py"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$OUT_DIR/site-deploy.config.json"

cfg_get() {
  local key="$1"
  python3 -c "import json,sys; p,k=sys.argv[1],sys.argv[2]; d=json.load(open(p,encoding='utf-8')); v=d.get(k); sys.stdout.write('' if v is None else str(v))" "$CFG" "$key" 2>/dev/null || true
}

SCAN="${1:-}"
if [[ -z "$SCAN" ]]; then
  SCAN="${FW_PACK_SCAN_DIR:-}"
fi
if [[ -z "$SCAN" && -f "$CFG" ]]; then
  SCAN="$(cfg_get defaultPackScanDir)"
fi
if [[ -z "$SCAN" ]]; then
  echo "Укажите каталог MR-02m/build/AppBoot аргументом, задайте FW_PACK_SCAN_DIR или defaultPackScanDir в site-deploy.config.json (см. site-deploy.config.example.json)" >&2
  exit 1
fi
if [[ ! -d "$SCAN" ]]; then
  echo "Каталог не найден: $SCAN" >&2
  exit 1
fi
python3 "$SCRIPT" --scan "$SCAN" --bundle-dir "$OUT_DIR"
echo "Готово. Загрузите на сайт: $OUT_DIR"
