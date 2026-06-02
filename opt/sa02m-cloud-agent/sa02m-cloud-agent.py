#!/usr/bin/env python3
"""
SA-02m Cloud Agent
Collects device metrics, maintains WireGuard tunnel, handles cloud commands.

Activation modes (no SSH needed):
  1. Factory: write token to /etc/sa02m-cloud/activation_token → auto-activates
  2. Web UI:  POST to /cgi-bin/cloud.cgi on device web UI → writes token → auto-activates
  3. Manual:  sa02m-cloud-activate --token XXX (fallback for technicians)
"""
import argparse
import configparser
import json
import logging
import os
import platform
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/sa02m-cloud-agent.log", mode="a"),
    ],
)
log = logging.getLogger("sa02m-cloud")

DEFAULT_CONFIG       = "/etc/sa02m-cloud/agent.conf"
ACTIVATION_TOKEN_FILE = "/etc/sa02m-cloud/activation_token"  # one-time token
DEFAULT_WG_CONF      = "/etc/wireguard/wg-cloud.conf"
STATUS_FILE          = "/run/sa02m-cloud-status.json"        # for web UI CGI


def _write_status(state: str, **kw):
    """Write machine-readable status for CGI/web UI."""
    payload = {"state": state, "ts": int(time.time()), **kw}
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────────────────
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "cloud": {
            "api_url":            "https://cloud.cyntron.ru/api/v1",
            "device_token":       "",
            "server_host":        "cloud.cyntron.ru",
            "metrics_interval":   "30",
            "heartbeat_interval": "10",
        },
        "wireguard": {
            "enabled":   "true",
            "interface": "wg-cloud",
            "config":    DEFAULT_WG_CONF,
        },
        "device": {
            "serial":   "",
            "web_port": "9999",
        },
    })
    if os.path.exists(path):
        cfg.read(path)
    return cfg


def save_config(path: str, cfg: configparser.ConfigParser):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        cfg.write(f)
    os.chmod(path, 0o640)


# ── Bootstrap / auto-activation ──────────────────────────────────────────────
def get_serial() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "Serial" in line:
                    return line.split(":")[1].strip()
    except Exception:
        pass
    try:
        with open("/etc/machine-id") as f:
            return f.read().strip()[:16]
    except Exception:
        pass
    return platform.node()


def gen_wg_keypair() -> tuple[str, str]:
    priv = subprocess.check_output(["wg", "genkey"], timeout=5).decode().strip()
    pub  = subprocess.check_output(["wg", "pubkey"], input=priv.encode(), timeout=5).decode().strip()
    return priv, pub


