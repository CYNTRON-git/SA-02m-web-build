#!/bin/bash
echo "Content-type: text/plain; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""

check_auth() {
    [[ -n "${HTTP_COOKIE:-}" && "$HTTP_COOKIE" =~ session_token=cyntron_session ]] && return 0
    return 1
}

timeout_run() {
    local sec=${1:-5}
    shift || true
    if command -v timeout >/dev/null 2>&1; then
        timeout "$sec" "$@"
    else
        "$@"
    fi
}

print_section() {
    printf '\n===== %s =====\n' "$1"
}

SNAPSHOT_FILE="${SNAPSHOT_FILE:-/run/sa02m_failure_monitor_state.txt}"

if ! check_auth; then
    echo "Нет доступа"
    exit 0
fi

echo "SSH web debug snapshot"
echo "Generated: $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)"
echo "Host: $(hostname 2>/dev/null || echo unknown)"
echo "Uptime: $(uptime 2>/dev/null || echo unavailable)"

print_section "failure monitor snapshot"
timeout_run 3 sh -c 'cat "'"$SNAPSHOT_FILE"'" 2>/dev/null || echo "snapshot unavailable"'

print_section "failure monitor log tail"
timeout_run 3 sh -c 'tail -n 80 /var/log/sa02m_failure_monitor.log 2>/dev/null || echo "failure monitor log unavailable"'

print_section "prepare working board log tail"
timeout_run 3 sh -c 'tail -n 40 /var/log/sa02m_prepare_working_board.log 2>/dev/null || echo "prepare log unavailable"'

print_section "root account"
timeout_run 3 sh -c 'getent passwd root 2>/dev/null || grep "^root:" /etc/passwd 2>/dev/null || echo "root passwd entry unavailable"'

print_section "board and serial map"
timeout_run 3 sh -c 'printf "model=%s\n" "$(tr -d "\0" < /proc/device-tree/model 2>/dev/null || echo unknown)"; cat /etc/sa02m_serial_map.conf 2>/dev/null || echo "serial map unavailable"'

print_section "sshd effective config"
timeout_run 5 sh -c 'sshd -T 2>/dev/null | grep -E "^(usepam|usedns|permitrootlogin|maxsessions|maxstartups|passwordauthentication|pubkeyauthentication|forcecommand|authorizedkeysfile|subsystem)" || echo "sshd -T unavailable"'

print_section "pam sshd"
timeout_run 3 sh -c 'sed -n "1,160p" /etc/pam.d/sshd 2>/dev/null || echo "/etc/pam.d/sshd unavailable"'

print_section "auth log tail"
timeout_run 4 sh -c 'tail -n 80 /var/log/auth.log 2>/dev/null || journalctl -u ssh -u ssh.service -u sshd --no-pager -n 80 2>/dev/null || echo "auth log unavailable"'

print_section "i2c devices"
timeout_run 3 sh -c 'ls -l /dev/i2c* 2>/dev/null || echo "/dev/i2c* unavailable"'

print_section "i2c bus 2 scan"
timeout_run 8 sh -c 'i2cdetect -y 2 2>/dev/null || echo "i2cdetect bus 2 unavailable"'

print_section "i2c bus 3 scan"
timeout_run 8 sh -c 'i2cdetect -y 3 2>/dev/null || echo "i2cdetect bus 3 unavailable"'

print_section "rtc sysfs"
timeout_run 4 sh -c 'for d in /sys/class/rtc/rtc*; do [ -d "$d" ] || continue; echo "RTC: $d"; for f in name date time since_epoch dev hctosys uevent; do [ -r "$d/$f" ] || continue; printf "  %s=" "$f"; sed -n "1,20p" "$d/$f" 2>/dev/null || true; done; done; [ -d /sys/class/rtc ] || echo "/sys/class/rtc unavailable"'

print_section "rtc kernel log"
timeout_run 4 sh -c 'dmesg 2>/dev/null | grep -Ei "pcf|rtc|i2c" | tail -n 120 || echo "rtc/i2c log unavailable"'
