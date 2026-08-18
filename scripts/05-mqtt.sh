#!/bin/bash
set -o pipefail  # catch masked failures in pipes (Y7); set -u deferred pending on-device install test
# ═══════════════════════════════════════════════════════════════════════════
# 05-mqtt.sh  •  Установка MQTT-инфраструктуры на СА-02м
#   - Mosquitto broker (apt)
#   - Python-зависимости (paho-mqtt, pyyaml)
#   - Modbus→MQTT мост (sa02m-modbus-mqtt.service)
#   - Системная телеметрия (sa02m-telemetry.service)
#   - Конфиг nginx (SSE для mqtt_monitor.cgi)
#   - sudoers для www-data
# ═══════════════════════════════════════════════════════════════════════════
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [05] Установка MQTT инфраструктуры ==="

BASE_DIR="$SCRIPT_DIR/.."
ETC_DIR="$BASE_DIR/etc"
OPT_DIR="$BASE_DIR/opt/sa02m-modbus-mqtt"
WWW_DIR="$BASE_DIR/www/network_config"

# ── 1. Mosquitto ──────────────────────────────────────────────────────────────
log INFO "Установка Mosquitto..."
pkg_install mosquitto mosquitto-clients

# Конфиг listeners
MOSQ_CONF_DIR="/etc/mosquitto/conf.d"
mkdir -p "$MOSQ_CONF_DIR"
install -m 0644 -o root -g root "$ETC_DIR/mosquitto/10listeners.conf" \
    "$MOSQ_CONF_DIR/10listeners.conf"
log OK "Конфиг Mosquitto установлен: $MOSQ_CONF_DIR/10listeners.conf"

# Создать ACL и passwd директории если нет
mkdir -p /etc/mosquitto/passwd /etc/mosquitto/acl

# ACL шаблон
if [ ! -f /etc/mosquitto/acl/default.conf ]; then
    install -m 0640 -o root -g mosquitto \
        "$ETC_DIR/mosquitto/acl_default.conf" /etc/mosquitto/acl/default.conf
    log OK "ACL установлен"
fi

# Создать пользователя mqttuser если нет файла паролей
if [ ! -f /etc/mosquitto/passwd/default.conf ]; then
    log INFO "Создаю пользователя mqttuser для внешнего MQTT-доступа..."
    # Генерируем случайный пароль
    MQTT_PASS=$(tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 16 || echo "cyntron_mqtt_$(date +%s)")
    mosquitto_passwd -b -c /etc/mosquitto/passwd/default.conf mqttuser "$MQTT_PASS"
    chown root:mosquitto /etc/mosquitto/passwd/default.conf
    chmod 0640 /etc/mosquitto/passwd/default.conf
    log OK "Пользователь mqttuser создан. Пароль: $MQTT_PASS"
    log WARN "Сохраните пароль! Он больше не отображается."
    echo "MQTT_USER=mqttuser" > /etc/sa02m_mqtt.env
    echo "MQTT_PASS=$MQTT_PASS" >> /etc/sa02m_mqtt.env
    chmod 0600 /etc/sa02m_mqtt.env
fi

sa02m_systemctl enable mosquitto >> "$LOG_FILE" 2>&1 || true
sa02m_systemctl restart mosquitto >> "$LOG_FILE" 2>&1 && log OK "Mosquitto запущен" \
    || log WARN "Mosquitto не стартовал — проверьте: journalctl -u mosquitto -n 50"

# Проверка порта 1883
sleep 1
if ss -H -ltn "sport = :1883" 2>/dev/null | grep -q ':1883'; then
    log OK "Mosquitto слушает порт 1883 (localhost)"
else
    log WARN "Порт 1883 не обнаружен — Mosquitto, возможно, не запустился"
fi

# ── 2. Python-зависимости ─────────────────────────────────────────────────────
log INFO "Установка Python-зависимостей..."
for pkg in python3 python3-pip; do
    dpkg -l "$pkg" >/dev/null 2>&1 || apt-get install -y "$pkg" >> "$LOG_FILE" 2>&1
done

