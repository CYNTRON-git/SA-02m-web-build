#!/bin/sh
set -eu
export PATH=/bin
b=/bin/busybox
trap 's=$?; if [ "$s" != 0 ]; then echo "VPN_BOOT_TEST_FAILED status=$s" >/dev/console; fi; /bin/busybox reboot -f' EXIT
$b mount -t proc proc /proc
$b mount -t sysfs sysfs /sys
$b mount -t devtmpfs devtmpfs /dev
$b mount -t tmpfs tmpfs /run
printf 'icmp 1 ICMP
ipv6-icmp 58 IPv6-ICMP icmpv6
' > /etc/protocols
umask 077
release=$($b uname -r)
[ "$release" = "$($b cat /expected-release)" ]
case "$release" in
    *-rt72-*) [ "$($b cat /sys/kernel/realtime)" = 1 ]; echo "VPN_BOOT PREEMPT_RT=1" ;;
esac
$b grep -q 'H3 Audio Codec' /proc/asound/cards
echo "VPN_BOOT kernel=$release userspace=production-armhf audio=H3"
while read -r module; do load-module "/modules/$module"; done < /load-order
/bin/ip tuntap add dev tunprobe mode tun
/bin/ip link set tunprobe up
/bin/ip -details link show tunprobe
/bin/ip link del tunprobe
[ -c /dev/net/tun ]
/bin/ip link set lo up
for side in a b; do
    /bin/ip netns add "$side"
    /bin/ip link add "wg$side" type wireguard
    /bin/ip link set "wg$side" netns "$side"
    /bin/wg genkey > "/run/$side.key"
    /bin/wg pubkey < "/run/$side.key" > "/run/$side.pub"
done
peer() {
    ns=$1; remote=$2; local4=$3; remote4=$4; local6=$5; remote6=$6; port=$7; peerport=$8
    /bin/ip -n "$ns" link set lo up
    /bin/ip -n "$ns" addr add "$local4/24" dev "wg$ns"
    /bin/ip -n "$ns" -6 addr add "$local6/64" dev "wg$ns" nodad
    /bin/ip netns exec "$ns" /bin/wg set "wg$ns" private-key "/run/$ns.key" listen-port "$port" peer "$($b cat "/run/$remote.pub")" allowed-ips "$remote4/32,$remote6/128" endpoint "127.0.0.1:$peerport"
    /bin/ip -n "$ns" link set "wg$ns" up
}
peer a b 192.0.2.1 192.0.2.2 2001:db8::1 2001:db8::2 43001 43002
peer b a 192.0.2.2 192.0.2.1 2001:db8::2 2001:db8::1 43002 43001
for dest in 'a ping 192.0.2.2' 'b ping 192.0.2.1' 'a ping6 2001:db8::2' 'b ping6 2001:db8::1'; do
    set -- $dest
    /bin/ip netns exec "$1" "/bin/$2" -n -c 2 "$3"
done
for ns in a b; do
    set -- $(/bin/ip netns exec "$ns" /bin/wg show "wg$ns" latest-handshakes)
    [ "$2" -gt 0 ]
    set -- $(/bin/ip netns exec "$ns" /bin/wg show "wg$ns" transfer)
    [ "$2" -gt 0 ]
    [ "$3" -gt 0 ]
    echo "VPN_BOOT peer=$ns rx=$2 tx=$3"
done
echo "VPN_BOOT_TEST_OK kernel=$release"
