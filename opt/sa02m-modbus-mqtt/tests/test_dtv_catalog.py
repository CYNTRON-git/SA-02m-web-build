"""Frozen DTV metadata and import-only configuration capability."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_entry_surface import bridge


class Catalog(unittest.TestCase):
    def test_frozen_maps_and_defaults(self):
        poller = bridge.DTVPoller
        payload = {"DTV_REGS": poller.DTV_REGS, "DTV_COILS": poller.DTV_COILS}
        self.assertEqual(hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest(),
                         "e3a124d4905102ffd67664b2a514bcbc2ac4c426de3fd651fd54f8460444a274")
        # The original constructor's effective defaults, without opening any port.
        from unittest.mock import Mock
        device = poller({"id": "dtv-fixture", "port": "/dev/COM1", "address": 1}, Mock())
        self.assertEqual([getattr(device, "_" + key) for key in
                          ("poll_sensors_s", "poll_presence_s", "poll_diag_s", "poll_uptime_s")],
                         [10, 2, 60, 5])

    def test_pure_capability(self):
        import bridge_dtv_catalog as catalog
        result = catalog.configuration_capability()
        self.assertEqual(result["sensor_keys"], [v[0] for reg, v in bridge.DTVPoller.DTV_REGS.items()
                                                if 1 <= reg <= 30])
        self.assertEqual(len(set(result["sensor_keys"])), 28)
        self.assertEqual(result["always_polled_keys"], ["buzzer", "leds"])
        self.assertEqual(result["poll_defaults"], dict(poll_sensors_s=10, poll_presence_s=2,
                                                     poll_diag_s=60, poll_uptime_s=5))
        original = json.loads(json.dumps(result))
        result["sensor_keys"].clear()
        result["always_polled_keys"].clear()
        result["poll_defaults"].clear()
        self.assertEqual(catalog.configuration_capability(), original)
        subprocess.run([sys.executable, "-I", "-S", "-B", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import bridge_dtv_catalog; "
                        "assert not any(k.split('.')[0] in ('serial', 'paho', 'yaml') for k in sys.modules)",
                        str(Path(catalog.__file__).parent)], check=True)
