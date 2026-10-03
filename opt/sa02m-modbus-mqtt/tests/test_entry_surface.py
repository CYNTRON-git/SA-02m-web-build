#!/usr/bin/env python3
"""Frozen entry-module surface + submodule import smoke (bridge decompose).

Tests do `import modbus_mqtt_bridge as bridge`; every name in FROZEN_SURFACE
below is pinned on the entry module so the facade re-exports survive the split
(and any later refactor). Remove nothing from the list without a plan.

Green in BOTH states by design: against the pre-split monolith (the bridge_*
smoke and pool-identity tests skip — no submodules yet) and against the split
tree (they run and the module set must match EXPECTED_MODULES exactly).
"""
from __future__ import annotations

import importlib
import re
import sys
import time as _time
import types
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import modbus_mqtt_bridge as bridge  # noqa: E402

# The planned split set (plan decompose-bridge, Option A). The smoke test pins
# it exactly: a module added later must be declared here AND in both deploy
# scripts (scripts/05-mqtt.sh, scripts/update-www-only.sh).
EXPECTED_MODULES = [
    "bridge_bus",
    "bridge_carel",
    "bridge_device",
    "bridge_dtv_ce",
    "bridge_fmb",
    "bridge_led",
    "bridge_meta",
    "bridge_mqtt",
    "bridge_mr02m",
    "bridge_mr02m_map",
    "bridge_probe",
    "bridge_serial",
    "bridge_spodes",
    "bridge_tcp",
    "bridge_template",
]

# Frozen test/import surface of the entry module. First block: every
# bridge.<name> the test suite references (incl. the patch target get_port).
# Second block: every other formerly-module-level name a downstream script or
# future test may import by the old path.
FROZEN_SURFACE = [
    # -- referenced by the test suite today --
    "FMB_EVT_COIL", "FMB_EVT_DISCRETE", "FMB_EVT_HOLDING", "FMB_EVT_INPUT",
    "FMB_EVT_REBOOT",
    "FastModbusEventPortManager", "FMB_INSURANCE_POLL_S",
    "FMB_RECONFIGURE_BACKOFF_S",
    "build_fmb_configure_events", "build_fmb_configure_events_wb",
    "get_port",
    "DeviceLiveCache", "DevicePoller", "PortCycleScheduler",
    "PortPollScheduler",
    "MR02mPoller", "DTVPoller", "CE02M3Poller", "TemplatePoller",
    "CarelPoller", "LedPoller", "mixed_baud_port_conflicts",
    "MR02M_MODULE_TYPES", "MR02M_AI_CHUNK_ENV",
    "resolve_ai_read_chunk_regs", "_canonical_mr02m_device_name",
    # -- RS-485 wire grammar + transport (bridge_serial) --
    "FMB_ADDR", "MODBUS_INTER_FRAME_DELAY_S", "MODBUS_POST_AI_BLOCK_GAP_S",
    "crc16", "build_request", "build_write_coil", "build_write_register",
    "build_fmb5", "build_fmb_poll_events",
    "ModbusSerial", "WRITEBACK_POLL_GRACE_S", "WritebackWorker",
    "FastModbusScanner",
    # -- Fast Modbus event engine (bridge_fmb) --
    "FMB_EVENT_PERIOD_S", "FMB_EVENT_BURST_S", "FMB_BALANCING_THRESHOLD_S",
    "FMB_MAX_POLL_TIME_S", "FMB_UNPARSED_LOG_PERIOD_S",
    # -- MQTT + live cache (bridge_mqtt) --
    "DEVICE_BASE", "LIVE_CACHE_DIR", "MQTTPublisher",
    "_PRECISION_BY_UNITS", "_ctrl_precision", "_make_title",
    # -- MR-02m register/AI-code domain (bridge_mr02m_map) --
    "MR02M_TYPE_NAMES", "_MR02M_LEGACY_NAME_TOKENS",
    "MR02M_AI_HOLDING_BASE", "MR02M_AI_CHANNEL_STRIDE",
    "MR02M_AI_READ_CHUNK_REGS", "MR02M_AI_READ_RETRIES",
    "MR02M_AI_PAIR_TYPES",
    "MR_MCU_HOLD_OP_DAYS", "MR_MCU_HOLD_POWER_TEMP", "MR_INP_MCU_UPTIME_LO",
    "MR_INP_DI_CNT_BASE", "MR_INP_MCU_DIAG_START",
    "MR_RESET_REASON_LABELS", "MR02M_SYS_CONTROLS",
    "AI_RTD_CODES_3_WIRE", "AI_TC_K_CODE",
    "AI_SENSOR_LEGACY_ENUM_MIGRATION", "AI_SENSOR_SCHEMA_MODBUS",
    "_migrate_legacy_ai_sensor_code", "_ai_register_is_legacy_enum",
    "_resolve_ai_sensor_type", "_migrate_config_ai_sensor_types",
    "AI_SENSOR_TYPES", "_TEMP",
    # -- entry-owned wiring (never moves) --
    "CONFIG_PATH", "sd_notify", "POLLER_CLASSES",
    "load_config", "watchdog_thread", "signal_handler",
    "ROSTER_PATH", "write_bridge_roster", "main",
    "compose_pollers", "make_port_scheduler",
]


