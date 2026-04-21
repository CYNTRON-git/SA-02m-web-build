#!/usr/bin/env bash
# Сборка пакета для сайта: копирует *.fw и пишет index.json в каталог скрипта.
set -euo pipefail
WEB_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$WEB_ROOT/opt/sa02m-flasher/scripts/prepare_firmware_for_site.py"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCAN="${1:-$HOME/Downloads/MR-02m/build/AppBoot}"
if [[ ! -d "$SCAN" ]]; then
  echo "Каталог не найден: $SCAN" >&2
  echo "Использование: $0 [/path/to/MR-02m/build/AppBoot]" >&2
  exit 1
fi
python3 "$SCRIPT" --scan "$SCAN" --bundle-dir "$OUT_DIR"
echo "Готово. Загрузите на сайт: $OUT_DIR"
