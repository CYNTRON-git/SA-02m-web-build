"""Unittest gate for the device-side frpc render hardening (O1 + O3).

unittest-style on purpose (no pytest) so the `py-unit-cloud` quality row can
discover it via `unittest discover -p "test_render*.py"` — pytest is not a
device/CI dependency, and the sibling test_agent.py (pytest-style) is excluded
by that pattern.

Asserts the device-side local-port allow-list (O1) and the pinned transport
TLS (O3):
  - a cloud proxy whose local_port is not in {80, 9999} is dropped (e.g. :22
    SSH, :1883 MQTT) while :80 web + :9999 cfg are kept;
  - the legacy single-proxy fallback goes through the same allow-list;
  - all-dropped renders a config with zero [[proxies]] (fail closed);
  - transport.tls.enable = true is present.

Contract: docs/contracts/cloud-enrollment.md (device-side mirror);
cloud repo docs/contracts/cloud-enrollment.md (authority).
"""
import importlib.util
import os
import unittest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")

_spec = importlib.util.spec_from_file_location("sa02m_cloud_agent_render", AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)


def _profile(*local_ports):
    """A claim/enroll frpc profile carrying one proxy per given local_port."""
    return {
        "server_addr": "cloud.cyntron.ru",
        "server_port": 8890,
        "token": "test-frp-token",
        "proxies": [
            {"name": "dev-p%d" % lp, "subdomain": "sa02m-p%d" % lp,
             "local_port": lp, "role": "x"}
            for lp in local_ports
        ],
    }


class AllowListTest(unittest.TestCase):
    def test_allow_list_constant(self):
        self.assertEqual(agent.ALLOWED_LOCAL_PORTS, frozenset({80, 9999}))

    def test_allowed_ports_kept(self):
        toml = agent.render_frpc_toml(_profile(80, 9999))
        self.assertEqual(toml.count("[[proxies]]"), 2)
        self.assertIn("localPort = 80", toml)
        self.assertIn("localPort = 9999", toml)

    def test_ssh_port_dropped_others_kept(self):
        # A compromised cloud pushes :22 (SSH) alongside the legit :80 (A5).
        toml = agent.render_frpc_toml(_profile(22, 80))
        self.assertEqual(toml.count("[[proxies]]"), 1)
        self.assertIn("localPort = 80", toml)
        self.assertNotIn("localPort = 22", toml)

    def test_mqtt_port_dropped(self):
        toml = agent.render_frpc_toml(_profile(1883))
        self.assertEqual(toml.count("[[proxies]]"), 0)

    def test_all_disallowed_fails_closed(self):
        toml = agent.render_frpc_toml(_profile(22, 1883))
        self.assertEqual(toml.count("[[proxies]]"), 0)
        # A valid top section still renders — no tunnel, not a malicious one.
        self.assertIn('serverAddr = "cloud.cyntron.ru"', toml)

    def test_legacy_fallback_disallowed_dropped(self):
        legacy = {
            "server_addr": "cloud.cyntron.ru", "server_port": 8890, "token": "t",
            "proxy_name": "dev-x", "subdomain": "sa02m-x", "local_port": 22,
        }
        toml = agent.render_frpc_toml(legacy)
        self.assertEqual(toml.count("[[proxies]]"), 0)

    def test_legacy_fallback_allowed_kept(self):
        legacy = {
            "server_addr": "cloud.cyntron.ru", "server_port": 8890, "token": "t",
            "proxy_name": "dev-x", "subdomain": "sa02m-x", "local_port": 9999,
        }
        toml = agent.render_frpc_toml(legacy)
        self.assertEqual(toml.count("[[proxies]]"), 1)
        self.assertIn("localPort = 9999", toml)

    def test_transport_tls_pinned(self):
        toml = agent.render_frpc_toml(_profile(80))
        self.assertIn("transport.tls.enable = true", toml)

    def test_kept_proxies_are_http(self):
        toml = agent.render_frpc_toml(_profile(80, 9999))
        types = [l for l in toml.splitlines() if l.startswith("type = ")]
        self.assertTrue(types)
        self.assertTrue(all(t == 'type = "http"' for t in types))


if __name__ == "__main__":
    unittest.main()
