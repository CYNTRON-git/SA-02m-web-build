#!/usr/bin/env bash
#
# build-sa02m-kernel.sh — собирает ядро SA-02м на базе wirenboard/linux
# с наложенным kernel-port/-оверлеем.
#
# Usage:
#   ./tools/kernel-wb/build-sa02m-kernel.sh <flavour> [--smoke] [--rebuild]
#
#   <flavour>  — sa02m | sa02m-rt
#   --smoke    — только zImage/dtbs/modules, без сборки .deb (быстрый smoke)
#   --rebuild  — очистить build/wb-linux и клонировать заново
#
# Артефакты:
#   $HOME/build/sa02m-kernel/                                — рабочий каталог
#   $HOME/build/sa02m-kernel/wb-linux/                       — checkout WB
#   $HOME/build/sa02m-kernel/artifacts/<flavour>/            — zImage, dtb, modules.tar.gz
#   $HOME/build/sa02m-kernel/*.deb                           — только без --smoke

set -euo pipefail

FLAVOUR="${1:-}"
if [ -z "$FLAVOUR" ] || [[ "$FLAVOUR" != "sa02m" && "$FLAVOUR" != "sa02m-rt" ]]; then
	echo "usage: $0 {sa02m|sa02m-rt} [--smoke] [--rebuild]" >&2
	exit 2
fi
shift || true

SMOKE=0
REBUILD=0
while [ "$#" -gt 0 ]; do
	case "$1" in
		--smoke) SMOKE=1 ;;
		--rebuild) REBUILD=1 ;;
		*) echo "unknown arg: $1" >&2; exit 2 ;;
	esac
	shift
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD_ROOT="${SA02M_BUILD_ROOT:-$HOME/build/sa02m-kernel}"
WB_TREE="$BUILD_ROOT/wb-linux"
WB_BRANCH="${SA02M_WB_BRANCH:-release/wb-2606/wb7-bullseye}"
WB_REMOTE="${SA02M_WB_REMOTE:-https://github.com/wirenboard/linux}"

RT_KVER="5.10.35"
RT_PATCH_VER="${SA02M_RT_PATCH_VER:-rt36}"
RT_PATCH_URL="${SA02M_RT_PATCH_URL:-https://cdn.kernel.org/pub/linux/kernel/projects/rt/5.10/older/patch-${RT_KVER}-${RT_PATCH_VER}.patch.gz}"

JOBS="${JOBS:-$(nproc)}"
export ARCH=arm
export CROSS_COMPILE="${CROSS_COMPILE:-arm-linux-gnueabihf-}"

echo "==[build-sa02m-kernel.sh]============================================"
echo "  flavour        = $FLAVOUR"
echo "  smoke          = $SMOKE"
echo "  rebuild        = $REBUILD"
echo "  BUILD_ROOT     = $BUILD_ROOT"
echo "  WB_TREE        = $WB_TREE"
echo "  WB_BRANCH      = $WB_BRANCH"
echo "  JOBS           = $JOBS"
echo "  CROSS_COMPILE  = $CROSS_COMPILE"
[ "$FLAVOUR" = "sa02m-rt" ] && echo "  RT patch       = $RT_PATCH_URL"
echo "====================================================================="

mkdir -p "$BUILD_ROOT"

if [ "$REBUILD" = 1 ] && [ -d "$WB_TREE" ]; then
	echo "[cleanup] removing $WB_TREE"
	rm -rf "$WB_TREE"
fi

if [ ! -d "$WB_TREE/.git" ]; then
	echo "[git clone] $WB_REMOTE @ $WB_BRANCH → $WB_TREE"
	git clone --depth 1 -b "$WB_BRANCH" "$WB_REMOTE" "$WB_TREE"
fi

# WB tree references out-of-tree submodules (drivers/net/wireless/realtek/rtl8723bu, rtl8733bu).
# `make sa02m_defconfig` fails to parse Kconfig without those directories present, even though
# we drop all WLAN drivers from the resulting config. `git submodule update` populates them.
echo "[git submodule] init (needed for Kconfig parsing)"
(
	cd "$WB_TREE"
	git submodule update --init --depth 1 --recursive 2>&1 | tail -8 || {
		echo "[submodule] init failed — falling back to Kconfig stub"
		# Fallback: create empty Kconfig stubs so the top-level Kconfig can source them without error.
		for sub in drivers/net/wireless/realtek/rtl8723bu drivers/net/wireless/realtek/rtl8733bu; do
			mkdir -p "$sub"
			[ -f "$sub/Kconfig" ] || : > "$sub/Kconfig"
			[ -f "$sub/Makefile" ] || : > "$sub/Makefile"
		done
	}
)

