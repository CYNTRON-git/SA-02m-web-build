#!/usr/bin/env python3
"""
SA-02m Cloud Activation — intentional technician-CLI fallback (one-home: O7).

This is NOT a second implementation of the enrollment path. The canonical
enrollment logic lives ONLY in sa02m-cloud-agent.py (run_token_flow /
finalize_enrollment / render_frpc_toml, incl. the device-side local-port
allow-list). This CLI is a thin trigger: it writes the enroll token to
/etc/sa02m-cloud/activation_token, makes sure the agent service runs, and tails
the agent's /run status until enrollment finishes. It holds no protocol logic
of its own by design — so there is nothing to duplicate and nothing to keep in
sync. Kept as the SSH-less technician path for installers; end users should
prefer the pairing-code flow in the device web UI (Cloud tab) — no token to
copy at all.

Usage: sa02m-cloud-activate --token <ENROLL_TOKEN> [--server cloud.cyntron.ru]
"""
import argparse
import configparser
import json
import os
import subprocess
import sys
import time

DEFAULT_CONFIG        = "/etc/sa02m-cloud/agent.conf"
ACTIVATION_TOKEN_FILE = "/etc/sa02m-cloud/activation_token"
STATUS_FILE           = "/run/sa02m-cloud-status.json"
WAIT_S                = 90


def read_status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser(description="SA-02m Cloud Activation")
    parser.add_argument("--token",  required=True,
                        help="Enroll token from the cloud fleet UI")
    parser.add_argument("--server", default="",
                        help="Cloud server hostname (default: keep agent config)")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    os.makedirs("/etc/sa02m-cloud", exist_ok=True)

    if args.server:
        cfg = configparser.ConfigParser()
        cfg.read(args.config)
        if not cfg.has_section("cloud"):
            cfg.add_section("cloud")
        cfg["cloud"]["server_host"] = args.server
        cfg["cloud"]["api_url"] = f"https://{args.server}/api/v1"
        with open(args.config, "w") as f:
            cfg.write(f)
        os.chmod(args.config, 0o640)

    with open(ACTIVATION_TOKEN_FILE, "w") as f:
        f.write(args.token.strip() + "\n")
    os.chmod(ACTIVATION_TOKEN_FILE, 0o600)
    print("Token written; starting the cloud agent...")

    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "--now", "sa02m-cloud-agent"],
                   check=False)

    print("Waiting for enrollment (up to %ds)..." % WAIT_S)
    deadline = time.time() + WAIT_S
    state = ""
    while time.time() < deadline:
        state = read_status().get("state", "")
        if state == "active":
            st = read_status()
            print("\n✓ Enrolled as %s (tunnel: %s)"
                  % (st.get("device_id", "?"), st.get("tunnel", "?")))
            return
        if state in ("enroll_failed",):
            break
        time.sleep(3)

    print("\nEnrollment did not finish (state: %s)." % (state or "unknown"))
    print("Check: journalctl -u sa02m-cloud-agent -n 50")
    sys.exit(1)


if __name__ == "__main__":
    main()
