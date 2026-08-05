#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# sa02m-watchdog-feed.sh  —  Kernel hardware watchdog feeder
#
# Зачем: /dev/watchdog требует периодической записи иначе делает reset.
# Старая реализация ("printf 1 > /dev/watchdog в цикле") открывала
# и закрывала устройство при каждой записи, из-за чего ядро каждый раз
# писало в журнал: "watchdog: watchdog0: watchdog did not stop!".
# Этот спам забивал journald.
#
# Здесь fd 3 открыт один раз и держится до SIGTERM; при остановке мы
# пишем 'V' (Magic Close) — драйвер корректно отключает watchdog без
# предупреждения и без выполнения reset.
# ═══════════════════════════════════════════════════════════════════════════
set -u

DEV="${WATCHDOG_DEV:-/dev/watchdog}"
INTERVAL="${WATCHDOG_FEED_INTERVAL:-10}"

if [ ! -c "$DEV" ]; then
    echo "watchdog-feed: ${DEV} отсутствует, выход" >&2
    exit 0
fi

# Открываем устройство один раз. Дальше работаем с fd 3.
# If the device is busy (e.g. systemd already holds it via RuntimeWatchdogSec),
# exit cleanly — systemd handles the hardware watchdog on its own.
if ! exec 3>"$DEV" 2>/dev/null; then
    echo "watchdog-feed: ${DEV} занят или недоступен; systemd вероятно управляет WDT напрямую (RuntimeWatchdogSec)" >&2
    exit 0
fi

# При остановке: 'V' = Magic Close. Драйвер закроет устройство без reset
# и без сообщения "watchdog did not stop!".
graceful_stop() {
    printf 'V' >&3 2>/dev/null || true
    exec 3>&- 2>/dev/null || true
    exit 0
}
trap graceful_stop TERM INT QUIT HUP

# Таймаут watchdog. Аппаратный ПРЕДЕЛ sun4i-wdt на A40i — 16с (max_timeout),
# и запрос выше предела драйвер отвергает целиком: значение не применяется
# вовсе, а ошибка тут глушится `|| true`. Поэтому просим не фиксированные 30с,
# а min(30, max_timeout) — исходный смысл (таймаут заведомо больше INTERVAL на
# платформе с маленьким дефолтом) сохранён, предел соблюдён. Если max_timeout
# недоступен — не трогаем таймаут и оставляем дефолт драйвера: это безопаснее,
# чем просить число, которое драйвер может отклонить.
WDT_MAX=$(cat /sys/class/watchdog/watchdog0/max_timeout 2>/dev/null || true)
case "$WDT_MAX" in
    ''|*[!0-9]*|0) WDT_MAX='' ;;
esac
if [ -n "$WDT_MAX" ] && [ -w /sys/class/watchdog/watchdog0/timeout ]; then
    WDT_WANT=30
    [ "$WDT_MAX" -lt "$WDT_WANT" ] && WDT_WANT="$WDT_MAX"
    echo "$WDT_WANT" > /sys/class/watchdog/watchdog0/timeout 2>/dev/null || true
fi

# Любая запись (один байт) сбрасывает watchdog. Используем '.' просто
# как маркер — содержимое не важно.
while :; do
    printf '.' >&3 2>/dev/null || break
    sleep "$INTERVAL"
done

graceful_stop
