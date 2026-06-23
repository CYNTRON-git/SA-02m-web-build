#!/bin/bash
# SA-02m: audit (and optionally fix) write permissions for service data directories.
# Run on device as root. Remote: py -3 tools/ssh/sa02m-check-perms.py [--fix]
set -euo pipefail

MODE="${1:-audit}"
FIX=0
[ "$MODE" = "--fix" ] || [ "$MODE" = "fix" ] && FIX=1

WEB_ROOT="${SA02M_WEB_ROOT:-/var/www/network_config}"
FLASHER_USER="${SA02M_FLASHER_USER:-sa02m-flasher}"

issues=0
fixes=0

log_issue() { issues=$((issues + 1)); printf 'ISSUE\t%s\t%s\t%s\n' "$1" "$2" "$3"; }
log_ok()    { printf 'OK\t%s\t%s\t%s\n' "$1" "$2" "$3"; }
log_fix()   { fixes=$((fixes + 1)); printf 'FIX\t%s\t%s\t%s\n' "$1" "$2" "$3"; }

stat_perm() {
    stat -c '%a %U:%G' "$1" 2>/dev/null || echo '? ?:?'
}

is_rw_mount() {
    local mp="$1"
    local opts
    opts=$(findmnt -no OPTIONS "$mp" 2>/dev/null || echo '')
    [ -z "$opts" ] && return 0
    # ext4 "errors=remount-ro" contains "ro" substring — match whole option only
    if grep -qE '(^|,)(ro)(,|$)' <<<"$opts"; then
        return 1
    fi
    return 0
}

write_test() {
    local path="$1" user="$2"
    [ ! -e "$path" ] && return 2
    if [ -f "$path" ]; then
        sudo -u "$user" test -w "$path" 2>/dev/null && return 0
        return 1
    fi
    local tmp="$path/.sa02m_wtest_$$"
    if sudo -u "$user" touch "$tmp" 2>/dev/null; then
        sudo -u "$user" rm -f "$tmp" 2>/dev/null
        return 0
    fi
    return 1
}

dir_mode_ok() {
    local have="$1" want="$2"
    [ "$have" = "$want" ] && return 0
    # More permissive than required is acceptable (vendor mplc4 often uses 777).
    [ "$have" -gt "$want" ] 2>/dev/null && return 0
    return 1
}

ensure_dir() {
    local path="$1" mode="$2" owner="$3" group="$4"
    if [ ! -d "$path" ]; then
        if [ "$FIX" -eq 1 ]; then
            install -d -m "$mode" -o "$owner" -g "$group" "$path"
            log_fix "$path" "created" "${mode} ${owner}:${group}"
        else
            log_issue "$path" "MISSING" "dir expected ${mode} ${owner}:${group}"
        fi
        return
    fi
    local cur perm own
    read -r perm own <<<"$(stat_perm "$path")"
    local own_ok=0 dir_ok=0
    [ "$own" = "${owner}:${group}" ] && own_ok=1
    dir_mode_ok "$perm" "$mode" && dir_ok=1
    if [ "$own_ok" -eq 1 ] && [ "$dir_ok" -eq 1 ]; then
        log_ok "$path" "${perm} ${own}" "dir"
    elif [ "$own_ok" -eq 0 ]; then
        if [ "$FIX" -eq 1 ]; then
            chown "$owner:$group" "$path"
            chmod "$mode" "$path"
            log_fix "$path" "chown/chmod" "${mode} ${owner}:${group} (was ${perm} ${own})"
        else
            log_issue "$path" "owner" "want ${owner}:${group}, have ${own}"
        fi
    elif [ "$dir_ok" -eq 0 ]; then
        if [ "$FIX" -eq 1 ]; then
            chmod "$mode" "$path"
            log_fix "$path" "chmod" "${mode} (was ${perm})"
        else
            log_issue "$path" "perm" "want >=${mode}, have ${perm}"
        fi
    else
        log_ok "$path" "${perm} ${own}" "dir"
    fi
}

ensure_file_mode() {
    local path="$1" mode="$2" owner="$3" group="$4"
    [ ! -f "$path" ] && return
    local cur perm own
    read -r perm own <<<"$(stat_perm "$path")"
    local own_ok=0 mode_ok=0
    [ "$own" = "${owner}:${group}" ] && own_ok=1
    [ "$perm" = "$mode" ] || [ "$perm" -gt "$mode" ] 2>/dev/null && mode_ok=1
    if [ "$own_ok" -eq 1 ] && [ "$mode_ok" -eq 1 ]; then
        log_ok "$path" "${perm} ${own}" "file"
    elif [ "$own_ok" -eq 0 ]; then
        if [ "$FIX" -eq 1 ]; then
            chown "$owner:$group" "$path"
            chmod "$mode" "$path"
            log_fix "$path" "chown/chmod" "${mode} ${owner}:${group}"
        else
            log_issue "$path" "owner" "want ${owner}:${group}, have ${own}"
        fi
    elif [ "$mode_ok" -eq 0 ]; then
        if [ "$FIX" -eq 1 ]; then
            chmod "$mode" "$path"
            log_fix "$path" "chmod" "${mode} (was ${perm})"
        else
            log_issue "$path" "perm" "want >=${mode}, have ${perm}"
        fi
    fi
}

