#!/usr/bin/env python3
"""
SA-02m Cloud Agent
Collects device metrics, maintains WireGuard tunnel, handles cloud commands.

Usage: python3 sa02m-cloud-agent.py [--config /etc/sa02m-cloud/agent.conf]
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

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/sa02m-cloud-agent.log", mode="a"),
    ],
)
log = logging.getLogger("sa02m-cloud")

DEFAULT_CONFIG = "/etc/sa02m-cloud/agent.conf"

# ── Config ────────────────────────────────────────────────────────────────────
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "cloud": {
            "api_url":       "https://cloud.cyntron.ru/api/v1",
            "device_token":  "",
            "metrics_interval": "30",
            "heartbeat_interval": "10",
        },
        "wireguard": {
            "enabled":   "true",
            "interface": "wg-cloud",
            "config":    "/etc/wireguard/wg-cloud.conf",
        },
        "device": {
            "serial": "",
            "web_port": "9999",
        },
    })
    if os.path.exists(path):
        cfg.read(path)
    return cfg


# ── System metrics ────────────────────────────────────────────────────────────
def _read_first(path: str, default: str = "0") -> str:
    try:
        with open(path) as f:
            return f.read().strip().split()[0]
    except Exception:
        return default


def collect_metrics() -> dict:
    metrics: dict = {}

    # CPU load
    try:
        load = os.getloadavg()
        metrics["load_1"] = round(load[0], 2)
        metrics["load_5"] = round(load[1], 2)
        metrics["load_15"] = round(load[2], 2)
        with open("/proc/stat") as f:
            line = f.readline()
        fields = list(map(int, line.split()[1:]))
        idle = fields[3]
        total = sum(fields)
        metrics["cpu_pct"] = round(100.0 * (1 - idle / total), 1)
    except Exception:
        metrics.update({"load_1": 0, "load_5": 0, "load_15": 0, "cpu_pct": 0})

    # RAM
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.strip().split()[0])
        total_kb = mem.get("MemTotal", 1)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        metrics["ram_total_mb"] = total_kb // 1024
        metrics["ram_used_mb"] = used_kb // 1024
        metrics["ram_pct"] = round(100.0 * used_kb / max(total_kb, 1), 1)
    except Exception:
        metrics.update({"ram_total_mb": 0, "ram_used_mb": 0, "ram_pct": 0})

    # Disk /
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        metrics["disk_total_mb"] = total // (1024 * 1024)
        metrics["disk_used_mb"] = used // (1024 * 1024)
        metrics["disk_pct"] = round(100.0 * used / max(total, 1), 1)
    except Exception:
        metrics.update({"disk_total_mb": 0, "disk_used_mb": 0, "disk_pct": 0})

    # Uptime
    try:
        metrics["uptime_s"] = int(float(_read_first("/proc/uptime")))
    except Exception:
        metrics["uptime_s"] = 0

    # CPU temperature (sunxi A40i)
    for tpath in [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ]:
        if os.path.exists(tpath):
            try:
                metrics["temp_c"] = round(int(_read_first(tpath)) / 1000.0, 1)
            except Exception:
                metrics["temp_c"] = 0.0
            break
    else:
        metrics["temp_c"] = 0.0

    # Network: eth0
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if "eth0:" in line:
                    parts = line.split()
                    metrics["eth0_rx_b"] = int(parts[1])
                    metrics["eth0_tx_b"] = int(parts[9])
                    break
    except Exception:
        metrics.update({"eth0_rx_b": 0, "eth0_tx_b": 0})

    # eth0 IP
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "eth0"], stderr=subprocess.DEVNULL, timeout=3
        ).decode()
        for part in out.split():
            if "." in part and "/" in part:
                metrics["eth0_ip"] = part.split("/")[0]
                break
        else:
            metrics["eth0_ip"] = ""
    except Exception:
        metrics["eth0_ip"] = ""

    # mplc service status
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "mplc4"],
            capture_output=True, text=True, timeout=3
        )
        metrics["mplc_status"] = r.stdout.strip()
    except Exception:
        metrics["mplc_status"] = "unknown"

    # WireGuard VPN IP
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "wg-cloud"],
            stderr=subprocess.DEVNULL, timeout=3
        ).decode()
        for part in out.split():
            if "." in part and "/" in part:
                metrics["vpn_ip"] = part.split("/")[0]
                break
        else:
            metrics["vpn_ip"] = ""
    except Exception:
        metrics["vpn_ip"] = ""

    metrics["timestamp"] = int(time.time())
    metrics["hostname"] = platform.node()
    return metrics


# ── WireGuard watchdog ────────────────────────────────────────────────────────
def wg_watchdog(iface: str, config: str):
    """Ensure WireGuard interface is up; restart if handshake is stale."""
    try:
        out = subprocess.check_output(
            ["wg", "show", iface, "latest-handshakes"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        if out:
            _, ts = out.split()
            age = int(time.time()) - int(ts)
            if age > 180:  # no handshake in 3 min → restart
                log.warning("WireGuard handshake stale (%ds), restarting %s", age, iface)
                subprocess.run(["wg-quick", "down", config], timeout=10, check=False)
                time.sleep(1)
                subprocess.run(["wg-quick", "up", config], timeout=10, check=False)
        else:
            # No peers yet, just ensure it's up
            subprocess.run(["wg-quick", "up", config], timeout=10, check=False)
    except subprocess.CalledProcessError:
        # Interface not found → bring it up
        log.info("WireGuard %s not up, starting...", iface)
        subprocess.run(["wg-quick", "up", config], timeout=10, check=False)
    except Exception as e:
        log.debug("WireGuard watchdog error: %s", e)


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def api_post(url: str, token: str, payload: dict, timeout: int = 10) -> dict | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "sa02m-cloud-agent/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("API POST %s → %d: %s", url, e.code, e.read()[:200])
    except Exception as e:
        log.debug("API POST error: %s", e)
    return None


def api_get(url: str, token: str, timeout: int = 10) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "sa02m-cloud-agent/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.debug("API GET error: %s", e)
    return None


# ── Command handler ───────────────────────────────────────────────────────────
def handle_command(cmd: dict) -> str:
    action = cmd.get("action", "")
    log.info("Command received: %s", action)

    if action == "reboot":
        threading.Timer(2.0, lambda: subprocess.run(["reboot"])).start()
        return "ok: rebooting in 2s"
    elif action == "restart_mplc":
        r = subprocess.run(
            ["systemctl", "restart", "mplc4"],
            capture_output=True, text=True, timeout=10
        )
        return f"ok: {r.returncode}"
    elif action == "restart_nginx":
        r = subprocess.run(
            ["systemctl", "restart", "nginx"],
            capture_output=True, text=True, timeout=10
        )
        return f"ok: {r.returncode}"
    elif action == "get_log":
        logfile = cmd.get("file", "/var/log/sa02m_install.log")
        safe_logs = ["/var/log/", "/var/log/sa02m"]
        if not any(logfile.startswith(p) for p in safe_logs):
            return "error: path not allowed"
        try:
            with open(logfile, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                return f.read().decode(errors="replace")
        except Exception as e:
            return f"error: {e}"
    elif action == "ping":
        return "pong"
    else:
        return f"error: unknown action {action!r}"


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SA-02m Cloud Agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    cfg = load_config(args.config)
    api_url   = cfg["cloud"]["api_url"].rstrip("/")
    token     = cfg["cloud"]["device_token"]
    m_interval = int(cfg["cloud"]["metrics_interval"])
    h_interval = int(cfg["cloud"]["heartbeat_interval"])
    wg_enabled = cfg["wireguard"].getboolean("enabled")
    wg_iface   = cfg["wireguard"]["interface"]
    wg_config  = cfg["wireguard"]["config"]

    if not token:
        log.error("device_token not set in %s — run sa02m-cloud-activate first", args.config)
        sys.exit(1)

    log.info("SA-02m Cloud Agent starting. API: %s", api_url)

    last_metrics = 0.0
    last_heartbeat = 0.0
    last_wg_check = 0.0

    while True:
        now = time.time()

        # WireGuard watchdog every 60s
        if wg_enabled and os.path.exists(wg_config) and now - last_wg_check > 60:
            wg_watchdog(wg_iface, wg_config)
            last_wg_check = now

        # Heartbeat (online status)
        if now - last_heartbeat > h_interval:
            api_post(f"{api_url}/devices/heartbeat", token, {"ts": int(now)})
            last_heartbeat = now

        # Metrics
        if now - last_metrics > m_interval:
            metrics = collect_metrics()
            result = api_post(f"{api_url}/devices/metrics", token, metrics)
            if result:
                log.debug("Metrics sent OK")
            last_metrics = now

        # Poll commands
        cmds = api_get(f"{api_url}/devices/commands/pending", token, timeout=8)
        if cmds and isinstance(cmds, list):
            for cmd in cmds:
                cmd_id = cmd.get("id")
                result = handle_command(cmd)
                api_post(f"{api_url}/devices/commands/{cmd_id}/result", token, {"result": result})

        time.sleep(5)


if __name__ == "__main__":
    main()
