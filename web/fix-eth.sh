#!/bin/bash
INTERFACE="eth0"
LOG_FILE="/var/log/fix-eth.log"
MAX_RETRIES=2
DELAY=3

log() {
    local level=$1
    local message=$2
    local timestamp=$(date "+%Y-%m-%d %H:%M:%S")
    echo -e "[${timestamp}] [${level}] ${message}" | tee -a "$LOG_FILE"
}

check_interface() {
    # Проверяем, что интерфейс имеет IP-адрес
    ip -4 addr show dev "$INTERFACE" | grep -q "inet "
}

reset_interface() {
    log "INFO" "Сброс интерфейса через ip link"
    ip link set "$INTERFACE" down
    sleep "$DELAY"
    ip link set "$INTERFACE" up
    sleep "$DELAY"
}

restart_dhcp() {
    log "INFO" "Перезапуск DHCP-клиента для $INTERFACE"
    # Определяем, какой DHCP-клиент используется
    if systemctl is-active --quiet dhcpcd@$INTERFACE; then
        systemctl restart dhcpcd@$INTERFACE
    elif systemctl is-active --quiet dhclient@$INTERFACE; then
        systemctl restart dhclient@$INTERFACE
    elif command -v dhclient >/dev/null; then
        dhclient -v "$INTERFACE" >> "$LOG_FILE" 2>&1
    else
        log "WARN" "Не найден DHCP-клиент, пробуем ifup"
        ifdown "$INTERFACE" && ifup "$INTERFACE"
    fi
}

main() {
    log "INFO" "Запуск процедуры восстановления (udev)"

    # Проверка физического подключения
    if ! ethtool "$INTERFACE" 2>/dev/null | grep -q "Link detected: yes"; then
        log "WARN" "Кабель не подключен, выход"
        return 1
    fi
    log "INFO" "Кабель подключен"

    # Цикл попыток восстановления
    for ((i=1; i<=MAX_RETRIES; i++)); do
        reset_interface
        restart_dhcp

        if check_interface; then
            log "INFO" "Интерфейс успешно восстановлен (IP получен)"
            return 0
        else
            log "WARN" "Попытка $i из $MAX_RETRIES не удалась"
            sleep "$DELAY"
        fi
    done

    log "ERROR" "Все попытки восстановления исчерпаны"
    return 1
}

main