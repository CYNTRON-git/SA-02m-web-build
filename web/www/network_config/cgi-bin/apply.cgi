#!/bin/bash
read -r POST_DATA

decode() {
    echo "$POST_DATA" | sed -n "s/^.*$1=\([^&]*\).*$/\1/p" \
        | sed 's/%\([0-9A-F][0-9A-F]\)/\\x\1/gI' \
        | xargs -0 printf '%b'
}

IP=$(decode ip)
NETMASK=$(decode netmask)
GATEWAY=$(decode gateway)
DNS=$(decode dns)
IP_ETH1=$(decode ip_eth1)
NETMASK_ETH1=$(decode netmask_eth1)
GATEWAY_ETH1=$(decode gateway_eth1)
DNS_ETH1=$(decode dns_eth1)
DATETIME=$(decode datetime)
TIMEZONE=$(decode timezone)
SKIP_NETWORK=$(decode skip_network)
NET_IFACE=$(decode net_iface)
ETH1_ENABLE=$(decode eth1_enable)

{
echo "[$(date '+%Y-%m-%d %H:%M:%S')] apply.cgi: iface=$NET_IFACE skip_net=$SKIP_NETWORK IP=$IP IP1=$IP_ETH1 TZ=$TIMEZONE DT=$DATETIME"
} >> /var/log/sa02m_install.log 2>&1

REDIRECT="applied"

# ── Network eth0 ─────────────────────────────────────────────────────────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth0" ]; then
    if [ -n "$IP" ] && [ -n "$NETMASK" ]; then
        CONFIG="auto eth0\niface eth0 inet static\n    address $IP\n    netmask $NETMASK"
        [ -n "$GATEWAY" ] && CONFIG="$CONFIG\n    gateway $GATEWAY"
        [ -n "$DNS" ]     && CONFIG="$CONFIG\n    dns-nameservers $DNS"
        echo -e "$CONFIG" | sudo tee /etc/network/interfaces.d/eth0.conf >/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth0.conf written" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── Network eth1 ────────────────────────────────────────────────────────────
if [ "$SKIP_NETWORK" != "1" ] && [ "$NET_IFACE" = "eth1" ]; then
    if [ "$ETH1_ENABLE" = "1" ] && [ -n "$IP_ETH1" ] && [ -n "$NETMASK_ETH1" ]; then
        CONFIG="auto eth1\niface eth1 inet static\n    address $IP_ETH1\n    netmask $NETMASK_ETH1"
        [ -n "$GATEWAY_ETH1" ] && CONFIG="$CONFIG\n    gateway $GATEWAY_ETH1"
        [ -n "$DNS_ETH1" ]     && CONFIG="$CONFIG\n    dns-nameservers $DNS_ETH1"
        echo -e "$CONFIG" | sudo tee /etc/network/interfaces.d/eth1.conf >/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf written" >> /var/log/sa02m_install.log 2>&1
    else
        sudo rm -f /etc/network/interfaces.d/eth1.conf
        echo "$(date '+%Y-%m-%d %H:%M:%S') eth1.conf removed (disabled or empty)" >> /var/log/sa02m_install.log 2>&1
    fi
fi

# ── Timezone ─────────────────────────────────────────────────────────────────
if [ -n "$TIMEZONE" ]; then
    if timedatectl list-timezones 2>/dev/null | grep -qx "$TIMEZONE"; then
        sudo timedatectl set-timezone "$TIMEZONE" \
            && echo "$(date '+%Y-%m-%d %H:%M:%S') TZ=$TIMEZONE OK" >> /var/log/sa02m_install.log 2>&1 \
            || REDIRECT="error_tz"
    else
        REDIRECT="error_tz"
    fi
fi

# ── Date/time ─────────────────────────────────────────────────────────────────
if [ -n "$DATETIME" ] && [ "$REDIRECT" != "error_tz" ]; then
    if date -d "$DATETIME" &>/dev/null; then
        if sudo /bin/date -s "$(date --date="$DATETIME" '+%Y-%m-%d %H:%M:%S')" >/dev/null 2>&1; then
            sudo /sbin/hwclock -w
            REDIRECT="time_updated"
            echo "$(date '+%Y-%m-%d %H:%M:%S') Time set OK" >> /var/log/sa02m_install.log 2>&1
        else
            REDIRECT="error_time"
        fi
    else
        REDIRECT="error_time"
    fi
fi

echo "Content-type: text/html; charset=utf-8"
echo "Location: /cgi-bin/index.cgi?status=$REDIRECT"
echo ""
exit 0
