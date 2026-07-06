#!/usr/bin/env python3
"""Send gratuitous ARP for a network interface.

Usage: sa02m-grat-arp.py [iface]
Default iface: eth0

Gratuitous ARP updates ARP caches on all hosts in the LAN so they
don't have to re-ARP when their cache entry for this device expires.
Needed on networks without a working gateway (no periodic traffic
that would keep ARP caches warm).
"""
import socket
import struct
import fcntl
import sys

SIOCGIFHWADDR = 0x8927
SIOCGIFADDR   = 0x8915


def _ioctl(iface: str, req: int) -> bytes:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return fcntl.ioctl(s.fileno(), req, struct.pack('256s', iface[:15].encode()))
    finally:
        s.close()


def get_mac(iface: str) -> bytes:
    return _ioctl(iface, SIOCGIFHWADDR)[18:24]


def get_ip(iface: str) -> bytes:
    return _ioctl(iface, SIOCGIFADDR)[20:24]


def send_gratuitous_arp(iface: str) -> None:
    mac = get_mac(iface)
    ip  = get_ip(iface)

    # Ethernet frame: dst=broadcast, src=our MAC, ethertype=ARP
    eth = b'\xff\xff\xff\xff\xff\xff' + mac + b'\x08\x06'

    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as s:
        s.bind((iface, 0))
        # op=1 ARP REQUEST (RFC 5227 ARP Announcement): target_mac=00:00:00:00:00:00
        arp_req = struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1)
        arp_req += mac + ip + b'\x00' * 6 + ip
        s.send(eth + arp_req)
        # op=2 ARP REPLY (gratuitous reply): target_mac=broadcast
        # More widely accepted by Windows in ARP FAILED/STALE states
        arp_rep = struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2)
        arp_rep += mac + ip + b'\xff' * 6 + ip
        s.send(eth + arp_rep)


if __name__ == '__main__':
    iface = sys.argv[1] if len(sys.argv) > 1 else 'eth0'
    try:
        send_gratuitous_arp(iface)
    except OSError as e:
        print(f'sa02m-grat-arp: {iface}: {e}', file=sys.stderr)
        sys.exit(1)
