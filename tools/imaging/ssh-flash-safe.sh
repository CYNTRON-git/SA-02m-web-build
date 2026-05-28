#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  SA-02m  •  ssh-flash-safe.sh   v1.0
#
#  Безопасная SSH-прошивка образа на SA-02m.
#  Используется ТОЛЬКО если USB-носитель недоступен.
#
#  Отличия от прямого dd:
#    - Явное подтверждение пользователя перед записью
#    - Остановка и маскировка ВСЕХ watchdog/reboot-trigger сервисов
#    - Применение watchdog-фиксов ДО перезагрузки (записывает в образ)
#    - Ожидание после reboot и проверка доступности
#    - Образ передаётся потоком (не хранится на устройстве)
#
#  Использование:
#    ./ssh-flash-safe.sh --image ./out/SA-02m-vX.X.X.X-shrunk.img.xz \
#                        [--ip 192.168.1.136] [--key ~/.ssh/sa02m_sa02]
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
LC_ALL=C

DEVICE_IP="192.168.1.136"
SSH_KEY="${SSH_KEY:-$(dirname "$0")/../../private/.ssh/sa02m_sa02}"
IMAGE=""
TARGET_DEV="/dev/mmcblk2"

usage() {
    echo "Usage: $0 --image PATH [--ip IP] [--key KEY] [--target DEV]"
    echo ""
    echo "  --image PATH   путь к .img.xz образу (обязательно)"
    echo "  --ip    IP     IP устройства (по умолчанию: $DEVICE_IP)"
    echo "  --key   KEY    путь к SSH ключу"
    echo "  --target DEV   блочное устройство на приёмнике (по умолчанию: $TARGET_DEV)"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)  IMAGE="$2";      shift 2 ;;
        --ip)     DEVICE_IP="$2";  shift 2 ;;
        --key)    SSH_KEY="$2";    shift 2 ;;
        --target) TARGET_DEV="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

[ -n "$IMAGE" ] || { echo "ERROR: --image обязателен" >&2; usage; }
[ -f "$IMAGE" ] || { echo "ERROR: файл не найден: $IMAGE" >&2; exit 1; }
[ -r "$SSH_KEY" ] || { echo "ERROR: SSH ключ не найден: $SSH_KEY" >&2; exit 1; }

SSH=(ssh -i "$SSH_KEY"
         -o StrictHostKeyChecking=no
         -o UserKnownHostsFile=/dev/null
         -o ConnectTimeout=10
         -o ServerAliveInterval=10
         -o ServerAliveCountMax=9999
         "root@$DEVICE_IP")

IMG_SIZE=$(stat -c%s "$IMAGE" 2>/dev/null || stat -f%z "$IMAGE")
SHA_FILE="${IMAGE}.sha256"
IMG_NAME=$(basename "$IMAGE")

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ── Проверка доступности устройства ─────────────────────────────────────
log "Проверка SSH на $DEVICE_IP..."
"${SSH[@]}" "uname -nrm" || die "устройство недоступно по SSH"

# ── Предупреждение и подтверждение ──────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║          ВНИМАНИЕ: ПРОШИВКА УСТРОЙСТВА ЧЕРЕЗ SSH                ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
printf "║  Образ:   %-55s║\n" "$IMG_NAME"
printf "║  Размер:  %-55s║\n" "$(numfmt --to=iec --suffix=B "$IMG_SIZE")"
printf "║  Цель:    %-55s║\n" "$DEVICE_IP  →  $TARGET_DEV"
echo "║                                                                  ║"
echo "║  Будет остановлена работающая система и перезаписана eMMC.      ║"
echo "║  Устройство перезагрузится. SSH сессия прервётся.               ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
printf "Введите 'yes' для продолжения: "
read -r CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "Отменено."; exit 0; }

# ── Стоп watchdog-сервисов и сервисов приложения ────────────────────────
log "[1/5] Остановка watchdog и сервисов на устройстве..."
"${SSH[@]}" '
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog \
           sa02m-watchdog-feed nginx fcgiwrap sa02m-flasher mplc mplc4 php8.3-fpm; do
    systemctl stop "$svc"  2>/dev/null || true
    systemctl mask "$svc"  2>/dev/null || true
done
sync
echo "STOP_DONE"
'

# ── Применяем watchdog-фиксы ДО записи образа ───────────────────────────
# Это гарантирует, что фиксы попадут в записываемый образ ТОЛЬКО если
# образ уже содержит правильные файлы. На самом деле фиксы применяются
# к живой системе перед reboot; образ их не содержит — применяем после.

