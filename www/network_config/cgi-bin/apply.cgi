#!/bin/bash
[[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] || {
    echo "Content-type: text/html"; echo "Location: /login.html"; echo ""; exit 0; }

read -r -n "${CONTENT_LENGTH:-0}" POST_DATA

decode() { printf '%b' "$(echo "$1" | sed 's/+/ /g; s/%/\\x/g')"; }
get_f() { decode "$(echo "$POST_DATA" | tr '&' '\n' | grep "^${1}=" | cut -d= -f2-)"; }

NET_IFACE=$(get_f "net_iface")
IP=$(get_f "ip"); NETMASK=$(get_f "netmask"); GATEWAY=$(get_f "gateway"); DNS=$(get_f "dns")
IP_ETH1=$(get_f "ip_eth1"); NETMASK_ETH1=$(get_f "netmask_eth1")
GATEWAY_ETH1=$(get_f "gateway_eth1"); DNS_ETH1=$(get_f "dns_eth1")
ETH1_ENABLE=$(get_f "eth1_enable"); SKIP_NETWORK=$(get_f "skip_network")
TIMEZONE=$(get_f "timezone"); DATETIME=$(get_f "datetime")

REDIRECT="applied"

# ── eth0 config ────────────────────────────────────────────────────────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth0" ] && [ -n "$IP" ] && [ -n "$NETMASK" ]; then
    CFG="auto eth0\niface eth0 inet static\n    address $IP\n    netmask $NETMASK"
    [ -n "$GATEWAY" ] && CFG="$CFG\n    gateway $GATEWAY"
    [ -n "$DNS" ]     && CFG="$CFG\n    dns-nameservers $DNS"
    echo -e "$CFG" | sudo tee /etc/network/interfaces.d/eth0.conf >/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') eth0.conf updated IP=$IP" >> /var/log/sa02m_install.log 2>&1
fi

# ── eth1 config ────────────────────────────────────────────────────────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth1" ]; then
    if [ "$ETH1_ENABLE" = "1" ] && [ -n "$IP_ETH1" ] && [ -n "$NETMASK_ETH1" ]; then
        CFG1="auto eth1\niface eth1 inet static\n    address $IP_ETH1\n    netmask $NETMASK_ETH1"
        [ -n "$GATEWAY_ETH1" ] && CFG1="$CFG1\n    gateway $GATEWAY_ETH1"
        [ -n "$DNS_ETH1" ]     && CFG1="$CFG1\n    dns-nameservers $DNS_ETH1"
        echo -e "$CFG1" | sudo tee /etc/network/interfaces.d/eth1.conf >/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf updated IP=$IP_ETH1" >> /var/log/sa02m_install.log 2>&1
    else
        sudo rm -f /etc/network/interfaces.d/eth1.conf
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf removed" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── Timezone ───────────────────────────────────────────────────────────────
if [ -n "$TIMEZONE" ]; then
    if sudo timedatectl set-timezone "$TIMEZONE" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') timezone set to $TIMEZONE" >> /var/log/sa02m_install.log 2>&1
    else
        REDIRECT="error_tz"
    fi
fi

# ── System time ────────────────────────────────────────────────────────────
if [ -n "$DATETIME" ] && [ "$REDIRECT" = "applied" ]; then
    if sudo date -s "$DATETIME" >/dev/null 2>&1; then
        sudo hwclock -w >/dev/null 2>&1 || true
        echo "$(date '+%Y-%m-%d %H:%M:%S') datetime set to $DATETIME" >> /var/log/sa02m_install.log 2>&1
    else
        REDIRECT="error_time"
    fi
fi

echo "Content-type: text/html"
echo "Location: /?status=${REDIRECT}"
echo ""
