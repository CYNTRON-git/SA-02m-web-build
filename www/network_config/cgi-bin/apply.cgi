#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

# FCGI: читать ровно CONTENT_LENGTH байт (read -n останавливается на переводе строки)
POST_DATA=""
if [ -n "${CONTENT_LENGTH:-}" ]; then
    CL=$(printf '%s' "${CONTENT_LENGTH}" | tr -cd '0-9')
    if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
        POST_DATA=$(dd bs=1 count="$CL" 2>/dev/null) || POST_DATA=""
    fi
fi

decode() { printf '%b' "$(printf '%s' "$1" | sed 's/+/ /g; s/%/\\x/g')"; }
get_f() {
    local line val
    line=$(printf '%s' "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | head -1)
    val="${line#*=}"
    decode "$val"
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

NET_IFACE=$(get_f "net_iface")
IP=$(get_f "ip"); NETMASK=$(get_f "netmask"); GATEWAY=$(get_f "gateway"); DNS=$(get_f "dns")
IP_ETH1=$(get_f "ip_eth1"); NETMASK_ETH1=$(get_f "netmask_eth1")
GATEWAY_ETH1=$(get_f "gateway_eth1"); DNS_ETH1=$(get_f "dns_eth1")
ETH0_ENABLE=$(get_f "eth0_enable"); ETH1_ENABLE=$(get_f "eth1_enable"); SKIP_NETWORK=$(get_f "skip_network")
TIMEZONE=$(get_f "timezone")
DATETIME=$(get_f "datetime")
DATETIME=$(printf '%s' "${DATETIME:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

REDIRECT="applied"
timezone_failed=0
time_ok=0

# ── eth0 config ────────────────────────────────────────────────────────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth0" ]; then
    if [ "$ETH0_ENABLE" = "1" ] && [ -n "$IP" ] && [ -n "$NETMASK" ]; then
        CFG="auto eth0\niface eth0 inet static\n    address $IP\n    netmask $NETMASK"
        [ -n "$GATEWAY" ] && CFG="$CFG\n    gateway $GATEWAY"
        [ -n "$DNS" ]     && CFG="$CFG\n    dns-nameservers $DNS"
        echo -e "$CFG" | sudo tee /etc/network/interfaces.d/eth0.conf >/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth0.conf updated IP=$IP" >> /var/log/sa02m_install.log 2>&1
    else
        CFG0="auto eth0\niface eth0 inet dhcp"
        echo -e "$CFG0" | sudo tee /etc/network/interfaces.d/eth0.conf >/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth0.conf set to dhcp" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── eth1 config ────────────────────────────────────────────────────────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth1" ]; then
    if [ "$ETH1_ENABLE" = "1" ] && [ -n "$IP_ETH1" ] && [ -n "$NETMASK_ETH1" ]; then
        CFG1="allow-hotplug eth1\niface eth1 inet static\n    address $IP_ETH1\n    netmask $NETMASK_ETH1"
        [ -n "$GATEWAY_ETH1" ] && CFG1="$CFG1\n    gateway $GATEWAY_ETH1"
        [ -n "$DNS_ETH1" ]     && CFG1="$CFG1\n    dns-nameservers $DNS_ETH1"
        echo -e "$CFG1" | sudo tee /etc/network/interfaces.d/eth1.conf >/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf updated IP=$IP_ETH1" >> /var/log/sa02m_install.log 2>&1
    else
        sudo rm -f /etc/network/interfaces.d/eth1.conf
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf removed" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── Timezone (ошибка таймзоны не блокирует установку времени) ──────────────
if [ -n "$TIMEZONE" ]; then
    if timeout_run 8 sudo -n /usr/bin/timedatectl set-timezone "$TIMEZONE" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') timezone set to $TIMEZONE" >> /var/log/sa02m_install.log 2>&1
    else
        timezone_failed=1
        echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: timedatectl set-timezone failed ($TIMEZONE)" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── System time + RTC ───────────────────────────────────────────────────────
SCRIPT_DIR=$(dirname "$0")
# shellcheck source=lib_rtc.sh
. "$SCRIPT_DIR/lib_rtc.sh"

apply_system_time() {
    local dt="$1"
    # При зависшем dbus timedatectl может не возвращаться → nginx 504; везде timeout.
    timeout_run 6 sudo -n /usr/bin/timedatectl set-ntp false >/dev/null 2>&1 || true
    if timeout_run 18 sudo -n /usr/bin/timedatectl set-time "$dt" >/dev/null 2>&1; then
        return 0
    fi
    if timeout_run 10 sudo -n /bin/date -s "$dt" >/dev/null 2>&1; then
        return 0
    fi
    if timeout_run 10 sudo -n /usr/bin/date -s "$dt" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

sync_hwclock_from_sys() {
    timeout_run 12 sync_rtc_from_system >/dev/null 2>&1 || true
}

if [ -n "$DATETIME" ]; then
    if apply_system_time "$DATETIME"; then
        time_ok=1
        sync_hwclock_from_sys
        echo "$(date '+%Y-%m-%d %H:%M:%S') datetime set to $DATETIME" >> /var/log/sa02m_install.log 2>&1
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') apply.cgi: set time failed ($DATETIME)" >> /var/log/sa02m_install.log 2>&1
        REDIRECT="error_time"
    fi
fi

if [ "$REDIRECT" = "applied" ]; then
    if [ "$time_ok" = "1" ] && [ "$timezone_failed" = "1" ]; then
        REDIRECT="applied_tz_failed"
    elif [ "$time_ok" = "0" ] && [ "$timezone_failed" = "1" ] && [ -z "$DATETIME" ]; then
        REDIRECT="error_tz"
    fi
fi

echo "Content-type: text/html"
echo "Location: /?status=${REDIRECT}"
echo ""
