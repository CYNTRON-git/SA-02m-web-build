#!/usr/bin/env python3
"""
SA-02m Cloud Agent — frpc reverse-tunnel edition.

Pairs the device with the cloud (cloud.cyntron.ru), maintains the frpc
reverse tunnel, and POSTs send-only telemetry heartbeats.

Contract: cloud repo docs/contracts/cloud-enrollment.md (frozen).
SECURITY: there is deliberately NO command channel — the cloud can never
make this device execute anything. Heartbeat responses are ignored except
for the "ok" field. (The former handle_command() root channel — threat
model F1 — was removed in Phase B together with WireGuard.)

Activation modes (no SSH needed):
  1. Claim code (primary): web UI Cloud tab → "connect" → the agent requests
     a pairing code, shows it via /run/sa02m-cloud-status.json, and polls
     until the user attaches the code in the cloud UI.
  2. Enroll token (fallback for installers): write the token to
     /etc/sa02m-cloud/activation_token (web UI POST or sa02m-cloud-activate).
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
import urllib.request
import urllib.error

_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.FileHandler("/var/log/sa02m-cloud-agent.log", mode="a"))
except OSError:
    pass  # read-only rootfs / test host — stdout (journal) still has it
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("sa02m-cloud")

DEFAULT_CONFIG        = "/etc/sa02m-cloud/agent.conf"
ACTIVATION_TOKEN_FILE = "/etc/sa02m-cloud/activation_token"   # enroll-token fallback
PAIR_REQUEST_FILE     = "/etc/sa02m-cloud/pair_request"       # claim-code trigger
FRPC_CONFIG           = "/etc/sa02m-cloud/frpc.toml"
FRPC_BINARY           = "/usr/local/bin/frpc"
FRPC_UNIT             = "sa02m-cloud-frpc"
STATUS_FILE           = "/run/sa02m-cloud-status.json"        # for web UI CGI
ROSTER_FILE           = "/run/sa02m-rs485-roster.json"        # bus-free module cache
HW_VARIANT            = "sa02m-1eth"
VERSION_FILE          = "/var/www/network_config/VERSION"

STANDBY_POLL_S  = 5
WATCHDOG_S      = 60


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
            "server_host":        "cloud.cyntron.ru",
            "enrolled":           "false",
            "device_id":          "",
            "heartbeat_interval": "30",
        },
        "device": {
            "serial":   "",
            "web_port": "9999",
        },
    })
    if os.path.exists(path):
        cfg.read(path)
    # WireGuard-era leftovers must not survive a migrated config file
    cfg.remove_section("wireguard")
    cfg.remove_option("cloud", "device_token")
    cfg.remove_option("cloud", "metrics_interval")
    return cfg


def save_config(path: str, cfg: configparser.ConfigParser):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        cfg.write(f)
    os.chmod(path, 0o640)


# ── Identity ──────────────────────────────────────────────────────────────────
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


def get_device_id(cfg: configparser.ConfigParser) -> str:
    """Stable device id, bench convention: sa02m-<serial> (contract charset
    ^[A-Za-z0-9._-]{1,64}$; the serial sources satisfy it)."""
    did = cfg["cloud"].get("device_id", "")
    if did:
        return did
    return "sa02m-" + get_serial().lower()


def get_fw_version() -> str:
    try:
        with open(VERSION_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return "unknown"


# ── HTTP (stdlib only) ────────────────────────────────────────────────────────
def api_post(url: str, payload: dict, timeout: int = 15):
    """POST JSON; returns (http_status, parsed_body|None). Network failure
    returns (0, None)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "sa02m-cloud-agent/2.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        log.warning("POST %s -> HTTP %d: %s", url, e.code, body)
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, None
    except Exception as e:
        log.debug("POST %s error: %s", url, e)
        return 0, None


