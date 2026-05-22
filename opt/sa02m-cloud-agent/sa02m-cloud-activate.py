#!/usr/bin/env python3
"""
SA-02m Cloud Activation Script
Registers device with cloud server using a one-time activation token.
Writes agent config and WireGuard client config.

Usage: python3 sa02m-cloud-activate.py --token <ACTIVATION_TOKEN> \
           --server cloud.cyntron.ru [--config /etc/sa02m-cloud/agent.conf]
"""
import argparse
import json
import os
import stat
import subprocess
import sys
import urllib.request
import urllib.error

DEFAULT_CONFIG = "/etc/sa02m-cloud/agent.conf"
DEFAULT_WG_CONF = "/etc/wireguard/wg-cloud.conf"


def api_post(url: str, payload: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "sa02m-activate/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_serial() -> str:
    """Read device serial from /proc/cpuinfo or /etc/machine-id."""
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
    import platform
    return platform.node()


def gen_wg_keypair() -> tuple[str, str]:
    """Generate WireGuard private/public key pair."""
    priv = subprocess.check_output(["wg", "genkey"], timeout=5).decode().strip()
    pub = subprocess.check_output(
        ["wg", "pubkey"], input=priv.encode(), timeout=5
    ).decode().strip()
    return priv, pub


def write_wg_config(path: str, priv_key: str, vpn_ip: str,
                    server_pubkey: str, server_endpoint: str,
                    server_vpn_ip: str = "10.100.0.1"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = f"""[Interface]
PrivateKey = {priv_key}
Address = {vpn_ip}/24
DNS = {server_vpn_ip}

[Peer]
PublicKey = {server_pubkey}
Endpoint = {server_endpoint}:51820
AllowedIPs = {server_vpn_ip}/32
PersistentKeepalive = 25
"""
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    print(f"  WireGuard config written: {path}")


def write_agent_config(path: str, api_url: str, device_token: str, serial: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = f"""[cloud]
api_url = {api_url}
device_token = {device_token}
metrics_interval = 30
heartbeat_interval = 10

[wireguard]
enabled = true
interface = wg-cloud
config = {DEFAULT_WG_CONF}

[device]
serial = {serial}
web_port = 9999
"""
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)  # 0640
    print(f"  Agent config written: {path}")


def main():
    parser = argparse.ArgumentParser(description="SA-02m Cloud Activation")
    parser.add_argument("--token",  required=True, help="One-time activation token from cloud UI")
    parser.add_argument("--server", default="cloud.cyntron.ru", help="Cloud server hostname")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    api_url = f"https://{args.server}/api/v1"
    serial = get_serial()

    print(f"\nSA-02m Cloud Activation")
    print(f"  Server : {args.server}")
    print(f"  Serial : {serial}")
    print()

    # Generate WireGuard keypair
    print("Generating WireGuard keypair...")
    priv_key, pub_key = gen_wg_keypair()
    print(f"  Public key: {pub_key}")

    # Activate with cloud server
    print("\nActivating with cloud server...")
    try:
        resp = api_post(f"{api_url}/devices/activate", {
            "activation_token": args.token,
            "serial": serial,
            "wg_pubkey": pub_key,
            "hostname": serial,
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    device_token   = resp["device_token"]
    vpn_ip         = resp["vpn_ip"]
    server_pubkey  = resp["server_pubkey"]
    server_endpoint = resp.get("server_endpoint", args.server)
    server_vpn_ip  = resp.get("server_vpn_ip", "10.100.0.1")

    print(f"  Device token: {device_token[:8]}...")
    print(f"  VPN IP: {vpn_ip}")
    print(f"  Server pubkey: {server_pubkey[:16]}...")

    # Write configs
    print("\nWriting configuration...")
    write_wg_config(DEFAULT_WG_CONF, priv_key, vpn_ip, server_pubkey,
                    server_endpoint, server_vpn_ip)
    write_agent_config(args.config, api_url, device_token, serial)

    # Start WireGuard
    print("\nStarting WireGuard tunnel...")
    r = subprocess.run(["wg-quick", "up", "wg-cloud"], capture_output=True, text=True)
    if r.returncode == 0:
        print("  WireGuard tunnel UP")
    else:
        print(f"  Warning: wg-quick up: {r.stderr.strip()}")

    # Enable and start agent service
    print("Enabling sa02m-cloud-agent service...")
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "--now", "sa02m-cloud-agent"], check=False)

    print(f"\n✓ Activation complete!")
    print(f"  Device is now registered with {args.server}")
    print(f"  VPN IP: {vpn_ip}")
    print(f"  Web UI accessible at: https://{vpn_ip.replace('.', '-')}.cloud.{args.server.split('.')[-2]}.{args.server.split('.')[-1]}/")
    print()


if __name__ == "__main__":
    main()
