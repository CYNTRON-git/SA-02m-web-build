#!/bin/bash
# Ждёт ping + ssh после reboot (до 5 мин).
set -e
KEY="$HOME/.ssh/sa02m_sa02"
IP="${IP:-192.168.1.136}"
REPO="/mnt/c/Users/admin/Downloads/SA-02m-web-build"
MAX="${MAX:-40}"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp "$REPO/private/.ssh/sa02m_sa02" "$KEY" 2>/dev/null || true
chmod 600 "$KEY"

for i in $(seq 1 "$MAX"); do
    if ping -c 1 -W 2 "$IP" >/dev/null 2>&1; then
        echo "[$(date +%H:%M:%S)] ping OK (attempt $i)"
        if ssh -i "$KEY" -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
            "root@${IP}" 'echo SSH_OK; uname -nrm; df -h /' 2>/dev/null; then
            exit 0
        fi
        echo "[$(date +%H:%M:%S)] ping ok, ssh not ready yet"
    else
        echo "[$(date +%H:%M:%S)] waiting ping ($i/$MAX)"
    fi
    sleep 8
done
echo "TIMEOUT waiting for donor" >&2
exit 1