check_service_user_write() {
    local path="$1" user="$2" svc="$3"
    [ ! -e "$path" ] && return
    if write_test "$path" "$user"; then
        log_ok "$path" "writable" "$svc as $user"
    else
        if [ "$FIX" -eq 1 ] && [ -d "$path" ]; then
            chmod g+w "$path" 2>/dev/null || chmod o+w "$path" 2>/dev/null || true
            if write_test "$path" "$user"; then
                log_fix "$path" "chmod+w" "$svc as $user"
            else
                log_issue "$path" "NOT_WRITABLE" "$svc as $user"
            fi
        else
            log_issue "$path" "NOT_WRITABLE" "$svc as $user"
        fi
    fi
}

echo "=== SA-02m service permissions ($MODE) ==="
echo "WEB_ROOT=$WEB_ROOT"

for mp in / /opt /var /etc; do
    if ! is_rw_mount "$mp"; then
        log_issue "$mp" "READONLY" "mount blocks persistence"
    else
        log_ok "$mp" "rw" "mount"
    fi
done

echo "--- systemd User= ---"
for u in mplc4.service codesyscontrol.service sa02m-flasher.service sa02m-modbus-mqtt.service mosquitto.service fcgiwrap.service nginx.service; do
    user=$(systemctl show -p User --value "$u" 2>/dev/null || echo '')
    [ -z "$user" ] || [ "$user" = "[not set]" ] && user="root"
    printf 'UNIT\t%s\t%s\n' "$u" "$user"
done

echo "--- MPLC4 / MasterSCADA (runs as root via init.d) ---"
ensure_dir /opt/mplc4 755 root root
ensure_dir /opt/mplc4/server 775 root root
ensure_dir /opt/mplc4/server/cfg 775 root root
ensure_dir /opt/mplc4/log 775 root root
ensure_dir /var/log/mplc4 755 root root
ensure_dir /var/log/mplc4/nginx 775 root root
ensure_dir /var/log/mplc4/dumps 775 root root
check_service_user_write /opt/mplc4/server/cfg root mplc4
check_service_user_write /opt/mplc4 root mplc4
if [ -f /opt/mplc4/backup.bin ]; then
    check_service_user_write /opt/mplc4/backup.bin root mplc4
else
    log_issue /opt/mplc4/backup.bin MISSING "project backup (created on first save)"
fi

echo "--- CODESYS (runs as root via init.d) ---"
ensure_dir /etc/codesyscontrol 750 root root
ensure_dir /var/opt/codesys 755 root root
ensure_dir /var/opt/codesys/PlcLogic 755 root root
ensure_file_mode /etc/codesyscontrol/CODESYSControl.cfg 644 root root
ensure_file_mode /etc/codesyscontrol/CODESYSControl_User.cfg 644 root root
check_service_user_write /var/opt/codesys/PlcLogic root codesys
check_service_user_write /etc/codesyscontrol root codesys

echo "--- Web UI ($WEB_ROOT, user www-data) ---"
check_service_user_write "$WEB_ROOT" www-data nginx/fcgiwrap
for sub in cgi-bin static; do
    [ -d "$WEB_ROOT/$sub" ] && check_service_user_write "$WEB_ROOT/$sub" www-data nginx/fcgiwrap
done
ensure_file_mode /etc/sa02m-modbus-mqtt.yaml 660 root www-data
ensure_file_mode /etc/sa02m-gateway.yaml 660 root www-data
ensure_file_mode /etc/sa02m_web.env 640 root www-data

echo "--- sa02m-flasher (user $FLASHER_USER) ---"
ensure_dir /opt/sa02m-flasher 755 "$FLASHER_USER" "$FLASHER_USER"
ensure_dir /var/lib/sa02m-flasher 755 "$FLASHER_USER" "$FLASHER_USER"
ensure_dir /var/log/sa02m-flasher 750 "$FLASHER_USER" "$FLASHER_USER"
check_service_user_write /var/lib/sa02m-flasher "$FLASHER_USER" sa02m-flasher
check_service_user_write /var/log/sa02m-flasher "$FLASHER_USER" sa02m-flasher

echo "--- mosquitto (user mosquitto) ---"
ensure_dir /var/lib/mosquitto 770 mosquitto mosquitto
ensure_dir /var/log/mosquitto 755 mosquitto mosquitto
check_service_user_write /var/lib/mosquitto mosquitto mosquitto

echo "--- other sa02m (root) ---"
for p in /etc/sa02m-modbus-mqtt.yaml /etc/sa02m-mqtt-opcua.conf /etc/sa02m-mqtt-snmp.conf /etc/sa02m_flasher.conf; do
    [ -f "$p" ] && log_ok "$p" "$(stat_perm "$p")" "config"
done

echo "--- tmpfs warning (data must NOT live here) ---"
for bad in /tmp /run; do
    if [ -d "$bad" ]; then
        hits=$(find /opt/mplc4 /var/opt/codesys -type l -lname "$bad/*" 2>/dev/null | wc -l)
        [ "$hits" -gt 0 ] && log_issue "$bad" "SYMLINK" "$hits project paths point to tmpfs"
    fi
done

echo "=== SUMMARY issues=$issues fixes=$fixes ==="
exit 0
