#!/bin/bash
set -e
KEY="$HOME/.ssh/sa02m_sa02"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp /mnt/c/Users/admin/Downloads/SA-02m-web-build/private/.ssh/sa02m_sa02 "$KEY"
chmod 600 "$KEY"

echo "=== ping ==="
ping -c 2 192.168.1.136

echo "=== ssh ==="
for i in $(seq 1 8); do
    echo "attempt $i"
    if ssh -i "$KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
        root@192.168.1.136 'echo SSH_WSL_OK; uname -nrm; df -h /'; then
        echo "SUCCESS"
        exit 0
    fi
    sleep 10
done
echo "FAILED"
exit 1
