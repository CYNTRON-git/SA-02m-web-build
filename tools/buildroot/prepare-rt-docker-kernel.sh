#!/bin/bash
# prepare-rt-docker-kernel.sh — подготовка и пересборка RT-ядра SA-02m (A40i) в Buildroot VM.
#
# RT (PREEMPT_RT) включается ПАТЧЕМ при сборке ядра — см. Starterkit «Сборка ядра на ВМ.txt»:
#   1) linux/patch-6.1-rc6-rt4.patch  → применяется на этапе `make linux-patch`
#   2) после патча в menuconfig: Expert → Preemption Model → Fully Preemptible Kernel (RT)
#   3) скрипт делает шаг 2 автоматически (PREEMPT_RT=y) только ПОСЛЕ linux-patch
#
# Запуск на VM (sudo):
#   cd /home/user/src/buildroot-2022.08.8-sk-a40i
#   sudo bash /home/user/prepare-rt-docker-kernel.sh check
#   sudo bash /home/user/prepare-rt-docker-kernel.sh build-kernel
set -euo pipefail

BR_DIR="${BR_DIR:-/home/user/src/buildroot-2022.08.8-sk-a40i}"
RT_PATCH_URL="${RT_PATCH_URL:-https://cdn.kernel.org/pub/linux/kernel/projects/rt/6.1/older/patch-6.1-rc6-rt4.patch.gz}"
DEFCONFIG_OUT="${DEFCONFIG_OUT:-board/starterkit/sk-a40i-sodimm/linux-sa02m-rt-docker.defconfig}"
LINUX_DIR="$BR_DIR/output/build/linux-custom"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

require_root() {
    [ "$(id -u)" -eq 0 ] || { echo "Run as root inside VM" >&2; exit 1; }
}

require_br() {
    [ -d "$BR_DIR" ] || { echo "Buildroot not found: $BR_DIR" >&2; exit 1; }
    cd "$BR_DIR"
}

ensure_rt_patch() {
    require_br
    mkdir -p linux
    local patch_file="linux/patch-6.1-rc6-rt4.patch"
    if [ ! -s "$patch_file" ]; then
        log "Downloading RT patch for A40i (patch-6.1-rc6-rt4)..."
        wget -qO- "$RT_PATCH_URL" | zcat > "$patch_file"
    fi
    log "RT patch ready: $patch_file ($(wc -l < "$patch_file") lines)"
    log "Buildroot applies it at: make linux-patch (do NOT duplicate via BR2_LINUX_KERNEL_PATCH)"
}

verify_rt_patch_applied() {
    [ -d "$LINUX_DIR" ] || return 0
    if [ -f "$LINUX_DIR/kernel/sched/rt.c" ] || grep -q '^config PREEMPT_RT' "$LINUX_DIR/kernel/Kconfig.preempt" 2>/dev/null; then
        log "RT patch applied in linux-custom (PREEMPT_RT Kconfig present)"
        return 0
    fi
    log "WARN: RT patch markers not found — run 'make linux-patch' after rm linux-custom" >&2
    return 1
}

cmd_check() {
    require_root
    require_br
    ensure_rt_patch
    log "Buildroot: $BR_DIR"
    log "Host: $(uname -a)"
    if [ -d "$LINUX_DIR" ]; then
        verify_rt_patch_applied || true
        log "linux-custom exists — kernel version in .config:"
        grep '^CONFIG_LOCALVERSION=' "$LINUX_DIR/.config" 2>/dev/null || true
        grep -E '^CONFIG_PREEMPT_RT|^CONFIG_OVERLAY_FS|^CONFIG_ICPLUS_PHY|^CONFIG_RTC_DRV_DS3231|^CONFIG_USB_SERIAL_OPTION' \
            "$LINUX_DIR/.config" 2>/dev/null || true
    else
        log "linux-custom not extracted yet — first 'make' will download sources"
    fi
    log "Required after build: output/images/zImage + output/target/lib/modules/6.1.0-rc6-rt4/"
}

