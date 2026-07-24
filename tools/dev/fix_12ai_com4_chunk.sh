#!/usr/bin/env bash
# Deploy 12AI COM4 Short-response fix to a live SA-02m stand over SSH.
#
# Changes:
#   1) MR02M_AI_READ_CHUNK_REGS 42 → 21  (modbus_mqtt_bridge.py)
#   2) /etc/sa02m-modbus-mqtt.yaml → mr02m-COM4-12 poll_ai_ao_s: 2
#
# Usage:
#   SA02M_HOST=192.168.10.136 ./tools/dev/fix_12ai_com4_chunk.sh
#   ./tools/dev/fix_12ai_com4_chunk.sh --local   # run on the stand itself
#
# Env: SA02M_HOST (default 192.168.10.136), SA02M_USER, SA02M_PASS
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="${SA02M_HOST:-192.168.10.136}"
USER="${SA02M_USER:-root}"
PASS="${SA02M_PASS:-cyntron}"
BRIDGE_SRC="$ROOT/opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py"
ONDEV="$ROOT/tools/dev/fix_12ai_com4_on_device.py"

if [[ "${1:-}" == "--local" ]]; then
  exec python3 "$ONDEV" "$BRIDGE_SRC"
fi

python3 - <<PY
import os, sys
from pathlib import Path

try:
    import paramiko
except ImportError as e:
    print("need paramiko: pip install paramiko", file=sys.stderr)
    raise SystemExit(2) from e

host = os.environ.get("SA02M_HOST", "$HOST")
user = os.environ.get("SA02M_USER", "$USER")
password = os.environ.get("SA02M_PASS", "$PASS")
bridge = Path("$BRIDGE_SRC")
ondev = Path("$ONDEV")
assert bridge.is_file() and ondev.is_file()

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    host, username=user, password=password,
    timeout=30, banner_timeout=30, auth_timeout=30,
    allow_agent=False, look_for_keys=False,
)
sftp = client.open_sftp()
sftp.put(str(bridge), "/tmp/modbus_mqtt_bridge.py.fix12ai")
sftp.put(str(ondev), "/tmp/fix_12ai_com4_on_device.py")
sftp.close()

cmd = "python3 /tmp/fix_12ai_com4_on_device.py /tmp/modbus_mqtt_bridge.py.fix12ai"
_, stdout, stderr = client.exec_command(cmd, timeout=120)
out = stdout.read().decode("utf-8", "replace")
err = stderr.read().decode("utf-8", "replace")
code = stdout.channel.recv_exit_status()
client.close()
sys.stdout.write(out)
if err:
    sys.stderr.write(err)
raise SystemExit(code)
PY
