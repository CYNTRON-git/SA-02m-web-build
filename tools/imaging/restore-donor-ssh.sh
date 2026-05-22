#!/bin/bash
# Восстановление sshd на доноре после прерванного снятия образа.
# Запускать НА ПЛАТЕ (serial console / локальный root shell):
#   bash restore-donor-ssh.sh
# Или с хоста:
#   ssh root@IP 'bash -s' < fix-donor-after-abort.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/fix-donor-after-abort.sh" "$@"