apply_sa02m_boot_kconfig() {
    # Boot-critical options from sunxi_sk_defconfig — olddefconfig after RT/Docker can drop ARCH_SUNXI.
    local sym
    for sym in \
        ARCH_SUNXI MACH_SUN8I ARCH_SUNXI_MC_SMP \
        DEVTMPFS DEVTMPFS_MOUNT \
        EXT4_FS EXT4_USE_FOR_EXT2 TMPFS \
        MMC MMC_BLOCK MMC_SUNXI PWRSEQ_EMMC \
        STMMAC_ETH STMMAC_PLATFORM DWMAC_SUNXI DWMAC_SUN8I DWMAC_GENERIC \
        ICPLUS_PHY \
        PINCTRL_SUNXI CLK_SUNXI SUNXI_CCU SUNXI_SRAM SUNXI_MBUS RESET_SUNXI \
        SERIAL_8250 SERIAL_8250_CONSOLE SERIAL_8250_DW SERIAL_OF_PLATFORM \
        I2C I2C_CHARDEV I2C_SUN6I_P2WI SUNXI_RSB \
        USB USB_EHCI_HCD USB_OHCI_HCD USB_MUSB_SUNXI \
        WATCHDOG SUNXI_WATCHDOG \
        NEW_LEDS LEDS_CLASS LEDS_GPIO LEDS_TRIGGERS LEDS_TRIGGER_HEARTBEAT LEDS_TRIGGER_DEFAULT_ON \
        RTC_CLASS RTC_DRV_PCF8563 RTC_DRV_SUNXI RTC_DRV_SUN6I RTC_DRV_DS3231 \
        GPIOLIB OF_GPIO \
        NETDEVICES PHYLIB
    do
        ./scripts/config --enable "$sym" || log "WARN: could not enable CONFIG_${sym}"
    done
}

verify_sa02m_boot_kconfig() {
    local missing=0 sym val cfg="$LINUX_DIR/.config"
    for sym in ARCH_SUNXI MMC_SUNXI STMMAC_ETH DWMAC_SUN8I EXT4_FS; do
        val=$(grep "^CONFIG_${sym}=" "$cfg" 2>/dev/null || true)
        case "$val" in
            *=y|*=m) ;;
            *) log "ERROR: missing boot-critical CONFIG_${sym} (got: ${val:-unset})" >&2; missing=1 ;;
        esac
    done
    grep -q '^CONFIG_PREEMPT_RT=y' "$cfg" || { log "ERROR: CONFIG_PREEMPT_RT not set" >&2; missing=1; }
    [ "$missing" -eq 0 ]
}

verify_sa02m_smp_boot_kconfig() {
    local missing=0 sym val cfg="$LINUX_DIR/.config"
    for sym in ARCH_SUNXI MMC_SUNXI STMMAC_ETH DWMAC_SUN8I EXT4_FS; do
        val=$(grep "^CONFIG_${sym}=" "$cfg" 2>/dev/null || true)
        case "$val" in
            *=y|*=m) ;;
            *) log "ERROR: missing boot-critical CONFIG_${sym} (got: ${val:-unset})" >&2; missing=1 ;;
        esac
    done
    if grep -q '^CONFIG_PREEMPT_RT=y' "$cfg" 2>/dev/null; then
        log "ERROR: CONFIG_PREEMPT_RT must be off for SMP build" >&2
        missing=1
    fi
    [ "$missing" -eq 0 ]
}

apply_smp_kconfig_snippets() {
    [ -f "$LINUX_DIR/.config" ] || { log "No .config — run linux-configure first"; return 1; }
    cd "$LINUX_DIR"
    log "Applying Kconfig (SMP + Docker + SA-02m, no PREEMPT_RT)..."

    kcfg() {
        local cmd=$1; shift
        local sym
        for sym in "$@"; do
            ./scripts/config "--$cmd" "$sym"
        done
    }

    if ! grep -q '^CONFIG_ARCH_SUNXI=y' .config; then
        log "WARN: ARCH_SUNXI missing — re-applying sunxi_sk_defconfig base"
        make ARCH=arm sunxi_sk_defconfig
    fi

    kcfg disable PREEMPT_RT 2>/dev/null || ./scripts/config --disable PREEMPT_RT || true
    ./scripts/config --set-str LOCALVERSION ""
    kcfg disable LOCALVERSION_AUTO

    apply_docker_netfilter_kconfig
    apply_sa02m_boot_kconfig

    kcfg module USB_USBNET USB_NET_CDCETHER USB_NET_CDC_NCM USB_NET_CDC_MBIM USB_NET_QMI_WWAN
    kcfg module USB_SERIAL USB_SERIAL_OPTION USB_ACM

    make ARCH=arm olddefconfig
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig
    make ARCH=arm olddefconfig
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig

    verify_sa02m_smp_boot_kconfig || return 1
    verify_docker_netfilter_kconfig || return 1
    kconfig_finalize_noninteractive smp || return 1
    verify_sa02m_smp_boot_kconfig || return 1
    verify_docker_netfilter_kconfig || return 1
    log "SMP Kconfig snippets applied (ARCH_SUNXI + Docker NAT, no PREEMPT_RT)."
}

