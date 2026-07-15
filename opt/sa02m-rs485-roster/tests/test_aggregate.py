# -*- coding: utf-8 -*-
"""Unit tests for the RS-485 roster aggregator merge + provider fallback logic.

Run: python3 -m unittest discover -s opt/sa02m-rs485-roster/tests
(the package is a plain-script dir, so we add it to sys.path here)."""

import json
import os
import sys
import tempfile
import time
import unittest

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PKG_DIR)

import aggregate  # noqa: E402
import providers  # noqa: E402
import rs485_roster_cgi  # noqa: E402


def _entry(port, addr, model, ours, online, source, ts=0.0):
    return {"port": port, "addr": addr, "model": model, "ours": ours,
            "online": online, "source": source, "ts": ts}


class TestMerge(unittest.TestCase):
    def test_bridge_outranks_scan_for_same_port_addr(self):
        entries = [
            _entry("COM4", 6, "AO6AI6", True, None, "scan", ts=1),
            _entry("COM4", 6, "AO6AI6", True, True, "bridge", ts=2),
        ]
        merged = aggregate.merge_entries(entries)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "bridge")
        self.assertIs(merged[0]["online"], True)

    def test_disjoint_addrs_both_kept(self):
        entries = [
            _entry("COM4", 6, "AO6AI6", True, True, "bridge"),
            _entry("COM4", 7, "DO16", True, None, "scan"),
        ]
        self.assertEqual(len(aggregate.merge_entries(entries)), 2)


class TestBuildPorts(unittest.TestCase):
    def test_live_port_reports_bridge_and_chip_online(self):
        ports = aggregate.build_ports([
            _entry("COM4", 6, "AO6AI6", True, True, "bridge"),
            _entry("COM4", 8, "DO4DI6", True, False, "bridge"),
        ])
        blk = ports["COM4"]
        self.assertEqual(blk["source"], "bridge")
        self.assertTrue(blk["live"])
        self.assertEqual([m["addr"] for m in blk["ours"]], [6, 8])

    def test_scan_only_port_is_not_live_and_third_party_online_is_null(self):
        ports = aggregate.build_ports([
            _entry("COM5", 3, "DO4DI6", True, None, "scan"),
            _entry("COM5", 20, "", False, None, "scan"),  # third-party responder
        ])
        blk = ports["COM5"]
        self.assertEqual(blk["source"], "scan")
        self.assertFalse(blk["live"])
        self.assertEqual(blk["third_party"]["total"], 1)
        self.assertIsNone(blk["third_party"]["online"])  # no false zero

    def test_our_scan_chip_keeps_online_none(self):
        ports = aggregate.build_ports([
            _entry("COM5", 3, "DO4DI6", True, None, "scan"),
        ])
        self.assertIsNone(ports["COM5"]["ours"][0]["online"])

    def test_mixed_source_bridge_port_scan_chip_not_coloured_live(self):
        # A BRIDGE-owned port (live) carrying one bridge-polled module AND one
        # scan-only module (never polled by the bridge). The scan chip must keep
        # online=None + source="scan" so the UI paints it grey, never a false red.
        ports = aggregate.build_ports([
            _entry("COM4", 6, "AO6AI6", True, True, "bridge"),   # live
            _entry("COM4", 9, "DO16", True, None, "scan"),       # scan-only, addr disjoint
        ])
        blk = ports["COM4"]
        self.assertTrue(blk["live"])          # port is bridge-owned
        by_addr = {m["addr"]: m for m in blk["ours"]}
        self.assertIs(by_addr[6]["online"], True)
        self.assertEqual(by_addr[6]["source"], "bridge")
        self.assertIsNone(by_addr[9]["online"])       # NOT False → no live-offline claim
        self.assertEqual(by_addr[9]["source"], "scan")


class TestProviderFallback(unittest.TestCase):
    def test_stale_bridge_roster_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "_roster.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time() - 999, "devices": []}, fh)
            p = providers.MqttBridgeProvider(path=path, stale_s=60)
            self.assertFalse(p.available())

    def test_fresh_bridge_roster_yields_live_entries(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "_roster.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "devices": [
                    {"port": "COM4", "addr": 6, "model": "AO6AI6",
                     "ours": True, "online": True}]}, fh)
            p = providers.MqttBridgeProvider(path=path, stale_s=60)
            self.assertTrue(p.available())
            entries = p.entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["source"], "bridge")
            self.assertIs(entries[0]["online"], True)

    def test_scan_provider_reads_port_files(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "COM5.json"), "w", encoding="utf-8") as fh:
                json.dump({"port": "COM5", "ts": 1, "devices": [
                    {"addr": 3, "model": "DO4DI6", "ours": True}]}, fh)
            p = providers.FlasherScanProvider(cache_dir=td)
            self.assertTrue(p.available())
            entries = p.entries()
            self.assertEqual(entries[0]["port"], "COM5")
            self.assertIsNone(entries[0]["online"])
            self.assertEqual(entries[0]["source"], "scan")


class TestCgiHelper(unittest.TestCase):
    def test_helper_preserves_per_chip_source_and_null_online(self):
        # The CGI helper must carry each chip's OWN source + tri-state online so the
        # UI colours per chip. Mixed bridge port: live chip + scan-only chip.
        block = {
            "source": "bridge", "live": True, "ts": 123.0,
            "ours": [
                {"addr": 6, "model": "AO6AI6", "online": True, "source": "bridge"},
                {"addr": 9, "model": "DO16", "online": None, "source": "scan"},
            ],
            "third_party": {"total": 0, "online": None},
        }
        mods = rs485_roster_cgi._modules_for(block, 123.0)
        by_addr = {m["addr"]: m for m in mods["ours"]}
        self.assertEqual(by_addr[6]["source"], "bridge")
        self.assertIs(by_addr[6]["online"], True)
        self.assertEqual(by_addr[9]["source"], "scan")
        self.assertIsNone(by_addr[9]["online"])   # never coerced to False


class TestEndToEnd(unittest.TestCase):
    def test_aggregate_prefers_live_bridge_over_scan(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = os.path.join(td, "_roster.json")
            scan_dir = os.path.join(td, "scan")
            os.makedirs(scan_dir)
            with open(bridge, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "devices": [
                    {"port": "COM4", "addr": 6, "model": "AO6AI6",
                     "ours": True, "online": True}]}, fh)
            with open(os.path.join(scan_dir, "COM4.json"), "w", encoding="utf-8") as fh:
                json.dump({"port": "COM4", "ts": 1, "devices": [
                    {"addr": 6, "model": "AO6AI6", "ours": True}]}, fh)
            provs = [providers.MqttBridgeProvider(path=bridge, stale_s=60),
                     providers.FlasherScanProvider(cache_dir=scan_dir)]
            result = aggregate.aggregate(provs)
            blk = result["ports"]["COM4"]
            self.assertTrue(blk["live"])
            self.assertIs(blk["ours"][0]["online"], True)


if __name__ == "__main__":
    unittest.main()
