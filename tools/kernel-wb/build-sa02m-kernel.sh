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
# PREEMPT_RT pin. rt39 is the only — and final — RT release upstream ever
# published for 5.10.35; rt36 belongs to 5.10.27, so the previous default named
# a file that has never existed. The pin is deliberate: the RT patch level
# changes scheduler and locking behaviour, so a device kernel must be
# reproducible rather than self-updating. Rationale and the escape hatch:
# docs/decisions/rt-patch-pinning.md.
RT_PATCH_VER="${SA02M_RT_PATCH_VER:-rt39}"
RT_PATCH_URL="${SA02M_RT_PATCH_URL:-https://cdn.kernel.org/pub/linux/kernel/projects/rt/5.10/older/patch-${RT_KVER}-${RT_PATCH_VER}.patch.gz}"
# sha256 of the .gz as published in upstream's signed sha256sums.asc, so the pin
# holds the bytes and not merely the filename. Override the version => override
# this too, or set it to "skip" to build without the integrity check.
RT_PATCH_SHA256="${SA02M_RT_PATCH_SHA256:-de1457a7bf65efdcfcca60e02974b0c43fec8d40c20476c1c6c3fcac777d3871}"

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
	RT_PATCH_GZ="$RT_PATCH_FILE.gz"
	if [ ! -f "$RT_PATCH_GZ" ]; then
		echo "[rt-patch] downloading $RT_PATCH_URL"
		if ! curl -fSL "$RT_PATCH_URL" -o "$RT_PATCH_GZ"; then
			rm -f "$RT_PATCH_GZ"
			echo "[rt-patch] ERROR: could not fetch $RT_PATCH_URL" >&2
			echo "[rt-patch] The RT version is pinned deliberately, so this does not self-heal." >&2
			echo "[rt-patch] Check what upstream still publishes for ${RT_KVER}:" >&2
			echo "[rt-patch]   https://cdn.kernel.org/pub/linux/kernel/projects/rt/5.10/older/" >&2
			echo "[rt-patch] then re-run with a matching version+checksum pair:" >&2
			echo "[rt-patch]   SA02M_RT_PATCH_VER=rtNN SA02M_RT_PATCH_SHA256=<sha256> $0 $FLAVOUR" >&2
			echo "[rt-patch] Rationale and fallbacks: docs/decisions/rt-patch-pinning.md" >&2
			exit 1
		fi
	fi
	# Checked on every run, not only after a download: BUILD_ROOT survives between
	# local builds, so a file left over from an earlier run clears the same bar.
	if [ "$RT_PATCH_SHA256" = "skip" ]; then
		echo "[rt-patch] WARNING: integrity check disabled (SA02M_RT_PATCH_SHA256=skip)"
	elif ! echo "$RT_PATCH_SHA256  $RT_PATCH_GZ" | sha256sum -c --status -; then
		echo "[rt-patch] ERROR: sha256 mismatch on $RT_PATCH_GZ" >&2
		echo "[rt-patch]   expected: $RT_PATCH_SHA256" >&2
		echo "[rt-patch]   actual:   $(sha256sum "$RT_PATCH_GZ" | cut -d' ' -f1)" >&2
		echo "[rt-patch] Refusing to patch the kernel with unverified bytes." >&2
		# Discard it, exactly as the download path does: keeping the bad archive
		# would skip the re-download branch above and wedge this build root into
		# failing identically for ever.
		rm -f "$RT_PATCH_GZ"
		echo "[rt-patch] Removed the rejected archive, so a re-run fetches it again." >&2
		echo "[rt-patch] If it keeps mismatching, the checksum and the version disagree —" >&2
		echo "[rt-patch] take the pair from upstream's signed sums and pass both:" >&2
		echo "[rt-patch]   https://cdn.kernel.org/pub/linux/kernel/projects/rt/5.10/older/sha256sums.asc" >&2
		echo "[rt-patch]   SA02M_RT_PATCH_VER=rtNN SA02M_RT_PATCH_SHA256=<sha256> $0 $FLAVOUR" >&2
		echo "[rt-patch] Rationale and fallbacks: docs/decisions/rt-patch-pinning.md" >&2
		exit 1
	fi
	# Re-expand from the verified archive so the .patch cannot drift away from it.
	gunzip -kf "$RT_PATCH_GZ"
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

# Force clean `uname -r` == kernel version (без "+" от setlocalversion).
# CONFIG_LOCALVERSION="" уже в defconfig; scripts/setlocalversion всё равно
# добавит "+" если git tree "dirty" (а он всегда dirty после apply.sh —
# копируем overlay-файлы поверх WB tree). `.scmversion` файл переопределяет
# setlocalversion и заставляет его вывести именно наше значение (пусто).
echo -n "" > .scmversion

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
