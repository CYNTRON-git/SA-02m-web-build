#!/usr/bin/env python3
"""JSON: host + mqttuser credentials for external clients (stdout)."""
import json
import os
import subprocess


def primary_ipv4() -> str:
    for iface in ("end0", "end1"):
        try:
            out = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show", "dev", iface],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            parts = out.strip().split()
            if len(parts) >= 4:
                return parts[3].split("/")[0]
        except (subprocess.SubprocessError, FileNotFoundError, IndexError):
            continue
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=2)
        tok = out.split()
        return tok[0] if tok else ""
    except subprocess.SubprocessError:
        return ""


def _env_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v.strip()


def load_mqtt_env() -> tuple[str, str]:
    user, passwd = "mqttuser", ""
    path = "/etc/sa02m_mqtt.env"
    if not os.path.isfile(path):
        return user, passwd
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("MQTT_USER="):
                user = _env_value(line.split("=", 1)[1]) or user
            elif line.startswith("MQTT_PASS=") or line.startswith("MQTT_PASSWORD="):
                passwd = _env_value(line.split("=", 1)[1])
    return user, passwd


def main() -> None:
    user, passwd = load_mqtt_env()
    print(
        json.dumps(
            {
                "host": primary_ipv4(),
                "mqtt_user": user,
                "mqtt_password": passwd,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
