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
if ! exec 3>"$DEV"; then
    echo "watchdog-feed: не удалось открыть ${DEV}" >&2
    exit 1
fi

# При остановке: 'V' = Magic Close. Драйвер закроет устройство без reset
# и без сообщения "watchdog did not stop!".
graceful_stop() {
    printf 'V' >&3 2>/dev/null || true
    exec 3>&- 2>/dev/null || true
    exit 0
}
trap graceful_stop TERM INT QUIT HUP

# Установим таймаут watchdog побольше, если драйвер поддерживает
# (некоторые драйверы Allwinner имеют дефолт 16с — нам хватит, но
# на случай других платформ задаём 30с).
if [ -w /sys/class/watchdog/watchdog0/timeout ]; then
    echo 30 > /sys/class/watchdog/watchdog0/timeout 2>/dev/null || true
fi

# Любая запись (один байт) сбрасывает watchdog. Используем '.' просто
# как маркер — содержимое не важно.
while :; do
    printf '.' >&3 2>/dev/null || break
    sleep "$INTERVAL"
done

graceful_stop
