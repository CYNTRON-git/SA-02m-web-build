"""Host-runnable tests for the SA-02m cloud agent (Phase B, frpc edition).

Covers the pure/protocol-shaped parts that must not regress:
  - frpc.toml rendering from the contract's frpc profile (two proxies + legacy);
  - the modules block passed VERBATIM from the roster cache (bus-free rule);
  - the security guarantee: NO command channel, NO WireGuard remnants.

Contract: cloud repo docs/contracts/cloud-enrollment.md (frozen 2026-07-16).
"""
import importlib.util
import json
import os
import sys

import pytest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")

spec = importlib.util.spec_from_file_location("sa02m_cloud_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
sys.modules["sa02m_cloud_agent"] = agent
spec.loader.exec_module(agent)

with open(AGENT_PATH, encoding="utf-8") as f:
    AGENT_SOURCE = f.read()


# ── Contract fixture (shape from docs/contracts/cloud-enrollment.md §1) ───────
FRPC_PROFILE = {
    "server_addr": "cloud.cyntron.ru",
    "server_port": 8890,
    "token": "test-frp-token",
    "proxies": [
        {"name": "dev-sa02m-abc", "subdomain": "sa02m-abc",
         "local_port": 80, "role": "web"},
        {"name": "dev-sa02m-abc-cfg", "subdomain": "sa02m-abc-cfg",
         "local_port": 9999, "role": "cfg"},
    ],
    "proxy_name": "dev-sa02m-abc", "subdomain": "sa02m-abc", "local_port": 9999,
}


# ── frpc.toml rendering ───────────────────────────────────────────────────────
def test_frpc_toml_emits_both_proxies():
    toml = agent.render_frpc_toml(FRPC_PROFILE)
    assert toml.count("[[proxies]]") == 2
    assert 'subdomain = "sa02m-abc"' in toml
    assert 'subdomain = "sa02m-abc-cfg"' in toml
    assert "localPort = 80" in toml
    assert "localPort = 9999" in toml


def test_frpc_toml_server_and_token():
    toml = agent.render_frpc_toml(FRPC_PROFILE)
    assert 'serverAddr = "cloud.cyntron.ru"' in toml
    assert "serverPort = 8890" in toml
    assert 'auth.token = "test-frp-token"' in toml


def test_frpc_toml_proxies_are_http_only():
    # frps NewProxy authz rejects anything but type http (contract §Isolation)
    toml = agent.render_frpc_toml(FRPC_PROFILE)
    types = [l for l in toml.splitlines() if l.startswith("type = ")]
    assert types and all(t == 'type = "http"' for t in types)


def test_frpc_toml_legacy_single_proxy_fallback():
    legacy = {k: v for k, v in FRPC_PROFILE.items() if k != "proxies"}
    toml = agent.render_frpc_toml(legacy)
    assert toml.count("[[proxies]]") == 1
    assert "localPort = 9999" in toml  # settings — the safe legacy default


def test_frpc_toml_local_ip_loopback():
    toml = agent.render_frpc_toml(FRPC_PROFILE)
    assert toml.count('localIP = "127.0.0.1"') == 2


# ── Roster → modules (bus-free, verbatim) ─────────────────────────────────────
def test_roster_modules_verbatim(tmp_path):
    ports = {
        "COM4": {"source": "bridge", "live": True,
                 "ours": [{"addr": 6, "model": "6AI6AO", "online": True}],
                 "third_party": {"total": 3, "online": 2}},
        "COM5": {"source": "scan", "live": False,
                 "ours": [{"addr": 3, "model": "DI8", "online": None}],
                 "third_party": {"total": 0, "online": None}},
    }
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"ts": 1, "ports": ports}))
    modules = agent.read_roster_modules(str(p))
    assert modules == {"ports": ports}  # verbatim — the device is the shape's home


def test_roster_missing_gives_none(tmp_path):
    assert agent.read_roster_modules(str(tmp_path / "absent.json")) is None


def test_roster_corrupt_gives_none(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text("{not json")
    assert agent.read_roster_modules(str(p)) is None


def test_roster_empty_ports_gives_none(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"ts": 1, "ports": {}}))
    assert agent.read_roster_modules(str(p)) is None


def test_agent_never_opens_serial_port():
    # Bus-free rule: the roster CACHE is the only module source.
    for needle in ("serial.Serial", "/dev/ttyS", "termios", "pyserial"):
        assert needle not in AGENT_SOURCE


# ── Security: no command channel, no WireGuard ────────────────────────────────
def test_no_command_channel_symbols():
    assert not hasattr(agent, "handle_command")
    assert "def handle_command" not in AGENT_SOURCE
    assert "commands/pending" not in AGENT_SOURCE


def test_no_remote_executable_actions():
    # The F1 backdoor's verbs must not come back in any form.
    for needle in ('["reboot"]', "restart_mplc", "restart_nginx", "get_log"):
        assert needle not in AGENT_SOURCE


def test_no_wireguard_remnants():
    # Functional remnants only: the config-migration line legitimately names
    # the legacy "wireguard" section it strips.
    for needle in ("wg-quick", "wg genkey", "/etc/wireguard",
                   "gen_wg_keypair", "write_wg_config", "wg_watchdog"):
        assert needle not in AGENT_SOURCE


def test_heartbeat_response_not_interpreted():
    # Send-only: the active loop must not read fields out of the heartbeat
    # response (beyond discarding it). Guard: no assignment from the
    # heartbeat api_post and no .get() chained on it.
    import re
    m = re.search(r"(\w+\s*=\s*)?api_post\(f?\"?\{?api_url\}?/heartbeat", AGENT_SOURCE)
    assert m, "heartbeat call not found"
    assert m.group(1) is None, "heartbeat response captured — send-only violated"


# ── Identity / config ─────────────────────────────────────────────────────────
def test_device_id_prefers_config():
    cfg = agent.load_config("/nonexistent")
    cfg["cloud"]["device_id"] = "sa02m-165541531f58b760"
    assert agent.get_device_id(cfg) == "sa02m-165541531f58b760"


def test_device_id_charset():
    import re
    cfg = agent.load_config("/nonexistent")
    did = agent.get_device_id(cfg)
    assert re.match(r"^[A-Za-z0-9._-]{1,64}$", did)  # contract charset


def test_default_config_has_no_wireguard_section():
    cfg = agent.load_config("/nonexistent")
    assert not cfg.has_section("wireguard")
    assert cfg["cloud"]["api_url"].startswith("https://")


def test_legacy_config_migrated(tmp_path):
    # A WireGuard-era agent.conf loses its legacy keys on load, so an
    # enrollment save never re-emits them.
    p = tmp_path / "agent.conf"
    p.write_text(
        "[cloud]\napi_url = https://cloud.cyntron.ru/api/v1\n"
        "device_token = OLDTOKEN\nmetrics_interval = 30\n"
        "[wireguard]\nenabled = true\ninterface = wg-cloud\n"
        "[device]\nserial = abc\n")
    cfg = agent.load_config(str(p))
    assert not cfg.has_section("wireguard")
    assert not cfg.has_option("cloud", "device_token")
    assert cfg["cloud"].getboolean("enrolled") is False
    assert cfg["device"]["serial"] == "abc"