def write_wg_config(path: str, priv_key: str, vpn_ip: str,
                    server_pubkey: str, server_endpoint: str,
                    server_vpn_ip: str = "10.100.0.1"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(
            f"[Interface]\n"
            f"PrivateKey = {priv_key}\n"
            f"Address = {vpn_ip}/24\n\n"
            f"[Peer]\n"
            f"PublicKey = {server_pubkey}\n"
            f"Endpoint = {server_endpoint}:51820\n"
            f"AllowedIPs = {server_vpn_ip}/32\n"
            f"PersistentKeepalive = 25\n"
        )
    os.chmod(path, 0o600)


def api_post(url: str, token: str, payload: dict, timeout: int = 15) -> dict | None:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "sa02m-cloud-agent/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("POST %s → HTTP %d: %s", url, e.code, e.read()[:200])
    except Exception as e:
        log.debug("POST %s error: %s", url, e)
    return None


def api_get(url: str, token: str, timeout: int = 10) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "sa02m-cloud-agent/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.debug("GET %s error: %s", url, e)
    return None


def try_activate(activation_token: str, cfg: configparser.ConfigParser,
                 config_path: str) -> bool:
    """
    Use activation_token to register device with cloud.
    Returns True and updates cfg on success.
    """
    server_host = cfg["cloud"]["server_host"]
    api_url     = cfg["cloud"]["api_url"].rstrip("/")
    serial      = get_serial()

    log.info("Activating device %s with cloud %s...", serial, server_host)
    _write_status("activating", serial=serial, server=server_host)

    try:
        priv_key, pub_key = gen_wg_keypair()
    except Exception as e:
        log.error("WireGuard keygen failed: %s", e)
        return False

    resp = api_post(f"{api_url}/devices/activate", "", {
        "activation_token": activation_token,
        "serial":           serial,
        "wg_pubkey":        pub_key,
        "hostname":         platform.node(),
    })

    if not resp:
        log.warning("Activation failed (no response from cloud)")
        return False

    device_token    = resp.get("device_token", "")
    vpn_ip          = resp.get("vpn_ip", "")
    server_pubkey   = resp.get("server_pubkey", "")
    server_endpoint = resp.get("server_endpoint", server_host)
    server_vpn_ip   = resp.get("server_vpn_ip", "10.100.0.1")

    if not device_token or not vpn_ip or not server_pubkey:
        log.error("Activation response missing required fields: %s", resp)
        return False

    # Write WireGuard config
    wg_config = cfg["wireguard"]["config"]
    write_wg_config(wg_config, priv_key, vpn_ip, server_pubkey,
                    server_endpoint, server_vpn_ip)

    # Update and save agent config
    cfg["cloud"]["device_token"] = device_token
    cfg["device"]["serial"]      = serial
    save_config(config_path, cfg)

    # Remove one-time activation token
    try:
        os.remove(ACTIVATION_TOKEN_FILE)
    except Exception:
        pass

    # Bring up WireGuard
    try:
        subprocess.run(["wg-quick", "up", cfg["wireguard"]["interface"]],
                       timeout=15, check=False)
    except Exception as e:
        log.warning("wg-quick up failed: %s", e)

    log.info("Activation successful! VPN IP: %s", vpn_ip)
    _write_status("active", vpn_ip=vpn_ip, serial=serial)
    return True


# ── System metrics ────────────────────────────────────────────────────────────
def _read_first(path: str, default: str = "0") -> str:
    try:
        with open(path) as f:
            return f.read().strip().split()[0]
    except Exception:
        return default


def collect_metrics() -> dict:
    m: dict = {}

    # CPU
    try:
        load = os.getloadavg()
        m["load_1"], m["load_5"], m["load_15"] = (round(x, 2) for x in load)
        with open("/proc/stat") as f:
            fields = list(map(int, f.readline().split()[1:]))
        idle = fields[3]; total = sum(fields)
        m["cpu_pct"] = round(100.0 * (1 - idle / total), 1)
    except Exception:
        m.update({"load_1": 0, "load_5": 0, "load_15": 0, "cpu_pct": 0})

    # RAM
    try:
        with open("/proc/meminfo") as f:
            mem = {k.strip(): int(v.strip().split()[0])
                   for line in f for k, v in [line.split(":", 1)]}
        total_kb = mem.get("MemTotal", 1)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb  = total_kb - avail_kb
        m["ram_total_mb"] = total_kb // 1024
        m["ram_used_mb"]  = used_kb  // 1024
        m["ram_pct"]      = round(100.0 * used_kb / max(total_kb, 1), 1)
    except Exception:
        m.update({"ram_total_mb": 0, "ram_used_mb": 0, "ram_pct": 0})

    # Disk
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize; free = st.f_bfree * st.f_frsize
        used  = total - free
        m["disk_total_mb"] = total // (1024 * 1024)
        m["disk_used_mb"]  = used  // (1024 * 1024)
        m["disk_pct"]      = round(100.0 * used / max(total, 1), 1)
    except Exception:
        m.update({"disk_total_mb": 0, "disk_used_mb": 0, "disk_pct": 0})

    # Uptime & temp
    m["uptime_s"] = int(float(_read_first("/proc/uptime")))
    for tp in ["/sys/class/thermal/thermal_zone0/temp",
               "/sys/devices/virtual/thermal/thermal_zone0/temp"]:
        if os.path.exists(tp):
            m["temp_c"] = round(int(_read_first(tp)) / 1000.0, 1)
            break
    else:
        m["temp_c"] = 0.0

    # Network
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if "end0:" in line:
                    p = line.split()
                    m["eth0_rx_b"] = int(p[1]); m["eth0_tx_b"] = int(p[9])
                    break
    except Exception:
        m.update({"eth0_rx_b": 0, "eth0_tx_b": 0})

    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "end0"], stderr=subprocess.DEVNULL, timeout=3
        ).decode()
        m["eth0_ip"] = next((p.split("/")[0] for p in out.split() if "." in p and "/" in p), "")
    except Exception:
        m["eth0_ip"] = ""

    # VPN IP
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "wg-cloud"],
            stderr=subprocess.DEVNULL, timeout=3
        ).decode()
        m["vpn_ip"] = next((p.split("/")[0] for p in out.split() if "." in p and "/" in p), "")
    except Exception:
        m["vpn_ip"] = ""

    # Services
    try:
        r = subprocess.run(["systemctl", "is-active", "mplc4"],
                           capture_output=True, text=True, timeout=3)
        m["mplc_status"] = r.stdout.strip()
    except Exception:
        m["mplc_status"] = "unknown"

    m["timestamp"] = int(time.time())
    m["hostname"]  = platform.node()
    return m