# 2.1. Приоритет: apt-пакеты (стабильнее pip на embedded)
#       python3-paho-mqtt, python3-yaml, python3-serial есть в bullseye main.
_MQTT_APT_MISSING=""
for _pkg in python3-paho-mqtt python3-yaml python3-serial; do
    if ! dpkg -l "$_pkg" 2>/dev/null | grep -q "^ii  $_pkg"; then
        _MQTT_APT_MISSING="$_MQTT_APT_MISSING $_pkg"
    fi
done
if [ -n "$_MQTT_APT_MISSING" ]; then
    log INFO "apt install:$_MQTT_APT_MISSING"
    if ! apt-get install -y --no-install-recommends $_MQTT_APT_MISSING >> "$LOG_FILE" 2>&1; then
        log WARN "apt install не удался — fallback: apt-get download + dpkg -i"
        _TMPDIR="$(mktemp -d)"
        (cd "$_TMPDIR" && apt-get download $_MQTT_APT_MISSING >> "$LOG_FILE" 2>&1             && dpkg -i "$_TMPDIR"/*.deb >> "$LOG_FILE" 2>&1) || true
        rm -rf "$_TMPDIR"
    fi
fi

# 2.2. Fallback через pip, если apt-версии всё ещё нет
if ! python3 -c "import paho.mqtt" >/dev/null 2>&1; then
    log WARN "python3-paho-mqtt из apt недоступен — fallback pip3"
    pip3 install --break-system-packages --quiet paho-mqtt 2>&1 | tee -a "$LOG_FILE" | tail -3
fi
if ! python3 -c "import yaml" >/dev/null 2>&1; then
    pip3 install --break-system-packages --quiet pyyaml 2>&1 | tee -a "$LOG_FILE" | tail -3
fi
if ! python3 -c "import serial" >/dev/null 2>&1; then
    pip3 install --break-system-packages --quiet pyserial 2>&1 | tee -a "$LOG_FILE" | tail -3
fi

# 2.3. Обязательная проверка импорта (fail-loud, если не установилось)
_MQTT_MISSING_MODULES=""
python3 -c "import paho.mqtt" 2>/dev/null || _MQTT_MISSING_MODULES="$_MQTT_MISSING_MODULES paho.mqtt"
python3 -c "import yaml"      2>/dev/null || _MQTT_MISSING_MODULES="$_MQTT_MISSING_MODULES yaml"
python3 -c "import serial"    2>/dev/null || _MQTT_MISSING_MODULES="$_MQTT_MISSING_MODULES serial"
if [ -n "$_MQTT_MISSING_MODULES" ]; then
    log ERR "Python-зависимости НЕ установлены:$_MQTT_MISSING_MODULES"
    log ERR "sa02m-modbus-mqtt/sa02m-telemetry уйдут в restart-loop!"
    exit 1
fi
log OK "Python-зависимости установлены и импортируются (paho.mqtt, yaml, serial)"

# ── 3. Modbus→MQTT мост ───────────────────────────────────────────────────────
log INFO "Деплой Modbus→MQTT моста..."

# Capture the bridge's prior state BEFORE copying fresh code. A RUNNING bridge
# must be restarted after the new .py land, or it keeps executing STALE code
# until a manual restart/reboot (the live-device upgrade defect this fixes). We
# never disable an enabled bridge and never start one the operator had stopped.
read -r _BRIDGE_PREV_EN _BRIDGE_PREV_ACT < <(sa02m_capture_svc_state sa02m-modbus-mqtt.service)

BRIDGE_DIR="/opt/sa02m-modbus-mqtt"
install -d -m 0755 -o root -g root "$BRIDGE_DIR"

# Копируем Python-скрипты
# Bridge modules FIRST, the entry modbus_mqtt_bridge.py LAST: the old entry is
# self-contained, so a device that crashes/restarts mid-copy still boots the
# previous bridge until the final file lands — only the last copy switches the
# composition. Keep this ordered list in sync with tests/test_entry_surface.py
# EXPECTED_MODULES and scripts/update-www-only.sh.
for f in bridge_serial.py bridge_fmb.py bridge_meta.py bridge_mqtt.py bridge_mr02m_map.py \
         bridge_device.py bridge_mr02m.py bridge_dtv_ce.py bridge_template.py; do
    install -m 0755 -o root -g root "$OPT_DIR/$f" "$BRIDGE_DIR/$f"
done
install -m 0755 -o root -g root "$OPT_DIR/modbus_mqtt_bridge.py" "$BRIDGE_DIR/modbus_mqtt_bridge.py"

# Device-template drop-in dir (type: template devices). Ship the self-authored
# example + README; the integrator drops their own WB-format JSON here.
install -d -m 0755 -o root -g root "$BRIDGE_DIR/templates"
for f in "$OPT_DIR/templates/"*.json "$OPT_DIR/templates/README.md"; do
    [ -f "$f" ] && install -m 0644 -o root -g root "$f" "$BRIDGE_DIR/templates/$(basename "$f")"
done
install -m 0755 -o root -g root "$OPT_DIR/sa02m_telemetry.py"    "$BRIDGE_DIR/sa02m_telemetry.py"
install -m 0755 -o root -g root "$OPT_DIR/mqtt_bus_scan.py"     "$BRIDGE_DIR/mqtt_bus_scan.py"
install -m 0755 -o root -g root "$OPT_DIR/mqtt_live_snapshot.py" "$BRIDGE_DIR/mqtt_live_snapshot.py"
install -m 0755 -o root -g root "$OPT_DIR/mqtt_monitor_stream.py" "$BRIDGE_DIR/mqtt_monitor_stream.py"

# Конфиг YAML (только если не существует — не перетираем пользовательские настройки)
if [ ! -f /etc/sa02m-modbus-mqtt.yaml ]; then
    install -m 0660 -o root -g www-data "$OPT_DIR/sa02m-modbus-mqtt.yaml" /etc/sa02m-modbus-mqtt.yaml
    log OK "Конфиг /etc/sa02m-modbus-mqtt.yaml создан (шаблон)"
else
    chown root:www-data /etc/sa02m-modbus-mqtt.yaml 2>/dev/null || true
    chmod 0660 /etc/sa02m-modbus-mqtt.yaml 2>/dev/null || true
    log INFO "/etc/sa02m-modbus-mqtt.yaml уже существует — права обновлены для www-data"
fi

install -m 0755 -o root -g root "$ETC_DIR/sa02m-mqtt-config-apply.sh" /usr/local/sbin/sa02m-mqtt-config-apply.sh
sed -i 's/\r$//' /usr/local/sbin/sa02m-mqtt-config-apply.sh
install -m 0755 -o root -g root "$ETC_DIR/sa02m-mqtt-external-info.py" /usr/local/sbin/sa02m-mqtt-external-info.py
sed -i 's/\r$//' /usr/local/sbin/sa02m-mqtt-external-info.py

# Systemd units
install -m 0644 -o root -g root "$ETC_DIR/sa02m-modbus-mqtt.service" \
    /etc/systemd/system/sa02m-modbus-mqtt.service
install -m 0644 -o root -g root "$ETC_DIR/sa02m-telemetry.service" \
    /etc/systemd/system/sa02m-telemetry.service

sa02m_systemctl daemon-reload
sa02m_systemctl enable sa02m-modbus-mqtt.service >> "$LOG_FILE" 2>&1 || true
sa02m_systemctl enable sa02m-telemetry.service   >> "$LOG_FILE" 2>&1 || true

# First install: leave the bridge enabled-but-stopped (user configures devices
# first). Upgrade: restore the prior RUNNING state on fresh code so a running
# bridge never keeps executing stale .py, and a bridge the operator was running
# is never left stopped (the hard rule of this fix).
if [ "$_BRIDGE_PREV_ACT" = active ]; then
    sa02m_restore_svc_state sa02m-modbus-mqtt.service "$_BRIDGE_PREV_EN" "$_BRIDGE_PREV_ACT" refresh
    log OK "sa02m-modbus-mqtt перезапущен на свежем коде (был активен до установки)"
else
    log INFO "sa02m-modbus-mqtt.service включён (не запущен — настройте устройства через веб-интерфейс)"
fi

# Телеметрию запускаем сразу
sa02m_systemctl restart sa02m-telemetry.service >> "$LOG_FILE" 2>&1 && \
    log OK "sa02m-telemetry запущен" || log WARN "sa02m-telemetry не стартовал"

# ── 4. CGI-скрипты веб-интерфейса ────────────────────────────────────────────
log INFO "Установка CGI MQTT..."
CGI_SRC="$WWW_DIR/cgi-bin"
: "${WEB_ROOT:=/var/www/network_config}"
CGI_DST="$WEB_ROOT/cgi-bin"

if [ -d "$CGI_DST" ]; then
    for cgi in mqtt_config.cgi mqtt_status.cgi mqtt_monitor.cgi mqtt_ctrl.cgi mqtt_scan.cgi mqtt_live.cgi; do
        if [ -f "$CGI_SRC/$cgi" ]; then
            install -m 0755 -o root -g www-data "$CGI_SRC/$cgi" "$CGI_DST/$cgi"
            sed -i 's/\r$//' "$CGI_DST/$cgi"
            log OK "Установлен $cgi"
        else
            log WARN "CGI не найден в репозитории: $CGI_SRC/$cgi"
        fi
    done
else
    log WARN "CGI директория $CGI_DST не найдена — пропускаем"
fi

# ── 5. Sudoers для www-data ───────────────────────────────────────────────────
log INFO "Устанавливаю sudoers sa02m-mqtt..."
sa02m_install_sudoers "$ETC_DIR/sudoers.d/sa02m-mqtt" /etc/sudoers.d/sa02m-mqtt

# ── 6. nginx: добавить location для SSE mqtt_monitor.cgi ─────────────────────
NGINX_CONF_SRC="$ETC_DIR/nginx/network_config.conf"
NGINX_CONF_DST=""
for f in /etc/nginx/sites-enabled/sa02m \
          /etc/nginx/sites-enabled/000-sa02m-network_config \
          /etc/nginx/conf.d/sa02m.conf \
          /etc/nginx/sites-available/sa02m \
          /etc/nginx/sites-available/network_config; do
    [ -f "$f" ] && NGINX_CONF_DST="$f" && break
done

if [ -n "$NGINX_CONF_DST" ]; then
    # Добавить location для mqtt_monitor.cgi если его ещё нет
    if ! grep -q "mqtt_monitor.cgi" "$NGINX_CONF_DST" 2>/dev/null; then
        # Вставить перед location /cgi-bin/
        SNIPPET='    location = /cgi-bin/mqtt_monitor.cgi {\n        include        fastcgi_params;\n        fastcgi_param  SCRIPT_FILENAME $document_root$fastcgi_script_name;\n        fastcgi_param  HTTP_COOKIE     $http_cookie;\n        fastcgi_connect_timeout 5s;\n        fastcgi_send_timeout    300s;\n        fastcgi_read_timeout    300s;\n        fastcgi_buffering      off;\n        fastcgi_pass   unix:/run/fcgiwrap/fcgiwrap.socket;\n    }'
        sed -i "s|# include первым:|${SNIPPET}\n\n    # include первым:|" "$NGINX_CONF_DST" 2>/dev/null \
            || log WARN "Не удалось автоматически обновить nginx.conf — добавьте mqtt_monitor.cgi location вручную"
        nginx -t >> "$LOG_FILE" 2>&1 && sa02m_systemctl reload nginx && log OK "nginx перезагружен" \
            || log WARN "nginx reload не удался"
    else
        log INFO "nginx уже содержит location mqtt_monitor.cgi"
    fi
else
    log WARN "Конфиг nginx не найден — SSE для MQTT monitor не настроен"
fi

# ── 7. Финальная проверка ─────────────────────────────────────────────────────
log INFO "--- Итог ---"
for svc in mosquitto sa02m-telemetry; do
    if pgrep -x "$svc" >/dev/null 2>&1 || (command -v systemctl && systemctl is-active "$svc" >/dev/null 2>&1); then
        log OK "$svc: запущен"
    else
        log WARN "$svc: не запущен"
    fi
done

if ss -H -ltn "sport = :1883" 2>/dev/null | grep -q ':1883'; then
    log OK "MQTT broker доступен на localhost:1883"
fi
if ss -H -ltn "sport = :1884" 2>/dev/null | grep -q ':1884'; then
    log OK "MQTT broker доступен на :1884 (внешний, с паролем)"
fi

log OK "=== [05] MQTT инфраструктура установлена ==="
log INFO "Следующий шаг: откройте веб-интерфейс → вкладка MQTT → Сканировать устройства"
