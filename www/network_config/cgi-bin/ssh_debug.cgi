#!/bin/bash
echo "Content-type: text/html; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}

if ! check_auth; then
    echo '<!DOCTYPE html><html><body><h2>&#1053;&#1077;&#1090; &#1076;&#1086;&#1089;&#1090;&#1091;&#1087;&#1072;</h2></body></html>'
    exit 0
fi

timeout_run() {
    local sec=${1:-5}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

html_esc() {
    sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

html_esc_str() {
    printf '%s' "$1" | html_esc
}

# wrap_section TITLE [1=open(default)|0=closed]
# Reads stdin, wraps in collapsible <details> with HTML-escaped <pre> content
wrap_section() {
    local title=$1 open_flag=${2:-1} attr=""
    [ "$open_flag" = "1" ] && attr=" open"
    printf '<details%s>\n<summary>%s</summary>\n<pre>' "$attr" "$title"
    html_esc
    printf '</pre>\n</details>\n'
}

SNAPSHOT_FILE="${SNAPSHOT_FILE:-/run/sa02m_failure_monitor_state.txt}"
HOST="$(hostname 2>/dev/null || echo unknown)"
GENERATED="$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)"
UPTIME_INFO="$(uptime 2>/dev/null || echo unavailable)"

HOST_H="$(html_esc_str "$HOST")"
GENERATED_H="$(html_esc_str "$GENERATED")"
UPTIME_H="$(html_esc_str "$UPTIME_INFO")"

cat <<HTML
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SA-02m Debug &mdash; ${HOST_H}</title>
<style>
*{box-sizing:border-box}
body{font-family:'Courier New',monospace;background:#0f1117;color:#c9d1d9;margin:0;padding:16px;font-size:14px}
h1{color:#58a6ff;font-size:1.1em;margin:0 0 6px;font-weight:bold}
.meta{color:#6e7681;font-size:.84em;margin-bottom:10px;line-height:1.8}
.meta b{color:#8b949e}
a.refresh{display:inline-block;padding:5px 16px;background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:6px;text-decoration:none;font-size:.84em;margin-bottom:16px;font-family:inherit;transition:background .15s}
a.refresh:hover{background:#30363d;color:#79c0ff}
h3.sec-group{color:#6e7681;font-size:.76em;text-transform:uppercase;letter-spacing:.1em;margin:16px 0 5px;border-bottom:1px solid #21262d;padding-bottom:3px;font-weight:normal}
details{margin:0 0 4px;border:1px solid #21262d;border-radius:6px;overflow:hidden}
summary{background:#161b22;padding:8px 12px;cursor:pointer;font-weight:bold;color:#58a6ff;font-size:.84em;list-style:none;user-select:none;display:flex;align-items:center;gap:7px;transition:background .1s}
summary::-webkit-details-marker{display:none}
summary::before{content:"\25B6";font-size:.65em;color:#484f58;flex-shrink:0;width:10px;line-height:1}
details[open]>summary::before{content:"\25BC";color:#58a6ff}
summary:hover{background:#1c2128}
details[open]>summary{border-bottom:1px solid #21262d}
pre{margin:0;padding:10px 14px;font-size:.78em;line-height:1.5;overflow-x:auto;white-space:pre-wrap;word-break:break-word;background:#0d1117;color:#b1bac4}
</style>
</head>
<body>
<h1>&#9881; SA-02m SSH Debug Snapshot</h1>
<div class="meta">
  Host: <b>${HOST_H}</b>&nbsp;&nbsp;|&nbsp;&nbsp;Generated: <b>${GENERATED_H}</b><br>
  ${UPTIME_H}
</div>
<a class="refresh" href="">&#8635;&nbsp; Обновить</a>

<h3 class="sec-group">&#9654; Системные метрики</h3>
HTML

# ─────────────────────────────────────────────────────────────────────────────
# 1. ЗАГРУЗКА СИСТЕМЫ
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== UPTIME ==="
    timeout_run 3 uptime 2>&1 || echo "ERROR: uptime failed"
    echo ""
    echo "=== TOP 12 PROCESSES (by CPU) ==="
    timeout_run 5 ps aux --sort=-%cpu 2>/dev/null | head -13 || echo "ERROR: ps failed"
    echo ""
    echo "=== MEMORY ==="
    timeout_run 3 free -h 2>&1 || echo "ERROR: free failed"
    echo ""
    echo "=== DISK ==="
    timeout_run 3 df -h 2>&1 || echo "ERROR: df failed"
} | wrap_section "&#128200; Загрузка системы (uptime / ps / free / df)"

# ─────────────────────────────────────────────────────────────────────────────
# 2. DMESG ERRORS
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== DMESG: error|fail|warn|locked|timeout|i2c|eth|phy (last 30 matches) ==="
    timeout_run 5 dmesg 2>/dev/null | grep -iE 'error|fail|warn|locked|timeout|i2c|eth|phy' | tail -30 \
        || echo "ERROR: dmesg unavailable"
} | wrap_section "&#9888; dmesg (ошибки / предупреждения / I2C / ETH)"

echo '<h3 class="sec-group">&#9654; Сеть</h3>'

# ─────────────────────────────────────────────────────────────────────────────
# 3. СЕТЕВАЯ ДИАГНОСТИКА
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== INTERFACES (ip addr) ==="
    timeout_run 3 ip addr 2>&1 || echo "ERROR: ip addr failed"
    echo ""
    echo "=== ROUTES (ip route) ==="
    timeout_run 3 ip route 2>&1 || echo "ERROR: ip route failed"
    echo ""
    echo "=== /etc/network/interfaces.d/eth0.conf ==="
    cat /etc/network/interfaces.d/eth0.conf 2>/dev/null || echo "eth0.conf unavailable"
    echo ""
    echo "=== /etc/network/interfaces.d/eth1.conf ==="
    cat /etc/network/interfaces.d/eth1.conf 2>/dev/null || echo "eth1.conf unavailable"
    echo ""
    echo "=== ETH0 LINK STATUS ==="
    printf "operstate: "; cat /sys/class/net/eth0/operstate 2>/dev/null || echo "n/a"
    printf "carrier:   "; cat /sys/class/net/eth0/carrier   2>/dev/null || echo "n/a"
    echo ""
    echo "=== ETH1 LINK STATUS ==="
    printf "operstate: "; cat /sys/class/net/eth1/operstate 2>/dev/null || echo "n/a"
} | wrap_section "&#127760; Сетевая диагностика (ip addr / routes / eth.conf)"

echo '<h3 class="sec-group">&#9654; SSH / PAM</h3>'

# ─────────────────────────────────────────────────────────────────────────────
# 4. SSH КОНФИГ
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== /etc/ssh/sshd_config (stripped) ==="
    timeout_run 3 bash -c 'grep -v "^#" /etc/ssh/sshd_config 2>/dev/null | grep -v "^$"' \
        || echo "ERROR: sshd_config unavailable"
    echo ""
    echo "=== /etc/ssh/sshd_config.d/ overrides ==="
    timeout_run 3 bash -c '
        found=0
        for f in /etc/ssh/sshd_config.d/*.conf; do
            [ -f "$f" ] || continue
            found=1
            echo "--- $f ---"
            cat "$f"
        done
        [ "$found" -eq 0 ] && echo "no overrides"
        ls /etc/ssh/sshd_config.d/ 2>/dev/null || echo "(sshd_config.d not found)"
    '
    echo ""
    echo "=== SSHD EFFECTIVE CONFIG (key params) ==="
    timeout_run 5 bash -c 'sshd -T 2>/dev/null | grep -E "^(usepam|usedns|permitrootlogin|maxsessions|maxstartups|passwordauthentication|pubkeyauthentication|forcecommand|authorizedkeysfile|subsystem|logingracetime|maxauthtries)"' \
        || echo "sshd -T unavailable"
    echo ""
    echo "=== /etc/hosts ==="
    cat /etc/hosts 2>/dev/null || echo "unavailable"
    echo ""
    echo "=== /etc/nsswitch.conf ==="
    cat /etc/nsswitch.conf 2>/dev/null || echo "unavailable"
} | wrap_section "&#128274; SSH конфиг (sshd_config / effective / hosts / nsswitch)"

# ─────────────────────────────────────────────────────────────────────────────
# 5. PAM
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== /etc/pam.d/sshd (stripped) ==="
    timeout_run 3 bash -c 'grep -v "^#" /etc/pam.d/sshd 2>/dev/null | grep -v "^$"' \
        || echo "ERROR: /etc/pam.d/sshd unavailable"
    echo ""
    echo "=== /etc/pam.d/common-session (stripped) ==="
    timeout_run 3 bash -c 'grep -v "^#" /etc/pam.d/common-session 2>/dev/null | grep -v "^$"' \
        || echo "ERROR: /etc/pam.d/common-session unavailable"
    echo ""
    echo "=== PAM session/account lines in sshd (hang suspects) ==="
    timeout_run 3 bash -c 'grep -nE "^(session|account)" /etc/pam.d/sshd 2>/dev/null' \
        || echo "no session/account lines"
} | wrap_section "&#128272; PAM конфигурация (pam.d/sshd + common-session)"

# ─────────────────────────────────────────────────────────────────────────────
# 6. UPDATE-MOTD.D
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== MOTD SCRIPTS (ls -la) ==="
    timeout_run 3 ls -la /etc/update-motd.d/ 2>/dev/null || echo "no update-motd.d"
    echo ""
    echo "=== MOTD SCRIPTS CONTENTS ==="
    timeout_run 5 bash -c '
        for f in /etc/update-motd.d/*; do
            [ -f "$f" ] || continue
            echo "--- $f ---"
            cat "$f"
            echo ""
        done
        echo "(end motd)"
    '
} | wrap_section "&#128233; update-motd.d скрипты (pam_motd / hang suspects)"

echo '<h3 class="sec-group">&#9654; I2C / Аппаратура</h3>'

# ─────────────────────────────────────────────────────────────────────────────
# 7. I2C ДИАГНОСТИКА
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== I2C BUSES (sysfs /sys/bus/i2c/devices/) ==="
    ls /sys/bus/i2c/devices/ 2>/dev/null || echo "unavailable"
    echo ""
    echo "=== I2C DEVICE NODES (/dev/i2c*) ==="
    ls -la /dev/i2c* 2>/dev/null || echo "/dev/i2c* unavailable"
    echo ""
    echo "--- i2cdetect bus 0 ---"
    timeout_run 3 i2cdetect -y -r 0 2>&1 || echo "i2c-0: TIMEOUT/ERROR"
    echo ""
    echo "--- i2cdetect bus 1 ---"
    timeout_run 3 i2cdetect -y -r 1 2>&1 || echo "i2c-1: TIMEOUT/ERROR"
    echo ""
    echo "--- i2cdetect bus 2 ---"
    timeout_run 3 i2cdetect -y -r 2 2>&1 || echo "i2c-2: TIMEOUT/ERROR"
    echo ""
    echo "--- i2cdetect bus 3 ---"
    timeout_run 3 i2cdetect -y -r 3 2>&1 || echo "i2c-3: TIMEOUT/ERROR"
    echo ""
    echo "--- I2C kernel messages (dmesg grep i2c|mv64xxx|locked) ---"
    timeout_run 4 dmesg 2>/dev/null | grep -iE 'i2c|mv64xxx|locked' | tail -20 \
        || echo "unavailable"
} | wrap_section "&#128268; I2C диагностика (detect bus 0-3 + dmesg)"

echo '<h3 class="sec-group">&#9654; Сервисы / Процессы</h3>'

# ─────────────────────────────────────────────────────────────────────────────
# 8. СТАТУС СЕРВИСОВ
# ─────────────────────────────────────────────────────────────────────────────
{
    printf "%-40s %-15s %s\n" "SERVICE" "ACTIVE" "ENABLED"
    printf "%-40s %-15s %s\n" "-------" "------" "-------"
    for svc in ssh sshd networking NetworkManager \
               mplc mplc4 klogic klogicd \
               ca_02m led-control sa02m-flasher sa02m-failure-monitor \
               armbian-led-state armbian-hardware-monitor net-watchdog \
               systemd-logind dbus nginx fcgiwrap; do
        status=$(timeout_run 2 systemctl is-active  "$svc" 2>/dev/null || echo "not-found")
        enabled=$(timeout_run 2 systemctl is-enabled "$svc" 2>/dev/null || echo "not-found")
        printf "%-40s %-15s %s\n" "$svc" "$status" "$enabled"
    done
} | wrap_section "&#9881; Статус сервисов (systemctl is-active / is-enabled)"

# ─────────────────────────────────────────────────────────────────────────────
# 9. RS-485 / SERIAL
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== SERIAL DEVICES ==="
    ls -la /dev/ttyS* /dev/ttyUSB* /dev/ttyAMA* /dev/RS-485-* 2>/dev/null || echo "none found"
    echo ""
    echo "=== OPENED BY PROCESSES (fuser) ==="
    timeout_run 3 fuser /dev/ttyS* /dev/RS-485-* 2>/dev/null || echo "none / fuser unavailable"
    echo ""
    echo "=== SERIAL DRIVER STATS (/proc/tty/driver/serial) ==="
    cat /proc/tty/driver/serial 2>/dev/null || echo "unavailable"
} | wrap_section "&#128268; RS-485 / Serial порты"

echo '<h3 class="sec-group">&#9654; Логи</h3>'

# ─────────────────────────────────────────────────────────────────────────────
# 10. SYSTEMD JOURNAL
# ─────────────────────────────────────────────────────────────────────────────
{
    echo "=== JOURNAL last 50 lines ==="
    timeout_run 5 journalctl -n 50 --no-pager 2>/dev/null \
        || echo "ERROR: journalctl unavailable"
    echo ""
    echo "=== SSH daemon log (last 30 lines) ==="
    { timeout_run 5 journalctl -u ssh  -n 30 --no-pager 2>/dev/null || \
      timeout_run 5 journalctl -u sshd -n 30 --no-pager 2>/dev/null; } \
        || echo "ERROR: ssh journal unavailable"
    echo ""
    echo "=== D-Bus log (last 20 lines) ==="
    timeout_run 5 journalctl -u dbus -n 20 --no-pager 2>/dev/null \
        || echo "dbus journal unavailable"
} | wrap_section "&#128196; Systemd Journal (journald last lines)"

echo '<h3 class="sec-group">&#9654; Расширенная диагностика</h3>'

# ─────────────────────────────────────────────────────────────────────────────
# EXISTING SECTIONS (collapsed by default)
# ─────────────────────────────────────────────────────────────────────────────
{
    timeout_run 3 sh -c 'cat "'"$SNAPSHOT_FILE"'" 2>/dev/null || echo "snapshot unavailable"'
} | wrap_section "Failure monitor snapshot" 0

{
    timeout_run 3 sh -c 'tail -n 80 /var/log/sa02m_failure_monitor.log 2>/dev/null || echo "failure monitor log unavailable"'
} | wrap_section "Failure monitor log (tail 80)" 0

{
    timeout_run 3 sh -c 'tail -n 40 /var/log/sa02m_prepare_working_board.log 2>/dev/null || echo "prepare log unavailable"'
} | wrap_section "Prepare working board log (tail 40)" 0

{
    timeout_run 3 sh -c 'getent passwd root 2>/dev/null || grep "^root:" /etc/passwd 2>/dev/null || echo "root passwd entry unavailable"'
} | wrap_section "Root account (passwd)" 0

{
    timeout_run 3 sh -c 'printf "model=%s\n" "$(tr -d "\0" < /proc/device-tree/model 2>/dev/null || echo unknown)"; cat /etc/sa02m_serial_map.conf 2>/dev/null || echo "serial map unavailable"'
} | wrap_section "Board and serial map" 0

{
    timeout_run 4 sh -c '
        for d in /sys/class/rtc/rtc*; do
            [ -d "$d" ] || continue
            echo "RTC: $d"
            for f in name date time since_epoch dev hctosys uevent; do
                [ -r "$d/$f" ] || continue
                printf "  %s=" "$f"
                sed -n "1,20p" "$d/$f" 2>/dev/null || true
            done
        done
        [ -d /sys/class/rtc ] || echo "/sys/class/rtc unavailable"
    '
} | wrap_section "RTC sysfs" 0

{
    timeout_run 4 sh -c 'dmesg 2>/dev/null | grep -Ei "pcf|rtc|i2c" | tail -n 120 || echo "rtc/i2c log unavailable"'
} | wrap_section "RTC / I2C kernel log (dmesg, 120 lines)" 0

{
    timeout_run 3 sh -c 'systemctl cat klogic.service 2>/dev/null || echo "klogic.service not found"'
} | wrap_section "klogic service unit" 0

{
    timeout_run 3 sh -c 'EXEC=$(systemctl show klogic.service -p ExecStart 2>/dev/null | grep -oP "path=[^;]+" | cut -d= -f2); [ -n "$EXEC" ] && cat "$EXEC" 2>/dev/null || echo "exec unavailable"'
} | wrap_section "klogic ExecStart script" 0

{
    timeout_run 3 sh -c 'echo "count: $(dmesg 2>/dev/null | grep -c "new_device.*ds3231")"; dmesg 2>/dev/null | grep "new_device.*ds3231"'
} | wrap_section "DS3231 new_device count and timestamps" 0

{
    timeout_run 3 sh -c 'ps -eo pid,ppid,stat,comm 2>/dev/null | awk "NR==1 || /sshd/"'
} | wrap_section "sshd children and zombie check" 0

{
    timeout_run 3 sh -c 'ss -tnp 2>/dev/null | awk "NR==1 || /:22/"'
} | wrap_section "TCP connections on port 22" 0

{
    timeout_run 5 sh -c 'for u in dbus systemd-logind; do echo "==$u=="; systemctl status "$u" --no-pager -n 3 2>/dev/null | head -8; done'
} | wrap_section "systemd-logind and dbus status" 0

{
    timeout_run 5 sh -c 'grep -rl "ds3231\|new_device" /etc/systemd/system/ /lib/systemd/system/ /usr/lib/systemd/system/ 2>/dev/null | head -10'
} | wrap_section "systemd units with ds3231 / new_device" 0

{
    timeout_run 3 sh -c 'for f in /root/.profile /root/.bashrc; do echo "--- $f ---"; sed -n "1,40p" "$f" 2>/dev/null || echo missing; done'
} | wrap_section "Root shell profile heads" 0

{
    timeout_run 4 sh -c 'tail -n 80 /var/log/auth.log 2>/dev/null || journalctl -u ssh -u ssh.service -u sshd --no-pager -n 80 2>/dev/null || echo "auth log unavailable"'
} | wrap_section "Auth log (tail 80)" 0

echo '</body></html>'
