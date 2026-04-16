#!/bin/bash

### Глобальные параметры ###
LOG_FILE="/var/log/sa02m_install.log"


### Функции ###

log() {
    local level=$1
    local message=$2
    local timestamp=$(date "+%Y-%m-%d %H:%M:%S")
    echo -e "[${timestamp}] [${level}] ${message}" | tee -a "$LOG_FILE"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log "ERROR" "Требуются права root! Запустите скрипт с sudo."
        exit 1
    fi
}

error_exit() {
    log "ERROR" "Критическая ошибка в строке $1: $2"
    exit 1
}

log "INFO" "Настройка прав для управления временем"
echo "www-data ALL=(ALL) NOPASSWD: /bin/date, /sbin/hwclock" > /etc/sudoers.d/time-sync
chmod 440 /etc/sudoers.d/time-sync
### Основной процесс ###
check_root
trap 'error_exit $LINENO "Прерывание выполнения"' SIGINT

log "INFO" "=== Начало установки ==="
# Синхронизация времени
log "INFO" "Выполнение синхронизации времени..."
{
    # Установка chrony при необходимости
    if ! command -v chronyc >/dev/null; then
        apt-get install -y chrony
    fi

    # Конфигурация NTP-серверов
    CHRONY_CONF="/etc/chrony/chrony.conf"
    RUS_SERVERS=(
        "ntp1.vniiftri.ru"
        "ntp2.vniiftri.ru"
        "ntp3.vniiftri.ru"
        "ntp1.ntp-servers.ru"
        "ntp2.ntp-servers.ru"
        "ntp.zebra.ru"
    )

    # Резервное копирование конфига
    TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
    cp "$CHRONY_CONF" "${CHRONY_CONF}.bak-$TIMESTAMP"

    # Добавление российских серверов
    log "INFO" "Обновление списка NTP-серверов..."
    {
        sed -i '/^pool/d;/^server\s.*\.pool\.ntp\.org/d' "$CHRONY_CONF"
        for server in "${RUS_SERVERS[@]}"; do
            echo "server $server iburst"
        done >> "$CHRONY_CONF"
    } >> "$LOG_FILE" 2>&1

    # Активация службы chrony
    if systemctl is-active chrony >/dev/null; then
        log "INFO" "Служба chrony уже активна"
    else
        systemctl unmask chrony.service
        systemctl enable --now chrony.service
        systemctl restart chrony.service
        log "INFO" "Служба chrony успешно активирована"
    fi

    # Настройка часового пояса
    CURRENT_TZ=$(timedatectl show --property=Timezone --value)
    [ "$CURRENT_TZ" = "Europe/Moscow" ] || {
        timedatectl set-timezone Europe/Moscow
        log "INFO" "Установлен часовой пояс Europe/Moscow"
    }

  # Обновление конфига chrony
    CHRONY_CONF="/etc/chrony/chrony.conf"
    sed -i 's/^#.*rtcsync/rtcsync/' $CHRONY_CONF
    echo "makestep 1 3" >> $CHRONY_CONF
	
    # Настройка аппаратных часов
    if timedatectl | grep -q "RTC in local TZ: yes"; then
        timedatectl set-local-rtc 0
        log "INFO" "RTC переведен на UTC"
    fi

    # Принудительная синхронизация
    log "INFO" "Запуск синхронизации времени..."
    chronyc -a makestep >/dev/null 2>&1
    systemctl restart chrony.service
    sleep 2

    # Проверка синхронизации
    if chronyc waitsync 30 0.01 >/dev/null 2>&1; then
        hwclock -wu >/dev/null 2>&1
        log "INFO" "Синхронизация успешна. Системное время: $(date '+%F %T %Z')"
    else
        error_exit $LINENO "Ошибка синхронизации с NTP-серверами"
    fi

    # Финализацияз
    timedatectl set-ntp true
    log "INFO" "Фоновая синхронизация активирована"

} || error_exit $LINENO "Ошибка настройки времени"


log "INFO" "Проверка временных настроек:"
{
    echo "=== Timezone ==="
    timedatectl | grep "Time zone"
    echo "=== System time ==="
    date
    echo "=== RTC time ==="
    hwclock --show
} | tee -a "$LOG_FILE"
log "OK" "Установка успешно завершена!"
exit 0