# ── WireGuard watchdog ────────────────────────────────────────────────────────
def wg_watchdog(iface: str, config: str):
    try:
        out = subprocess.check_output(
            ["wg", "show", iface, "latest-handshakes"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        if out:
            _, ts_str = out.split()
            age = int(time.time()) - int(ts_str)
            if age > 180:
                log.warning("WireGuard handshake stale (%ds), restarting", age)
                subprocess.run(["wg-quick", "down", config], timeout=10, check=False)
                time.sleep(1)
                subprocess.run(["wg-quick", "up", config], timeout=10, check=False)
        else:
            subprocess.run(["wg-quick", "up", config], timeout=10, check=False)
    except subprocess.CalledProcessError:
        subprocess.run(["wg-quick", "up", config], timeout=10, check=False)
    except Exception as e:
        log.debug("WireGuard watchdog: %s", e)


# ── Command handler ───────────────────────────────────────────────────────────
def handle_command(cmd: dict) -> str:
    action = cmd.get("action", "")
    log.info("Command: %s", action)
    if action == "reboot":
        threading.Timer(2.0, lambda: subprocess.run(["reboot"])).start()
        return "ok: rebooting"
    if action == "restart_mplc":
        r = subprocess.run(["systemctl", "restart", "mplc4"],
                           capture_output=True, text=True, timeout=10)
        return f"ok: {r.returncode}"
    if action == "restart_nginx":
        r = subprocess.run(["systemctl", "restart", "nginx"],
                           capture_output=True, text=True, timeout=10)
        return f"ok: {r.returncode}"
    if action == "get_log":
        logfile = cmd.get("file", "/var/log/sa02m_install.log")
        if not logfile.startswith("/var/log/"):
            return "error: path not allowed"
        try:
            with open(logfile, "rb") as f:
                f.seek(max(0, f.seek(0, 2) or 0 - 8192))
                return f.read().decode(errors="replace")
        except Exception as e:
            return f"error: {e}"
    if action == "ping":
        return "pong"
    return f"error: unknown action {action!r}"


# ── Bootstrap loop (standby until activated) ─────────────────────────────────
def bootstrap_loop(cfg: configparser.ConfigParser, config_path: str) -> bool:
    """
    Wait for activation. Checks every 15s:
      1. /etc/sa02m-cloud/activation_token file (factory or web UI wrote it)
    Returns True when activated successfully.
    """
    log.info("Standby: waiting for activation token at %s", ACTIVATION_TOKEN_FILE)
    log.info("Activate via web UI: http://device-ip:9999  → Cloud tab")
    log.info("Or factory: write token to %s", ACTIVATION_TOKEN_FILE)
    _write_status("standby")

    while True:
        # Check token file
        if os.path.exists(ACTIVATION_TOKEN_FILE):
            try:
                with open(ACTIVATION_TOKEN_FILE) as f:
                    token = f.read().strip()
                if token:
                    success = try_activate(token, cfg, config_path)
                    if success:
                        return True
                    else:
                        log.warning("Activation failed, will retry in 60s")
                        _write_status("activation_failed")
                        time.sleep(60)
                        continue
            except Exception as e:
                log.error("Error reading activation token: %s", e)

        time.sleep(15)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SA-02m Cloud Agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    os.makedirs("/etc/sa02m-cloud", exist_ok=True)

    cfg = load_config(args.config)
    token = cfg["cloud"]["device_token"]

    # ── Not activated yet → bootstrap mode ───────────────────────────────────
    if not token:
        activated = bootstrap_loop(cfg, args.config)
        if not activated:
            sys.exit(1)
        # Reload config after activation
        cfg   = load_config(args.config)
        token = cfg["cloud"]["device_token"]

    # ── Active mode ───────────────────────────────────────────────────────────
    api_url    = cfg["cloud"]["api_url"].rstrip("/")
    m_interval = int(cfg["cloud"]["metrics_interval"])
    h_interval = int(cfg["cloud"]["heartbeat_interval"])
    wg_enabled = cfg["wireguard"].getboolean("enabled")
    wg_iface   = cfg["wireguard"]["interface"]
    wg_config  = cfg["wireguard"]["config"]

    log.info("SA-02m Cloud Agent active. API: %s", api_url)
    _write_status("active", vpn_ip=collect_metrics().get("vpn_ip", ""))

    last_metrics   = 0.0
    last_heartbeat = 0.0
    last_wg_check  = 0.0

    while True:
        now = time.time()

        if wg_enabled and os.path.exists(wg_config) and now - last_wg_check > 60:
            wg_watchdog(wg_iface, wg_config)
            last_wg_check = now

        if now - last_heartbeat > h_interval:
            api_post(f"{api_url}/devices/heartbeat", token, {"ts": int(now)})
            last_heartbeat = now

        if now - last_metrics > m_interval:
            metrics = collect_metrics()
            api_post(f"{api_url}/devices/metrics", token, metrics)
            _write_status("active", vpn_ip=metrics.get("vpn_ip", ""),
                          eth0_ip=metrics.get("eth0_ip", ""),
                          last_metrics=int(now))
            last_metrics = now

        cmds = api_get(f"{api_url}/devices/commands/pending", token, timeout=8)
        if cmds and isinstance(cmds, list):
            for cmd in cmds:
                cmd_id = cmd.get("id")
                result = handle_command(cmd)
                api_post(f"{api_url}/devices/commands/{cmd_id}/result", token,
                         {"result": result})

        time.sleep(5)


if __name__ == "__main__":
    main()