apply_kconfig_snippets() {
    # После make linux-patch + linux-configure. PREEMPT_RT=y — только если RT-патч уже применён.
    [ -f "$LINUX_DIR/.config" ] || { log "No .config — run linux-configure first"; return 1; }
    verify_rt_patch_applied || return 1
    cd "$LINUX_DIR"
    log "Applying Kconfig (PREEMPT_RT after patch + Docker + SA-02m)..."

    kcfg() {
        local cmd=$1; shift
        local sym
        for sym in "$@"; do
            ./scripts/config "--$cmd" "$sym"
        done
    }

    # Ensure sunxi base (Buildroot should have applied sunxi_sk; re-apply if stale).
    if ! grep -q '^CONFIG_ARCH_SUNXI=y' .config; then
        log "WARN: ARCH_SUNXI missing after linux-configure — re-applying sunxi_sk_defconfig base"
        make ARCH=arm sunxi_sk_defconfig
    fi

    kcfg enable EXPERT
    ./scripts/config --set-val PREEMPT_RT y
    ./scripts/config --set-str LOCALVERSION ""
    kcfg disable LOCALVERSION_AUTO
    kcfg enable HIGH_RES_TIMERS NO_HZ_FULL
    ./scripts/config --set-val HZ_1000 y

    # Docker / netfilter (re-applied after olddefconfig — see apply_docker_netfilter_kconfig)
    apply_docker_netfilter_kconfig

    apply_sa02m_boot_kconfig

    # USB modem (Первая сборка.docx + scripts/01-system.sh)
    kcfg module USB_USBNET USB_NET_CDCETHER USB_NET_CDC_NCM USB_NET_CDC_MBIM USB_NET_QMI_WWAN
    kcfg module USB_SERIAL USB_SERIAL_OPTION USB_ACM

    make ARCH=arm olddefconfig
    # olddefconfig may drop sunxi/netfilter — force again and reconcile.
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig
    make ARCH=arm olddefconfig
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig

    verify_sa02m_boot_kconfig || return 1
    verify_docker_netfilter_kconfig || return 1
    kconfig_finalize_noninteractive rt || return 1
    verify_sa02m_boot_kconfig || return 1
    verify_docker_netfilter_kconfig || return 1
    log "Kconfig snippets applied (ARCH_SUNXI + PREEMPT_RT + NAT verified)."
}

apply_docker_netfilter_kconfig() {
    local sym
    for sym in \
        OVERLAY_FS NAMESPACES UTS_NS IPC_NS PID_NS NET_NS USER_NS POSIX_MQUEUE \
        CGROUPS CGROUP_FREEZER CGROUP_DEVICE CGROUP_CPUACCT CGROUP_SCHED CGROUP_PIDS CGROUP_NET_PRIO \
        VETH BRIDGE BRIDGE_NETFILTER NETFILTER NETFILTER_ADVANCED NETFILTER_INGRESS \
        NF_TABLES NF_TABLES_INET NF_TABLES_IPV4 NF_TABLES_IPV6 \
        NF_NAT NF_NAT_IPV4 NF_NAT_MASQUERADE NFT_NAT NFT_MASQ NFT_CHAIN_NAT \
        NETFILTER_XTABLES NETFILTER_XT_NAT NETFILTER_XT_MATCH_ADDRTYPE \
        NETFILTER_XT_MATCH_CONNTRACK NETFILTER_XT_CONNTRACK \
        IP_NF_IPTABLES IP_NF_FILTER IP_NF_NAT IP_NF_RAW IP_NF_TARGET_MASQUERADE IP_NF_TARGET_REDIRECT \
        NF_DEFRAG_IPV4 NF_CONNTRACK NF_CONNTRACK_IPV4 INET_DIAG \
        BPF BPF_SYSCALL BPF_JIT CGROUP_BPF
    do
        ./scripts/config --enable "$sym" || log "WARN: could not enable CONFIG_${sym}"
    done
}