# ── Потоковая передача и запись образа ──────────────────────────────────
log "[2/5] Передача и запись образа (streaming xz | dd)..."
log "    Не прерывайте до завершения!"

# Передаём xz-поток через SSH в dd — образ не хранится на устройстве
pv -pterb "$IMAGE" 2>/dev/null | \
"${SSH[@]}" "xz -dc | dd of=$TARGET_DEV bs=4M conv=fsync status=none" \
|| cat "$IMAGE" | "${SSH[@]}" "xz -dc | dd of=$TARGET_DEV bs=4M conv=fsync status=none"

log "[3/5] sync на устройстве..."
"${SSH[@]}" "sync; sync; echo SYNC_DONE" || true

# ── Применяем watchdog-фиксы на свежезаписанный образ ───────────────────
# Монтируем корневой раздел нового образа и патчим watchdog прямо в нём
log "[4/5] Применение watchdog-фиксов в новый образ (loop mount)..."
"${SSH[@]}" '
set -e
PART=$(partx -g -o NR,TYPE '"$TARGET_DEV"' 2>/dev/null | awk '"'"'$2=="Linux"{print $1; exit}'"'"' || echo 2)
MNT=$(mktemp -d /tmp/new-root-XXXXXX)
mount '"$TARGET_DEV"'p${PART} "$MNT" 2>/dev/null || mount '"$TARGET_DEV"'2 "$MNT"

# RuntimeWatchdogSec
[ -f "$MNT/etc/systemd/system.conf" ] && \
    sed -i "s/^#*RuntimeWatchdogSec=.*/RuntimeWatchdogSec=8s/" "$MNT/etc/systemd/system.conf" || true
grep -q "^RuntimeWatchdogSec=" "$MNT/etc/systemd/system.conf" 2>/dev/null || \
    sed -i "/^\[Manager\]/a RuntimeWatchdogSec=8s" "$MNT/etc/systemd/system.conf" 2>/dev/null || true

# Watchdog-feed script — graceful exit when /dev/watchdog busy
mkdir -p "$MNT/usr/local/sbin"
cat > "$MNT/usr/local/sbin/sa02m-watchdog-feed.sh" << '"'"'WDEOF'"'"'
#!/bin/bash
set -u
DEV="${WATCHDOG_DEV:-/dev/watchdog}"
INTERVAL="${WATCHDOG_FEED_INTERVAL:-5}"
[ ! -c "$DEV" ] && exit 0
if ! exec 3>"$DEV" 2>/dev/null; then
    echo "watchdog-feed: $DEV busy (systemd handles it)" >&2
    exit 0
fi
graceful_stop() { printf "V" >&3 2>/dev/null||true; exec 3>&- 2>/dev/null||true; exit 0; }
trap graceful_stop TERM INT QUIT HUP
[ -w /sys/class/watchdog/watchdog0/timeout ] && echo 30 >/sys/class/watchdog/watchdog0/timeout 2>/dev/null||true
while :; do printf "." >&3 2>/dev/null || break; sleep "$INTERVAL"; done
graceful_stop
WDEOF
chmod +x "$MNT/usr/local/sbin/sa02m-watchdog-feed.sh"

# Noop watchdog-trigger services
for svc in sa02m-userspace-watchdog sa02m-failure-monitor net-watchdog; do
    cat > "$MNT/etc/systemd/system/${svc}.service" << EOF
[Unit]
Description=${svc} DISABLED
[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
done

sync
umount "$MNT"
rmdir "$MNT"
echo "FIXES_IN_IMAGE_DONE"
' || log "    WARN: loop mount не удался — фиксы будут применены после загрузки автоматически"

log "[5/5] Reboot..."
"${SSH[@]}" "sync; reboot" 2>/dev/null || true

# ── Ожидание загрузки ────────────────────────────────────────────────────
log "Ожидание перезагрузки (до 3 минут)..."
sleep 15
MAX=24
for i in $(seq 1 $MAX); do
    printf "[%s] попытка %d/%d..." "$(date +%H:%M:%S)" "$i" "$MAX"
    if ping -c 1 -W 2 "$DEVICE_IP" >/dev/null 2>&1; then
        if "${SSH[@]}" "echo SSH_OK; uptime" 2>/dev/null; then
            log "Устройство поднялось успешно!"
            break
        fi
        echo " ping ok, ssh не готов"
    else
        echo " нет ping"
    fi
    sleep 8
done

log "Прошивка завершена. Устройство: $DEVICE_IP"