# ── frpc profile → config ─────────────────────────────────────────────────────
def render_frpc_toml(frpc: dict) -> str:
    """Render frpc.toml from the contract's frpc profile. Emits EVERY proxy the
    cloud handed out (two: <sub> → :80 web SCADA, <sub>-cfg → :9999 settings),
    type http — the only shape the frps NewProxy authz accepts."""
    server_addr = frpc["server_addr"]
    server_port = int(frpc["server_port"])
    token       = frpc["token"]
    proxies     = frpc.get("proxies") or []
    if not proxies:
        # Legacy single-proxy fallback fields (pre-Phase-B contract)
        proxies = [{
            "name":       frpc["proxy_name"],
            "subdomain":  frpc["subdomain"],
            "local_port": frpc["local_port"],
        }]
    lines = [
        'serverAddr = "%s"' % server_addr,
        "serverPort = %d" % server_port,
        'auth.token = "%s"' % token,
        "",
    ]
    for p in proxies:
        lines += [
            "[[proxies]]",
            'name = "%s"' % p["name"],
            'type = "http"',
            'subdomain = "%s"' % p["subdomain"],
            "localIP = \"127.0.0.1\"",
            "localPort = %d" % int(p["local_port"]),
            "",
        ]
    return "\n".join(lines)


def write_frpc_config(frpc: dict, path: str = FRPC_CONFIG):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = render_frpc_toml(frpc)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o600)  # carries the frp token
    log.info("frpc config written: %s (%d proxies)", path, content.count("[[proxies]]"))