verify_docker_netfilter_kconfig() {
    local missing=0 sym val cfg="$LINUX_DIR/.config"
    for sym in NF_NAT IP_NF_NAT NETFILTER_XTABLES NF_TABLES IP_NF_IPTABLES NETFILTER_XT_MATCH_CONNTRACK NETFILTER_XT_MATCH_ADDRTYPE; do
        val=$(grep "^CONFIG_${sym}=" "$cfg" 2>/dev/null || true)
        case "$val" in
            *=y|*=m) ;;
            *) log "ERROR: missing Docker-critical CONFIG_${sym} (got: ${val:-unset})" >&2; missing=1 ;;
        esac
    done
    [ "$missing" -eq 0 ]
}

# olddefconfig/syncconfig must not block on NEW symbols when buildroot runs make linux.
kconfig_finalize_noninteractive() {
    local profile=${1:-smp}
    [ -f "$LINUX_DIR/.config" ] || return 1
    cd "$LINUX_DIR"
    if ! grep -q '^CONFIG_ARCH_SUNXI=y' .config; then
        log "WARN: ARCH_SUNXI missing before finalize — sunxi_sk_defconfig"
        make ARCH=arm sunxi_sk_defconfig
    fi
    log "Finalizing Kconfig (non-interactive olddefconfig + syncconfig, profile=$profile)..."
    yes '' | make ARCH=arm olddefconfig >/dev/null
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig
    if [ "$profile" = rt ]; then
        ./scripts/config --set-val PREEMPT_RT y 2>/dev/null || true
        ./scripts/config --set-str LOCALVERSION "" 2>/dev/null || true
    else
        ./scripts/config --disable PREEMPT_RT 2>/dev/null || true
        ./scripts/config --set-str LOCALVERSION "" 2>/dev/null || true
    fi
    yes '' | make ARCH=arm olddefconfig >/dev/null
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig
    yes '' | make ARCH=arm syncconfig >/dev/null
    apply_sa02m_boot_kconfig
    apply_docker_netfilter_kconfig
    cd "$BR_DIR"
}

cmd_menuconfig() {
    require_root
    require_br
    ensure_rt_patch
    log "linux-menuconfig: PREEMPT_RT only after RT patch is applied."
    log "  Prefer: $0 build-kernel"
    log "  Or manually: make linux-extract && make linux-patch && make linux-configure"
    log "  Then: Expert → Preemption Model → Fully Preemptible Kernel (RT)"
    make linux-menuconfig
    apply_kconfig_snippets || true
}

