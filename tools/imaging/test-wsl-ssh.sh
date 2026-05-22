#!/bin/bash
# Быстрая проверка WSL → SA-02m: ping + ssh
set -e
KEY="${KEY:-$HOME/.ssh/sa02m_sa02}"
IP="${IP:-192.168.1.136}"
REPO="/mnt/c/Users/admin/Downloads/SA-02m-web-build"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f "$KEY" ] || cp "$REPO/private/.ssh/sa02m_sa02" "$KEY"
chmod 600 "$KEY"

echo "=== ping $IP ==="
ping -c 2 -W 2 "$IP"

echo "=== ssh $IP ==="
ssh -i "$KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    "root@${IP}" 'echo SSH_OK; uname -nrm; df -h /; ip -4 -o addr show eth0 | awk "{print \$4}"; test -f /root/.not_logged_in_yet && echo FIRSTRUN_SET || echo FIRSTRUN_CLEAR; wc -c /etc/machine-id; systemctl is-active ssh nginx fcgiwrap sa02m-flasher 2>/dev/null | paste -sd, -'