def _systemctl(*args, timeout: int = 15) -> int:
    try:
        r = subprocess.run(["systemctl", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode
    except Exception as e:
        log.warning("systemctl %s failed: %s", " ".join(args), e)
        return 1


def ensure_frpc_running() -> str:
    """frpc watchdog (replaces the WireGuard one): make sure the tunnel unit
    runs whenever a config exists. Returns a state string for the status file."""
    if not os.path.exists(FRPC_CONFIG):
        return "no_config"
    if not os.path.exists(FRPC_BINARY):
        return "frpc_missing"
    if _systemctl("is-active", "--quiet", FRPC_UNIT) == 0:
        return "running"
    log.info("frpc unit not active — (re)starting %s", FRPC_UNIT)
    _systemctl("enable", FRPC_UNIT)
    _systemctl("restart", FRPC_UNIT)
    if _systemctl("is-active", "--quiet", FRPC_UNIT) == 0:
        return "running"
    return "failed"


# ── Telemetry (heartbeat filler) ──────────────────────────────────────────────
def _read_first(path: str, default: str = "0") -> str:
    try:
        with open(path) as f:
            return f.read().strip().split()[0]
    except Exception:
        return default


def _fs_stats(path: str):
    v = os.statvfs(path)
    tot = v.f_blocks * v.f_frsize
    free = v.f_bavail * v.f_frsize
    if not tot:
        return None
    return {"total_gb": round(tot / 1e9, 1),
            "used_pct": round(100.0 * (1 - free / tot), 1)}


def collect_storage() -> dict:
    """eMMC (root fs) + removable drives, per the contract: emmc = the mmcblk
    with boot0/boot1 siblings (reported as the root fs), other mmcblk = sd,
    a /sys device path containing /usb = usb. Unmounted → dev only."""
    st = {}
    try:
        s = _fs_stats("/")
        if s:
            st["emmc"] = s
    except Exception:
        pass
    mounts = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if p[0].startswith("/dev/"):
                    mounts[p[0][5:]] = p[1]
    except Exception:
        pass
    usb, sd = [], []
    try:
        blocks = os.listdir("/sys/block")
    except Exception:
        blocks = []
    for b in blocks:
        if b.startswith(("loop", "ram", "zram")) or "boot" in b:
            continue
        try:
            link = os.readlink("/sys/block/" + b)
        except OSError:
            link = ""
        is_usb = "/usb" in link
        is_mmc = b.startswith("mmcblk")
        if not is_usb and not is_mmc:
            continue
        if is_mmc and os.path.exists("/sys/block/%sboot0" % b):
            continue  # eMMC — reported as the root fs above
        entry = {"dev": b}
        for d, mp in mounts.items():
            if d == b or (d.startswith(b) and d[len(b):].lstrip("p").isdigit()):
                entry["dev"] = d
                try:
                    s = _fs_stats(mp)
                    if s:
                        entry.update(s)
                except Exception:
                    pass
                break
        (usb if is_usb else sd).append(entry)
    if usb:
        st["usb"] = usb
    if sd:
        st["sd"] = sd
    return st


def read_roster_modules(path: str = ROSTER_FILE):
    """The modules block, passed VERBATIM from the bus-free roster cache —
    the agent NEVER opens the RS-485 port. Absent/invalid cache → None."""
    try:
        with open(path) as f:
            roster = json.load(f)
        ports = roster.get("ports") or {}
        if ports:
            return {"ports": ports}
    except Exception:
        pass
    return None


def collect_telemetry(prev_cpu=None) -> tuple:
    """Contract telemetry object. CPU% is computed between successive calls
    (heartbeat cadence) via /proc/stat deltas — no sleep on the loop.
    Returns (telemetry, cpu_snapshot)."""
    t = {}
    t["uptime_s"] = int(float(_read_first("/proc/uptime")))
    try:
        with open("/proc/stat") as f:
            fields = list(map(int, f.readline().split()[1:]))
        idle, total = fields[3] + fields[4], sum(fields)
        if prev_cpu:
            didle, dtotal = idle - prev_cpu[0], total - prev_cpu[1]
            t["cpu"] = round(100.0 * (1 - didle / max(dtotal, 1)), 1)
        cpu_snap = (idle, total)
    except Exception:
        cpu_snap = prev_cpu
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        t["ram_pct"] = round(
            100.0 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"], 1)
    except Exception:
        pass
    try:
        t["temp_c"] = round(
            int(_read_first("/sys/class/thermal/thermal_zone0/temp")) / 1000.0, 1)
    except Exception:
        pass
    try:
        t["storage"] = collect_storage()
    except Exception:
        pass
    t["services_ok"] = _systemctl("is-active", "--quiet", "nginx") == 0
    modules = read_roster_modules()
    if modules:
        t["modules"] = modules
    return t, cpu_snap


# ── Enrollment finalisation (shared by claim + token flows) ───────────────────
def finalize_enrollment(resp: dict, cfg: configparser.ConfigParser,
                        config_path: str, device_id: str) -> bool:
    """Write frpc config + agent config from a claim/enroll response."""
    frpc = resp.get("frpc") or {}
    if not frpc.get("server_addr") or not frpc.get("token"):
        log.error("enrollment response missing frpc profile: %s",
                  {k: v for k, v in resp.items() if k != "frpc"})
        return False
    try:
        write_frpc_config(frpc)
    except Exception as e:
        log.error("cannot write frpc config: %s", e)
        return False
    cfg["cloud"]["enrolled"]  = "true"
    cfg["cloud"]["device_id"] = device_id
    hb = resp.get("heartbeat_interval_s")
    if hb:
        cfg["cloud"]["heartbeat_interval"] = str(int(hb))
    cfg["device"]["serial"] = get_serial()
    save_config(config_path, cfg)
    for p in (ACTIVATION_TOKEN_FILE, PAIR_REQUEST_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    tunnel = ensure_frpc_running()
    log.info("Enrolled as %s; tunnel: %s", device_id, tunnel)
    _write_status("active", device_id=device_id, tunnel=tunnel,
                  serial=cfg["device"]["serial"])
    return True


# ── Claim-code flow (primary) ─────────────────────────────────────────────────
def run_claim_flow(cfg: configparser.ConfigParser, config_path: str) -> bool:
    """POST /claim → show the code via the status file → poll /claim/status
    until claimed/expired. Returns True once enrolled."""
    api_url   = cfg["cloud"]["api_url"].rstrip("/")
    device_id = get_device_id(cfg)
    serial    = get_serial()

    status, resp = api_post(f"{api_url}/claim", {
        "device_id":  device_id,
        "hw_variant": HW_VARIANT,
        "fw_version": get_fw_version(),
    })
    if status == 409:
        log.warning("device already claimed in the cloud — detach it first")
        _write_status("already_claimed", device_id=device_id, serial=serial)
        return False
    if status != 200 or not resp or not resp.get("claim_code"):
        log.warning("claim request failed (HTTP %s)", status)
        _write_status("claim_failed", device_id=device_id, serial=serial)
        return False

    code     = resp["claim_code"]
    ttl      = int(resp.get("expires_in_s", 900))
    poll_s   = max(int(resp.get("poll_interval_s", 5)), 2)
    deadline = time.time() + ttl
    log.info("Pairing code %s (valid %ds) — enter it at %s",
             code, ttl, cfg["cloud"]["server_host"])

    while time.time() < deadline:
        _write_status("pairing", claim_code=code, device_id=device_id,
                      serial=serial, expires_at=int(deadline))
        status, st = api_post(f"{api_url}/claim/status",
                              {"device_id": device_id, "claim_code": code})
        if status == 200 and st:
            state = st.get("state", "")
            if state == "claimed":
                return finalize_enrollment(st, cfg, config_path, device_id)
            if state == "expired":
                break
        # user may cancel pairing from the web UI (trigger file removed)
        if not os.path.exists(PAIR_REQUEST_FILE):
            log.info("pairing cancelled from the web UI")
            _write_status("standby", serial=serial)
            return False
        time.sleep(poll_s)

    log.info("pairing code expired")
    _write_status("pair_expired", device_id=device_id, serial=serial)
    try:
        os.remove(PAIR_REQUEST_FILE)
    except OSError:
        pass
    return False


# ── Enroll-token flow (installer fallback) ────────────────────────────────────
def run_token_flow(token: str, cfg: configparser.ConfigParser,
                   config_path: str) -> bool:
    api_url   = cfg["cloud"]["api_url"].rstrip("/")
    device_id = get_device_id(cfg)
    _write_status("enrolling", device_id=device_id, serial=get_serial())
    status, resp = api_post(f"{api_url}/enroll", {
        "enroll_token": token,
        "device_id":    device_id,
        "hw_variant":   HW_VARIANT,
        "fw_version":   get_fw_version(),
    })
    if status != 200 or not resp or not resp.get("ok"):
        log.warning("enroll failed (HTTP %s): %s", status, resp)
        _write_status("enroll_failed", device_id=device_id)
        return False
    return finalize_enrollment(resp, cfg, config_path, device_id)


# ── Standby loop (wait for an activation trigger) ─────────────────────────────
def bootstrap_loop(cfg: configparser.ConfigParser, config_path: str) -> bool:
    log.info("Standby: waiting for pairing (web UI Cloud tab) or an enroll "
             "token at %s", ACTIVATION_TOKEN_FILE)
    _write_status("standby", serial=get_serial())
    while True:
        if os.path.exists(PAIR_REQUEST_FILE):
            if run_claim_flow(cfg, config_path):
                return True
            time.sleep(STANDBY_POLL_S)
            continue
        if os.path.exists(ACTIVATION_TOKEN_FILE):
            try:
                with open(ACTIVATION_TOKEN_FILE) as f:
                    token = f.read().strip()
            except Exception as e:
                log.error("cannot read activation token: %s", e)
                token = ""
            if token:
                if run_token_flow(token, cfg, config_path):
                    return True
                log.warning("enroll failed, retrying in 60s")
                time.sleep(60)
                continue
        time.sleep(STANDBY_POLL_S)


# ── Active loop — frpc watchdog + send-only heartbeat ─────────────────────────
def active_loop(cfg: configparser.ConfigParser):
    api_url    = cfg["cloud"]["api_url"].rstrip("/")
    device_id  = get_device_id(cfg)
    h_interval = int(cfg["cloud"]["heartbeat_interval"])

    log.info("SA-02m Cloud Agent active (device %s, heartbeat %ds). "
             "Send-only: no command channel.", device_id, h_interval)

    last_heartbeat = 0.0
    last_watchdog  = 0.0
    tunnel         = ensure_frpc_running()
    cpu_snap       = None

    while True:
        now = time.time()

        if now - last_watchdog > WATCHDOG_S:
            tunnel = ensure_frpc_running()
            last_watchdog = now

        if now - last_heartbeat > h_interval:
            telemetry, cpu_snap = collect_telemetry(cpu_snap)
            # Send-only by design: the response carries no commands (F1
            # removed cloud-side too); nothing here interprets it.
            api_post(f"{api_url}/heartbeat", {
                "device_id": device_id,
                "uptime_s":  telemetry.get("uptime_s", 0),
                "telemetry": telemetry,
            })
            _write_status("active", device_id=device_id, tunnel=tunnel,
                          serial=cfg["device"]["serial"],
                          last_heartbeat=int(now))
            last_heartbeat = now

        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="SA-02m Cloud Agent (frpc)")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    os.makedirs("/etc/sa02m-cloud", exist_ok=True)

    cfg = load_config(args.config)

    if not cfg["cloud"].getboolean("enrolled"):
        bootstrap_loop(cfg, args.config)
        cfg = load_config(args.config)

    active_loop(cfg)


if __name__ == "__main__":
    main()