class TestFrozenSurface(unittest.TestCase):
    def test_every_frozen_name_present(self):
        missing = [n for n in FROZEN_SURFACE if not hasattr(bridge, n)]
        self.assertEqual([], missing,
                         "entry module lost frozen surface names: %s" % missing)

    def test_entry_keeps_time_module(self):
        # Tests patch bridge.time.monotonic — the entry must keep `import time`.
        self.assertIs(bridge.time, _time)


def copy_loop_modules(text: str) -> list[str]:
    """Module names in a deploy script's bridge copy loop (`for f in … ; do`).

    Parsed, not substring-searched: until 1.0.6.56 this suite checked
    `"bridge_x.py" in text`, so a module named only in a COMMENT passed while
    the loop never copied it (shape (a), docs/agent-rules/quality-gate-rigor.md).
    Comments are stripped first, continuation lines joined, and only the loop
    whose every token is a bridge_*.py file counts. Returns [] when there is
    no such loop — the caller fails on that (non-vacuous).
    """
    lines = [re.sub(r"(^|\s)#.*$", "", ln) for ln in text.splitlines()]
    joined = re.sub(r"\\" + "\n", " ", "\n".join(lines))
    found = []
    for m in re.finditer(r"^\s*for\s+f\s+in\s+([^;\n]*);\s*do\b", joined, re.M):
        tokens = m.group(1).split()
        if tokens and all(re.fullmatch(r"bridge_\w+\.py", t) for t in tokens):
            found.append([t[:-3] for t in tokens])
    return found[0] if len(found) == 1 else []


class TestSubmoduleImportSmoke(unittest.TestCase):
    def _found_modules(self) -> list[str]:
        return sorted(p.stem for p in BRIDGE_DIR.glob("bridge_*.py"))

    def test_every_module_travels_in_both_deploy_paths(self):
        """A bridge module absent from a deploy script is a dead board.

        `scripts/05-mqtt.sh` (installer) and `scripts/update-www-only.sh` (the
        www/bridge refresh) both copy an EXPLICIT list of bridge modules. Until
        1.0.6.31 the comment above claimed this list was kept in sync with them
        and nothing checked it: a module added here and forgotten there imports
        fine in CI and raises ImportError on the device the moment the entry
        module composes the poller table.
        """
        repo = BRIDGE_DIR.parent.parent
        for script in ("scripts/05-mqtt.sh", "scripts/update-www-only.sh"):
            text = (repo / script).read_text(encoding="utf-8", errors="replace")
            self.assertTrue(text.strip(), "%s is empty or unreadable" % script)
            copied = copy_loop_modules(text)
            self.assertTrue(copied, "%s: no single bridge_*.py copy loop "
                            "(`for f in bridge_… ; do`) found" % script)
            missing = [n for n in EXPECTED_MODULES if n not in copied]
            extra = [n for n in copied if n not in EXPECTED_MODULES]
            # assertIn would dump the whole script into the failure message.
            self.assertEqual([], missing,
                             "%s never copies: %s" % (script, ", ".join(missing)))
            self.assertEqual([], extra,
                             "%s copies undeclared: %s" % (script, ", ".join(extra)))

    def test_copy_loop_parser_is_not_comment_blind(self):
        loop = ("for f in bridge_serial.py \\\n"
                "         bridge_device.py; do\n  install x\ndone\n")
        self.assertEqual(copy_loop_modules(loop), ["bridge_serial", "bridge_device"])
        # Named only in a comment: not copied.
        self.assertNotIn("bridge_tcp",
                         copy_loop_modules("# bridge_tcp.py\n" + loop))
        # A commented-out loop is no loop (non-vacuous: the caller FAILS).
        self.assertEqual(copy_loop_modules("# " + loop.replace("\n", "\n# ")), [])
        self.assertEqual(copy_loop_modules(""), [])

    def test_split_modules_import_cleanly(self):
        found = self._found_modules()
        if not found:
            self.skipTest("monolith stage: no bridge_* modules yet")
        # Exact set: a module renamed/added/dropped must update this list
        # AND both deploy scripts.
        self.assertEqual(EXPECTED_MODULES, found)
        for name in found:
            mod = importlib.import_module(name)
            self.assertIn(name, sys.modules)
            self.assertTrue(hasattr(mod, "__doc__"))

    def test_get_port_single_pool_identity(self):
        if not (BRIDGE_DIR / "bridge_serial.py").exists():
            self.skipTest("monolith stage: no bridge_serial yet")
        bridge_serial = importlib.import_module("bridge_serial")
        # ONE port pool per process: the entry re-export and the defining
        # module must expose the very same function (and thus the same pool).
        self.assertIs(bridge.get_port, bridge_serial.get_port)


if __name__ == "__main__":
    unittest.main()
