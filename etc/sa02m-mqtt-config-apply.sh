#!/bin/bash
# Установка YAML-конфига MQTT-моста из фиксированного staging-файла (только root).
# Путь фиксирован (не аргумент), чтобы sudoers не давал www-data копировать
# произвольный root-файл в 0660 root:www-data yaml (эскалация чтения). www-data
# пишет только своё содержимое в SRC; symlink отвергается.
set -euo pipefail
SRC=/tmp/sa02m-mqtt.yaml.new
DST=/etc/sa02m-modbus-mqtt.yaml
if [ ! -f "$SRC" ] || [ -L "$SRC" ]; then
    echo "sa02m-mqtt-config-apply: staging $SRC отсутствует или является симлинком" >&2
    exit 1
fi
install -m 0660 -o root -g www-data "$SRC" "$DST"
rm -f "$SRC"
sync