cmd_build_kernel_smp_only() {
    require_root
    require_br
    log "SMP kernel-only build (no RT patch, Docker netfilter for SA-02m)..."
    if [ -f .config ]; then
        sed -i 's|^BR2_LINUX_KERNEL_PATCH=.*|BR2_LINUX_KERNEL_PATCH=""|' .config
    fi
    rm -rf ./output/build/linux-custom
    log "linux extract + configure (no RT patch)..."
    make linux-extract
    # buildroot linux-configure depends on .stamp_patched — skip PREEMPT_RT patch for SMP
    touch ./output/build/linux-custom/.stamp_patched
    make linux-configure
    if [ -f "$LINUX_DIR/.config" ]; then
        apply_smp_kconfig_snippets
    else
        log "ERROR: no $LINUX_DIR/.config after linux-configure" >&2
        return 1
    fi
    kconfig_finalize_noninteractive smp || return 1
    log "linux build + install..."
    rm -f ./output/build/linux-custom/.stamp_{built,installed,target_installed,images_installed}
    rm -rf ./output/target/lib/modules/6.1.0-rc6
    make linux
    log "SMP artifacts:"
    ls -la output/images/zImage output/images/*.dtb 2>/dev/null || true
    find output/target/lib/modules -maxdepth 1 -type d -name '6.1*' 2>/dev/null || true
    if [ -f output/images/zImage ]; then
        cp -f output/images/zImage output/images/zImage.smp
        log "Saved output/images/zImage.smp — deploy with sa02m-kernel-deploy.sh install-smp"
    fi
}

cmd_build_kernel_only() {
    require_root
    require_br
    ensure_rt_patch
    log "Kernel-only build (skip full rootfs/Qt)..."
    # RT patch lives in buildroot/linux/patch-6.1-rc6-rt4.patch (auto-applied).
    # Clear BR2_LINUX_KERNEL_PATCH to avoid duplicate application.
    if [ -f .config ]; then
        sed -i 's|^BR2_LINUX_KERNEL_PATCH=.*|BR2_LINUX_KERNEL_PATCH=""|' .config
    fi
    rm -rf ./output/build/linux-custom
    log "linux extract + patch + configure (patch-first RT workflow)..."
    make linux-extract
    make linux-patch
    verify_rt_patch_applied || { log "ERROR: RT patch not applied after linux-patch" >&2; return 1; }
    make linux-configure
    if [ -f "$LINUX_DIR/.config" ]; then
        apply_kconfig_snippets
    else
        log "ERROR: no $LINUX_DIR/.config after linux-configure" >&2
        return 1
    fi
    kconfig_finalize_noninteractive rt || return 1
    log "linux build + install..."
    rm -f ./output/build/linux-custom/.stamp_{built,installed,target_installed,images_installed}
    make linux
    log "Artifacts:"
    ls -la output/images/zImage output/images/*.dtb 2>/dev/null || true
    find output/target/lib/modules -maxdepth 1 -type d -name '6.1*' 2>/dev/null || true
    if [ -f "$LINUX_DIR/.config" ]; then
        mkdir -p "$(dirname "$DEFCONFIG_OUT")"
        cp "$LINUX_DIR/.config" "$BR_DIR/$DEFCONFIG_OUT"
        log "Saved defconfig -> $DEFCONFIG_OUT"
    fi
}

cmd_build() {
    require_root
    require_br
    ensure_rt_patch
    log "Removing stale linux-custom (required after RT/Docker config change)..."
    rm -rf ./output/build/linux-custom
    log "Building (may take 1-2 hours)..."
    make -j"$(nproc)"
    log "Artifacts:"
    ls -la output/images/zImage output/images/*.dtb 2>/dev/null || true
    ls -d output/target/lib/modules/6.1.0-rc6-rt4 2>/dev/null || true
    if [ -f "$LINUX_DIR/.config" ]; then
        mkdir -p "$(dirname "$DEFCONFIG_OUT")"
        cp "$LINUX_DIR/.config" "$BR_DIR/$DEFCONFIG_OUT"
        log "Saved defconfig -> $DEFCONFIG_OUT"
    fi
}

cmd_help() {
    cat <<EOF
Usage: $0 {check|menuconfig|build|build-kernel|build-kernel-smp|kconfig}

RT (PREEMPT_RT) — patch-first workflow (Starterkit «Сборка ядра на ВМ.txt»):
  1) linux/patch-6.1-rc6-rt4.patch  →  make linux-patch  (NOT BR2_LINUX_KERNEL_PATCH)
  2) make linux-configure
  3) Expert + PREEMPT_RT=y + Docker kconfig (this script, step build-kernel)

SMP (no RT, Docker netfilter) — for web kernel switch / lower CPU load:
  build-kernel-smp  — extract → configure → SMP kconfig → linux (no RT patch)

  check         — verify Buildroot path, RT patch file, patch-applied markers
  menuconfig    — linux-menuconfig (RT options only after patch; prefer build-kernel)
  kconfig       — apply RT snippets only (linux-custom + RT patch must exist)
  build-kernel  — extract → patch → configure → kconfig → linux (RT)
  build-kernel-smp — extract → configure → SMP kconfig → linux (no RT)
  build         — full make (slow, sk_qt5 entire image)

Env: BR_DIR, RT_PATCH_URL, DEFCONFIG_OUT
EOF
}

main() {
    case "${1:-help}" in
        check) cmd_check ;;
        menuconfig) cmd_menuconfig ;;
        kconfig) require_root; require_br; apply_kconfig_snippets ;;
        build-kernel) cmd_build_kernel_only ;;
        build-kernel-smp) cmd_build_kernel_smp_only ;;
        build) cmd_build ;;
        *) cmd_help ;;
    esac
}

main "$@"
