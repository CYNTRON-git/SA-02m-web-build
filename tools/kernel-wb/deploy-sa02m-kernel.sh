#!/usr/bin/env bash
#
# deploy-sa02m-kernel.sh — заливает свежесобранные linux-image-*.deb на SA-02м
# и через `dpkg -i` устанавливает их. Post-inst-хук
# `/etc/kernel/postinst.d/50-sa02m-fat-sync` синхронизирует FAT-раздел
# автоматически.
#
# Usage:
#   ./tools/kernel-wb/deploy-sa02m-kernel.sh <ssh-target> [--flavour sa02m|sa02m-rt|both] [--reboot]
#
# Пример:
#   ./tools/kernel-wb/deploy-sa02m-kernel.sh root@192.168.1.50 --flavour both --reboot

set -euo pipefail

SSH_TARGET="${1:-}"
if [ -z "$SSH_TARGET" ]; then
	echo "usage: $0 <ssh-target> [--flavour sa02m|sa02m-rt|both] [--reboot]" >&2
	exit 2
fi
shift || true

FLAVOUR_FILTER=both
DO_REBOOT=0
while [ "$#" -gt 0 ]; do
	case "$1" in
		--flavour) FLAVOUR_FILTER="${2:-both}"; shift 2 ;;
		--reboot)  DO_REBOOT=1; shift ;;
		*) echo "unknown arg: $1" >&2; exit 2 ;;
	esac
done

BUILD_ROOT="${SA02M_BUILD_ROOT:-$HOME/build/sa02m-kernel}"
SSH_OPTS="${SA02M_SSH_OPTS:--o StrictHostKeyChecking=accept-new -o ConnectTimeout=20}"

echo "==[deploy-sa02m-kernel.sh]==========================================="
echo "  ssh target = $SSH_TARGET"
echo "  filter     = $FLAVOUR_FILTER"
echo "  do reboot  = $DO_REBOOT"
echo "  BUILD_ROOT = $BUILD_ROOT"
echo "====================================================================="

if [ ! -d "$BUILD_ROOT" ]; then
	echo "error: $BUILD_ROOT does not exist. Run build-sa02m-kernel.sh first." >&2
	exit 2
fi

case "$FLAVOUR_FILTER" in
	sa02m)     pattern='linux-image-sa02m_*.deb linux-headers-sa02m_*.deb linux-libc-dev*.deb' ;;
	sa02m-rt)  pattern='linux-image-sa02m-rt_*.deb linux-headers-sa02m-rt_*.deb linux-libc-dev*.deb' ;;
	both)      pattern='linux-image-*.deb linux-headers-*.deb linux-libc-dev*.deb' ;;
	*) echo "bad --flavour: $FLAVOUR_FILTER" >&2; exit 2 ;;
esac

DEBS=()
for pat in $pattern; do
	while IFS= read -r -d '' f; do
		DEBS+=("$f")
	done < <(find "$BUILD_ROOT" -maxdepth 1 -name "$pat" -print0 2>/dev/null)
done

if [ "${#DEBS[@]}" -eq 0 ]; then
	echo "error: no .deb found matching '$pattern' under $BUILD_ROOT" >&2
	exit 2
fi

echo "[found ${#DEBS[@]} .deb files]"
for d in "${DEBS[@]}"; do echo "   $(basename "$d")"; done

echo "[remote check] compatible on target"
if ! ssh $SSH_OPTS "$SSH_TARGET" "grep -qE 'cyntron,sa-02m|sk,a40i-nano-2e' /proc/device-tree/compatible"; then
	echo "error: target does not report cyntron,sa-02m or sk,a40i-nano-2e in compatible" >&2
	echo "       (aborting to avoid installing armhf-A40i kernel on a wrong device)" >&2
	exit 2
fi

echo "[remote prep] stop mplc4 / codesyscontrol (best effort)"
ssh $SSH_OPTS "$SSH_TARGET" 'systemctl stop mplc4.service 2>/dev/null || true; \
                systemctl stop codesyscontrol.service 2>/dev/null || true; \
                sync'

echo "[scp] copying .deb to /tmp/ on target"
scp $SSH_OPTS "${DEBS[@]}" "$SSH_TARGET":/tmp/

REMOTE_LIST="$(printf '/tmp/%s ' "$(for d in "${DEBS[@]}"; do echo "$(basename "$d")"; done)")"
REMOTE_LIST=$(for d in "${DEBS[@]}"; do echo -n "/tmp/$(basename "$d") "; done)

echo "[dpkg -i] installing on target"
# shellcheck disable=SC2029
ssh $SSH_OPTS "$SSH_TARGET" "dpkg -i $REMOTE_LIST"

echo "[remote cleanup] removing /tmp/*.deb"
# shellcheck disable=SC2029
ssh $SSH_OPTS "$SSH_TARGET" "rm -f $REMOTE_LIST"

echo "[remote status]"
ssh $SSH_OPTS "$SSH_TARGET" '/usr/local/sbin/sa02m-kernel-select.sh status || true'

if [ "$DO_REBOOT" = 1 ]; then
	echo "[remote reboot]"
	ssh $SSH_OPTS "$SSH_TARGET" 'shutdown -r +1 "sa02m kernel deploy: rebooting in 1 min"' || true
fi

echo "[done]"
