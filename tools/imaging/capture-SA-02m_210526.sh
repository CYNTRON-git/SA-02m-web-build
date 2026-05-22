#!/bin/bash
# Ждёт ssh и запускает make-image.sh
set -euo pipefail
REPO="/mnt/c/Users/admin/Downloads/SA-02m-web-build"
DEVICE_IP="${DEVICE_IP:-192.168.1.136}"
cd "$REPO/tools/imaging"
IP="$DEVICE_IP" bash wait-donor.sh
exec bash make-image.sh \
    --ip "$DEVICE_IP" \
    --key "$HOME/.ssh/sa02m_sa02" \
    --name SA-02m_210526 \
    --out-dir ./out