echo "[apply overlay + patches]"
"$REPO_ROOT/kernel-port/apply.sh" "$WB_TREE"

if [ "$FLAVOUR" = "sa02m-rt" ]; then
	RT_PATCH_FILE="$BUILD_ROOT/patch-${RT_KVER}-${RT_PATCH_VER}.patch"
	if [ ! -f "$RT_PATCH_FILE" ]; then
		echo "[rt-patch] downloading $RT_PATCH_URL"
		curl -fSL "$RT_PATCH_URL" -o "$RT_PATCH_FILE.gz"
		gunzip -f "$RT_PATCH_FILE.gz"
	fi
	if ! (cd "$WB_TREE" && patch -p1 --dry-run --reverse --silent < "$RT_PATCH_FILE" >/dev/null 2>&1); then
		if (cd "$WB_TREE" && patch -p1 --dry-run --silent < "$RT_PATCH_FILE" >/dev/null 2>&1); then
			echo "[rt-patch] applying $(basename "$RT_PATCH_FILE")"
			(cd "$WB_TREE" && patch -p1 < "$RT_PATCH_FILE")
		else
			echo "[rt-patch] WARNING: patch does not apply cleanly — attempting merge"
			(cd "$WB_TREE" && patch -p1 --merge=diff3 < "$RT_PATCH_FILE") \
				|| { echo "[rt-patch] ERROR: manual merge required" >&2; exit 1; }
		fi
	else
		echo "[rt-patch] already applied"
	fi
fi

cd "$WB_TREE"

echo "[make sa02m_defconfig]"
make sa02m_defconfig

if [ "$FLAVOUR" = "sa02m-rt" ]; then
	echo "[merge sa02m_rt.config]"
	./scripts/kconfig/merge_config.sh -m .config arch/arm/configs/sa02m_rt.config >/dev/null
	make olddefconfig
fi

if [ "$SMOKE" = 1 ]; then
	echo "[smoke build] zImage + dtbs (no modules install)"
	time make -j"$JOBS" zImage sun8i-r40-sa02m.dtb
	echo ""
	echo "  zImage    : $(ls -la arch/arm/boot/zImage)"
	echo "  DTB       : $(ls -la arch/arm/boot/dts/sun8i-r40-sa02m.dtb)"
	echo ""
	echo "[smoke check] key symbols in .config"
	for sym in MFD_AXP20X_I2C REGULATOR_AXP20X AXP20X_ADC \
	           STMMAC_ETH SUN4I_EMAC ICPLUS_PHY \
	           MMC_SUNXI CAN_SUN4I SUNXI_WATCHDOG SUN8I_THERMAL \
	           RTC_DRV_DS1307 GPIO_PCA953X \
	           OVERLAY_FS VETH MACVLAN VXLAN TUN WIREGUARD \
	           NF_TABLES CGROUPS BRIDGE \
	           CRYPTO_DEV_SUN8I_CE CRYPTO_DEV_SUN8I_SS \
	           SERIAL_8250_DW SPI_SUN6I I2C_MV64XXX; do
		if grep -qE "^CONFIG_${sym}=[ym]" .config; then
			printf "  OK   CONFIG_%s\n" "$sym"
		else
			printf "  MISS CONFIG_%s\n" "$sym" >&2
			exit 1
		fi
	done
	echo ""
	echo "[smoke check] undesired symbols (must be absent)"
	for sym in MFD_WBEC GPIO_WBEC WBEC_POWER WBEC_WATCHDOG WBEC_ADC RTC_DRV_WBEC \
	           MFD_AC100 RTC_DRV_AC100 CFG80211 BT USB_GADGET MEDIA_SUPPORT; do
		if grep -qE "^CONFIG_${sym}=[ym]" .config; then
			printf "  UNEXPECTED CONFIG_%s is enabled\n" "$sym" >&2
			exit 1
		else
			printf "  OK   CONFIG_%s is disabled\n" "$sym"
		fi
	done
	echo ""
	echo "[smoke build OK]"
	exit 0
fi

echo "[full build] bindeb-pkg (may take 20-40 minutes)"
KDEB_PKGVERSION="5.10.35-${FLAVOUR}-$(date +%Y%m%d%H%M)"
time make -j"$JOBS" KDEB_PKGVERSION="$KDEB_PKGVERSION" bindeb-pkg

echo ""
echo "[collect artifacts] $BUILD_ROOT/*.deb"
mv -v "$BUILD_ROOT"/wb-linux/../*.deb "$BUILD_ROOT/" 2>/dev/null || true
ls -la "$BUILD_ROOT"/*.deb 2>/dev/null || echo "  (no *.deb found)"

echo ""
echo "[done]"
