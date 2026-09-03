#!/bin/bash
# Reset SA-02m cloud enrollment to factory (standby, not claimed).
# Safe for clone images: removes per-device secret + frpc tunnel profile.
set -euo pipefail

CLOUD_DIR=/etc/sa02m-cloud
CFG=$CLOUD_DIR/agent.conf

systemctl stop sa02m-cloud-frpc.service 2>/dev/null || true
systemctl stop sa02m-cloud-agent.service 2>/dev/null || true

mkdir -p "$CLOUD_DIR"
chmod 750 "$CLOUD_DIR"

rm -f \
  "$CLOUD_DIR/device_secret" \
  "$CLOUD_DIR/frpc.toml" \
  "$CLOUD_DIR/frpc.toml.bak"* \
  "$CLOUD_DIR/pair_request" \
  "$CLOUD_DIR/activation_token" \
  /run/sa02m-cloud-status.json \
  /run/sa02m-cloud-frpc.cursor

# Keep agent.conf skeleton but clear enrollment identity.
if [ -f "$CFG" ]; then
  python3 - <<'PY'
import configparser, os
path = "/etc/sa02m-cloud/agent.conf"
cfg = configparser.ConfigParser()
cfg.read(path)
if not cfg.has_section("cloud"):
    cfg.add_section("cloud")
cfg["cloud"]["api_url"] = cfg["cloud"].get("api_url", "https://cloud.cyntron.ru/api/v1") or "https://cloud.cyntron.ru/api/v1"
cfg["cloud"]["server_host"] = cfg["cloud"].get("server_host", "cloud.cyntron.ru") or "cloud.cyntron.ru"
cfg["cloud"]["enrolled"] = "false"
cfg["cloud"]["device_id"] = ""
cfg["cloud"]["heartbeat_interval"] = cfg["cloud"].get("heartbeat_interval", "30") or "30"
# drop token leftovers AND the stand-down marker (unlinked_* — identity, not
# configuration: a clone must not boot on the donor's «Доступ отозван»;
# docs/contracts/image-identity-reset.md §6)
for opt in ("device_token", "metrics_interval",
            "unlinked_at", "unlinked_reason", "unlinked_reason_text"):
    if cfg.has_option("cloud", opt):
        cfg.remove_option("cloud", opt)
if cfg.has_section("wireguard"):
    cfg.remove_section("wireguard")
with open(path, "w") as f:
    cfg.write(f)
os.chmod(path, 0o640)
print("agent.conf reset: enrolled=false device_id=")
PY
else
  cat > "$CFG" <<'EOF'
[cloud]
api_url = https://cloud.cyntron.ru/api/v1
server_host = cloud.cyntron.ru
enrolled = false
device_id =
heartbeat_interval = 30

[device]
serial =
web_port = 9999
EOF
  chmod 640 "$CFG"
fi

# Agent stays enabled in standby (waits for pair/token); tunnel stays off until enroll.
systemctl enable sa02m-cloud-agent.service 2>/dev/null || true
systemctl restart sa02m-cloud-agent.service 2>/dev/null || true
systemctl disable sa02m-cloud-frpc.service 2>/dev/null || true

sleep 1
echo "=== VERIFY ==="
echo -n "enrolled="; grep -E '^enrolled' "$CFG" || true
echo -n "device_id="; grep -E '^device_id' "$CFG" || true
ls -la "$CLOUD_DIR"
systemctl is-active sa02m-cloud-agent sa02m-cloud-frpc 2>&1 || true
test ! -f "$CLOUD_DIR/device_secret" && echo "device_secret=ABSENT_OK"
test ! -f "$CLOUD_DIR/frpc.toml" && echo "frpc.toml=ABSENT_OK"
cat /run/sa02m-cloud-status.json 2>/dev/null || echo "status=none_yet"
