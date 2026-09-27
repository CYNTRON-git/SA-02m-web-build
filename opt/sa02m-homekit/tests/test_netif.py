"""netif.py — one wired interface, never a modem, never a wildcard (D3)."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import tempfile
import unittest
from unittest import mock

from sa02m_homekit import netif

REFUSED = ("ppp0", "wwan0", "usb0", "eth2", "lo", "", "eth0;reboot", "../eth0", "eth0 ")


def _ifreq(addr: str) -> bytes:
    """What SIOCGIFADDR hands back: name (16) + sockaddr_in, the IPv4 at [20:24]."""
    return struct.pack("16sH2s4s8s", b"eth0", socket.AF_INET, b"\x00\x00",
                       socket.inet_aton(addr), b"\x00" * 8).ljust(256, b"\x00")


class Ipv4Tests(unittest.TestCase):
    def test_names_outside_the_allow_list_never_reach_the_kernel(self):
        with mock.patch.object(netif.fcntl, "ioctl",
                               side_effect=AssertionError("ioctl for a refused name")):
            for name in REFUSED:
                self.assertIsNone(netif.ipv4_address(name), repr(name))

    def test_address_of_the_chosen_interface(self):
        with mock.patch.object(netif.fcntl, "ioctl", return_value=_ifreq("192.168.1.136")) as ioctl:
            self.assertEqual(netif.ipv4_address("eth1"), "192.168.1.136")
        self.assertEqual(ioctl.call_args[0][1], netif._SIOCGIFADDR)
        self.assertTrue(ioctl.call_args[0][2].startswith(b"eth1\x00"))

    def test_unusable_addresses_are_no_address(self):
        for addr in ("0.0.0.0", "169.254.10.2"):
            with mock.patch.object(netif.fcntl, "ioctl", return_value=_ifreq(addr)):
                self.assertIsNone(netif.ipv4_address("eth0"), addr)

    def test_no_address_or_absent_interface(self):
        with mock.patch.object(netif.fcntl, "ioctl", side_effect=OSError(99, "no address")):
            self.assertIsNone(netif.ipv4_address("eth0"))


class SysfsTests(unittest.TestCase):
    def setUp(self):
        self.sys_net = tempfile.mkdtemp()
        for name, mac in (("eth0", "02:42:AC:11:00:02\n"), ("ppp0", "00:00:00:00:00:00\n"),
                          ("eth1", "not-a-mac\n")):
            os.makedirs(os.path.join(self.sys_net, name))
            with open(os.path.join(self.sys_net, name, "address"), "w", encoding="ascii") as fh:
                fh.write(mac)

    def tearDown(self):
        shutil.rmtree(self.sys_net, ignore_errors=True)

    def test_interface_present_only_for_wired_names(self):
        self.assertTrue(netif.interface_present("eth0", self.sys_net))
        self.assertTrue(netif.interface_present("eth1", self.sys_net))
        self.assertFalse(netif.interface_present("ppp0", self.sys_net))  # present, but a modem
        shutil.rmtree(os.path.join(self.sys_net, "eth1"))
        self.assertFalse(netif.interface_present("eth1", self.sys_net))

    def test_mac_address(self):
        self.assertEqual(netif.mac_address("eth0", self.sys_net), "02:42:ac:11:00:02")
        self.assertIsNone(netif.mac_address("eth1", self.sys_net))   # malformed
        self.assertIsNone(netif.mac_address("ppp0", self.sys_net))   # refused name
        self.assertIsNone(netif.mac_address("eth0", os.path.join(self.sys_net, "absent")))

    def test_bridge_name_from_the_last_three_mac_bytes(self):
        self.assertEqual(netif.bridge_name("02:42:ac:11:00:02"), "SA-02m 110002")
        self.assertEqual(netif.bridge_name(None), "SA-02m")
        self.assertEqual(netif.bridge_name("garbage"), "SA-02m")


if __name__ == "__main__":
    unittest.main()
