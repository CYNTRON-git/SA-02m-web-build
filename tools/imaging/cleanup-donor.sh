#!/bin/bash
# cleanup-donor.sh — фазы 1–4 (без сброса ID / ssh keys).
# Шаги 5–7 (machine-id, host keys, firstrun) выполняет stream-after-cleanup.sh
# в той же ssh-сессии, что и zero-fill + dd — иначе новые подключения к sshd падают.
#
# Режимы:
#   --dry-run   (по умолчанию) только список путей/размеров, без удаления
#   --apply     реальное удаление (make-image.sh / capture-image-win.py передают явно)
#   --verbose   печать каждого пути
#   --report    du ключевых деревьев до/после (совместим с dry-run/apply)
#   --purge-update-state  удалить deployed_* / trusted keys (ОПАСНО, default off)
#   --purge-docker        apt purge docker.io (default off)
#   --purge-installers    удалить /root/*.deb|*.rpm и /root/mplc_update
#                         (по умолчанию НЕ трогаем — нужны для CODESYS/MPLC/CodeMeter)
#
# Fail-safe: без --apply ничего не удаляется.
# Установщики CODESYS/MPLC (/opt/vendor-installers, /root/*.deb) по умолчанию сохраняются.
set -euo pipefail
LC_ALL=C
export DEBIAN_FRONTEND=noninteractive

MODE=dry-run
VERBOSE=0
REPORT=0
PURGE_UPDATE_STATE=0
PURGE_DOCKER=0
PURGE_INSTALLERS=0
PARTIAL_FAIL=0
REPORT_FILE=/tmp/sa02m-cleanup-report.txt

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

die_usage() {
    cat >&2 <<'USAGE'
Usage: cleanup-donor.sh [--dry-run|--apply] [--verbose] [--report]
                        [--purge-update-state]
                        [--purge-docker] [--purge-installers]
  --dry-run   list candidates only (default; fail-safe)
  --apply     delete candidates + toolchain/apt/logs phases
  --verbose   print each path
  --report    print du of key trees before/after
  --purge-installers  also remove /root/*.deb|*.rpm and /root/mplc_update
USAGE
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) MODE=dry-run; shift ;;
        --apply) MODE=apply; shift ;;
        --verbose|-v) VERBOSE=1; shift ;;
        --report) REPORT=1; shift ;;
        --purge-update-state) PURGE_UPDATE_STATE=1; shift ;;
        --purge-docker) PURGE_DOCKER=1; shift ;;
        --purge-installers) PURGE_INSTALLERS=1; shift ;;
        -h|--help) die_usage ;;
        *) log "ERROR: unknown arg: $1"; die_usage ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    log "ERROR: cleanup-donor.sh должен запускаться от root"
    exit 1
fi

# --- DENY (жёсткий запрет) ---
# Trees: path itself or children (/boot, /boot/...) — not /bootloader
DENY_TREES=(
    /var/www/network_config
    /opt/mplc4
    /opt/codesys
    /opt/vendor-installers
    /var/lib/sa02m-alice
    /var/lib/sa02m-flasher
    /var/lib/mosquitto
    /etc/network
    /etc/nginx
    /etc/ssh
    /boot
    /lib/modules
    /root/.node-red
    /root/.ssh
)
# String prefixes: /opt/sa02m-* , /etc/sa02m* , /etc/mosquitto*
DENY_STR_PREFIXES=(
    /opt/sa02m-
    /opt/vendor-installers
    /etc/sa02m
    /etc/mosquitto
    /etc/machine-id
    /tmp/sa02m-gpioset-
    /tmp/systemd-private-
    /run/
)

# Literal paths that must never appear as ALLOW entries (config error → exit 3)
DENY_LITERALS=(
    /var/www/network_config
    /opt/mplc4
    /opt/codesys
    /opt/vendor-installers
    /var/lib/sa02m-alice
    /var/lib/sa02m-flasher
    /etc/network
    /etc/nginx
    /etc/ssh
    /boot
    /root/.node-red
    /root/.ssh
)

_path_denied() {
    local p="$1" d
    for d in "${DENY_TREES[@]}"; do
        case "$p" in
            "$d"|"$d"/*) return 0 ;;
        esac
    done
    for d in "${DENY_STR_PREFIXES[@]}"; do
        case "$p" in
            "$d"*) return 0 ;;
        esac
    done
    return 1
}

is_denied() {
    local path="$1" canon=""
    path="${path%/}"
    [ -z "$path" ] && return 0
    if [ -e "$path" ] || [ -L "$path" ]; then
        canon="$(readlink -f "$path" 2>/dev/null || true)"
    fi
    [ -z "$canon" ] && canon="$path"

    # --purge-update-state: allow only these update paths (still never alice/firmware)
    if [ "$PURGE_UPDATE_STATE" -eq 1 ]; then
        case "$canon" in
            /var/lib/sa02m-update/state/deployed_*) return 1 ;;
            /etc/sa02m-update/trusted-keys|/*) return 1 ;;
        esac
        case "$path" in
            /var/lib/sa02m-update/state/deployed_*) return 1 ;;
            /etc/sa02m-update/trusted-keys/*) return 1 ;;
        esac
    fi

    if _path_denied "$canon" || _path_denied "$path"; then
        return 0
    fi

    # Never wipe entire /home/<user> — only cache/history/Trash are allow-listed
    if [[ "$canon" =~ ^/home/[^/]+$ ]] || [[ "$path" =~ ^/home/[^/]+$ ]]; then
        return 0
    fi
    return 1
}

bytes_of() {
    local p="$1"
    if [ -e "$p" ] || [ -L "$p" ]; then
        du -sb "$p" 2>/dev/null | awk '{print $1}'
    else
        echo 0
    fi
}

human_mib() {
    local b="${1:-0}"
    awk -v b="$b" 'BEGIN { printf "%.1f", b/1024/1024 }'
}

df_root_used() {
    df -B1 / 2>/dev/null | awk 'NR==2 {print $3}'
}

du_path() {
    local p="$1"
    if [ -e "$p" ]; then
        du -sh "$p" 2>/dev/null | awk '{print $1}'
    else
        echo "-"
    fi
}

print_report_trees() {
    local label="$1"
    log "REPORT $label"
    printf '  /              used=%s\n' "$(df -h / | awk 'NR==2 {print $3"/"$2" ("$5")"}')" >&2
    printf '  /root          %s\n' "$(du_path /root)" >&2
    printf '  /var/log       %s\n' "$(du_path /var/log)" >&2
    printf '  /var/log.hdd   %s\n' "$(du_path /var/log.hdd)" >&2
    printf '  /var/cache/apt %s\n' "$(du_path /var/cache/apt)" >&2
    printf '  /tmp           %s\n' "$(du_path /tmp)" >&2
}

CANDIDATES=()
DENY_SKIPPED=()
DELETED_COUNT=0
DELETED_BYTES=0
EST_BYTES=0

candidate_seen() {
    local p="$1" i
    for i in "${CANDIDATES[@]+"${CANDIDATES[@]}"}"; do
        [ "$i" = "$p" ] && return 0
    done
    return 1
}

add_candidate() {
    local p="$1" sz
    [ -e "$p" ] || [ -L "$p" ] || return 0
    candidate_seen "$p" && return 0
    if is_denied "$p"; then
        DENY_SKIPPED+=("$p")
        log "WARN DENY_SKIP: $p"
        return 0
    fi
    CANDIDATES+=("$p")
    sz="$(bytes_of "$p")"
    EST_BYTES=$((EST_BYTES + sz))
    if [ "$VERBOSE" -eq 1 ]; then
        log "  candidate $(human_mib "$sz") MiB  $p"
    fi
}

expand_globs() {
    local pat matches m
    shopt -s nullglob
    for pat in "$@"; do
        # shellcheck disable=SC2206
        matches=($pat)
        for m in "${matches[@]+"${matches[@]}"}"; do
            add_candidate "$m"
        done
    done
    shopt -u nullglob
}

# Explicit allow literals (config sanity vs DENY)
ALLOW_LITERALS=(
    /root/backup
    /root/mplc_cyntron_build
    /root/sa02m-deploy
    /root/sa02m-deploy.tar.gz
    /root/sa02m-web-build
    /root/deploy.tar.gz
    /root/cursor_build.swap
    /root/u-boot-sunxi-with-spl.bin
    /root/40-usb_modeswitch.rules
    /root/-d
    /root/nul
    /root/NUL
    /root/f10-backup
    /root/.npm
    /root/.cache
    /root/.bash_history
    /root/.viminfo
    /root/.lesshst
    /root/.local/share/Trash
)

check_allow_config() {
    local lit
    for lit in "${DENY_LITERALS[@]}"; do
        case " ${ALLOW_LITERALS[*]} " in
            *" $lit "*)
                log "ERROR: ALLOW list contains DENY path: $lit"
                exit 3
                ;;
        esac
    done
}

check_allow_config

collect_junk() {
    log "[2/4] Сбор кандидатов мусора (/root, caches, staging)"

    # A. Staging деплоев и архивы
    add_candidate /root/sa02m-deploy
    add_candidate /root/sa02m-deploy.tar.gz
    add_candidate /root/sa02m-web-build
    add_candidate /root/deploy.tar.gz
    expand_globs \
        '/root/sa02m-deploy-*' \
        '/root/sa02m-install-*' \
        '/root/deploy-*.tar' \
        '/root/deploy-*.tar.gz'

    # B. Кеши разработчика
    add_candidate /root/.npm
    add_candidate /root/.cache
    add_candidate /root/.local/share/Trash
    add_candidate /root/.bash_history
    add_candidate /root/.viminfo
    add_candidate /root/.lesshst

    local h
    shopt -s nullglob
    for h in /home/*/; do
        [ -d "$h" ] || continue
        add_candidate "${h}.cache"
        add_candidate "${h}.bash_history"
        add_candidate "${h}.local/share/Trash"
    done
    shopt -u nullglob

    # C. Установщики CODESYS/MPLC/CodeMeter — по умолчанию НЕ удаляем.
    #    /opt/vendor-installers в DENY; /root/*.deb и mplc_update — только с --purge-installers.
    if [ "$PURGE_INSTALLERS" -eq 1 ]; then
        log "    --purge-installers: кандидаты /root/*.deb|*.rpm, /root/mplc_update"
        expand_globs '/root/*.deb' '/root/*.rpm'
        add_candidate /root/mplc_update
    fi

    # D. Стендовые бэкапы / артефакты (не установщики)
    add_candidate /root/backup
    add_candidate /root/mplc_cyntron_build
    add_candidate /root/f10-backup
    add_candidate /root/cursor_build.swap
    add_candidate /root/u-boot-sunxi-with-spl.bin
    add_candidate /root/40-usb_modeswitch.rules
    add_candidate '/root/-d'
    add_candidate /root/nul
    add_candidate /root/NUL
    add_candidate '/root/ystemd-analyze critical-chain'
    expand_globs \
        '/root/mplc_backup*' \
        '/root/www-backup-*.tgz' \
        '/root/www-backup-*.tar.gz' \
        '/root/flasher-backup-*.tgz' \
        '/root/preinstall-backup-*.tgz' \
        '/root/opt-bridge-backup-*' \
        '/root/zImage*.bak*' \
        '/root/modules-*.bak*'

    # F. Update staging + in-flight transaction state (ephemeral). A donor must
    # not ship an interrupted update: a baked transaction.json makes every flashed
    # board run sa02m-update-recover on boot. state/deployed_* and trusted-keys
    # stay preserved (only --purge-update-state removes them).
    expand_globs \
        '/var/lib/sa02m-update/staging/*' \
        '/var/lib/sa02m-update/incoming/*' \
        '/var/lib/sa02m-update/runner/*' \
        '/var/lib/sa02m-update/rollback/*' \
        '/var/lib/sa02m-update/transaction.json' \
        '/var/lib/sa02m-update/update.lock' \
        '/var/lib/sa02m-update/update.log' \
        '/tmp/sa02m-update*' \
        '/tmp/sa02m-deploy*' \
        '/tmp/sa02m-ota*' \
        '/tmp/sa02m-cleanup-report.txt'
    # /tmp/sa02m-gpioset-*.pid|.state — runtime USB power holds; не трогать

    if [ "$PURGE_UPDATE_STATE" -eq 1 ]; then
        expand_globs \
            '/var/lib/sa02m-update/state/deployed_*' \
            '/etc/sa02m-update/trusted-keys/*'
    fi
}

remove_one() {
    local p="$1" sz
    if is_denied "$p"; then
        DENY_SKIPPED+=("$p")
        log "WARN DENY_SKIP at rm: $p"
        return 0
    fi
    sz="$(bytes_of "$p")"
    if [ "$VERBOSE" -eq 1 ]; then
        log "  rm -rf $p"
    fi
    if rm -rf -- "$p" 2>/dev/null; then
        DELETED_COUNT=$((DELETED_COUNT + 1))
        DELETED_BYTES=$((DELETED_BYTES + sz))
    else
        log "WARN: failed to remove $p"
        PARTIAL_FAIL=1
    fi
}

apply_junk() {
    local p
    for p in "${CANDIDATES[@]+"${CANDIDATES[@]}"}"; do
        remove_one "$p"
    done
}

list_log_hdd_candidates() {
    [ -d /var/log.hdd ] || return 0
    local f
    shopt -s nullglob
    for f in /var/log.hdd/*.1 /var/log.hdd/*.gz /var/log.hdd/*.1.gz; do
        [ -f "$f" ] || continue
        is_denied "$f" && continue
        add_candidate "$f"
    done
    shopt -u nullglob
}

truncate_log_hdd() {
    [ -d /var/log.hdd ] || return 0
    local f
    shopt -s nullglob
    for f in /var/log.hdd/*.1 /var/log.hdd/*.gz /var/log.hdd/*.1.gz; do
        [ -f "$f" ] || continue
        is_denied "$f" && continue
        if [ "$VERBOSE" -eq 1 ]; then
            log "  truncate/rm log.hdd: $f"
        fi
        truncate -s 0 "$f" 2>/dev/null || rm -f -- "$f" 2>/dev/null || true
    done
    shopt -u nullglob
}

purge_toolchain() {
    log "[3/4] Удаление тулчейна (build tools / kernel headers)"
    local PURGE_PATTERNS=(
        'build-essential' 'dkms' 'make'
        'gcc' 'gcc-1?' 'g++*' 'cpp' 'cpp-1?'
        'gcc-arm-linux-gnueabihf' 'gcc-13-arm-linux-gnueabihf'
        'libgcc-*-dev' 'libstdc++-*-dev'
        'linux-headers-*' 'linux-source-*'
    )
    local PURGE_LIST=()
    local pat pkg
    for pat in "${PURGE_PATTERNS[@]}"; do
        while IFS= read -r pkg; do
            [ -n "$pkg" ] && PURGE_LIST+=("$pkg")
        done < <(dpkg-query -W -f='${Package} ${Status}\n' "$pat" 2>/dev/null \
            | awk '$3 == "installed" { print $1 }' || true)
    done
    if [ "$PURGE_DOCKER" -eq 1 ]; then
        while IFS= read -r pkg; do
            [ -n "$pkg" ] && PURGE_LIST+=("$pkg")
        done < <(dpkg-query -W -f='${Package} ${Status}\n' 'docker.io' 'docker-ce' 'docker-ce-cli' 2>/dev/null \
            | awk '$3 == "installed" { print $1 }' || true)
    fi
    if [ "${#PURGE_LIST[@]}" -eq 0 ]; then
        log "    нет пакетов под удаление"
        return 0
    fi
    if [ "$MODE" != apply ]; then
        log "    dry-run: apt-get purge (${#PURGE_LIST[@]} pkgs): ${PURGE_LIST[*]}"
        return 0
    fi
    apt-get purge -y --auto-remove "${PURGE_LIST[@]}" || true
    apt-get autoremove --purge -y || true
}

# Safe /tmp cleanup: never wipe whole /tmp (gpioset USB-power holds, sockets, systemd-private).
clean_tmp_safe() {
    local f
    shopt -s nullglob
    for f in \
        /tmp/sa02m-update* \
        /tmp/sa02m-deploy* \
        /tmp/sa02m-ota* \
        /tmp/sa02m-cleanup-report.txt \
        /tmp/*.deb \
        /tmp/*.rpm \
        /var/tmp/sa02m-update* \
        /var/tmp/sa02m-deploy* \
        /var/tmp/sa02m-ota*
    do
        [ -e "$f" ] || [ -L "$f" ] || continue
        if is_denied "$f"; then
            log "WARN DENY_SKIP tmp: $f"
            continue
        fi
        [ "$VERBOSE" -eq 1 ] && log "  rm tmp junk: $f"
        rm -rf -- "$f" 2>/dev/null || true
    done
    shopt -u nullglob
}

clean_apt_logs_tmp() {
    log "[4/4] Очистка apt cache, journald, логов, безопасный /tmp"
    if [ "$MODE" != apply ]; then
        log "    dry-run: apt-get clean; purge lists; journal vacuum; truncate logs; safe /tmp junk only"
        return 0
    fi
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    mkdir -p /var/lib/apt/lists/partial
    journalctl --rotate 2>/dev/null || true
    journalctl --vacuum-time=1s 2>/dev/null || true
    find /var/log -type f \( -name '*.log' -o -name '*.log.*' -o -name '*.gz' \) \
         -exec truncate -s 0 {} \; 2>/dev/null || true
    find /var/log -type f -regex '.*\.[0-9]+\(\.gz\)?$' -delete 2>/dev/null || true
    truncate_log_hdd
    clean_tmp_safe
}

install_regen_ssh_unit() {
    # Сервис regen ставим заранее (до сброса keys в stream-after-cleanup.sh),
    # чтобы после reboot донора sshd мог подняться, если сессия dd прервётся.
    cat > /etc/systemd/system/regen-ssh-host-keys.service <<'EOF'
[Unit]
Description=Regenerate SSH host keys on first boot (after image clone)
DefaultDependencies=no
After=local-fs.target
Before=ssh.service sshd.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable regen-ssh-host-keys.service 2>/dev/null || true
}

# --- main ---
log "cleanup-donor.sh mode=$MODE verbose=$VERBOSE report=$REPORT"
log "[1/4] Состояние до cleanup"
df -hT / | sed 's/^/    /' >&2
BEFORE_ROOT_USED="$(df_root_used)"
BEFORE_ROOT_DU="$(bytes_of /root)"
[ "$REPORT" -eq 1 ] && print_report_trees BEFORE

collect_junk
list_log_hdd_candidates

log "CANDIDATES: ${#CANDIDATES[@]} paths, ~$(human_mib "$EST_BYTES") MiB"
if [ "$VERBOSE" -eq 0 ]; then
    for _p in "${CANDIDATES[@]+"${CANDIDATES[@]}"}"; do
        log "  $(human_mib "$(bytes_of "$_p")") MiB  $_p"
    done
fi

if [ "$MODE" = apply ]; then
    apply_junk
else
    log "dry-run: удаление не выполняется (передайте --apply)"
fi

purge_toolchain
clean_apt_logs_tmp

if [ "$MODE" = apply ]; then
    install_regen_ssh_unit
fi

AFTER_ROOT_USED="$(df_root_used)"
AFTER_ROOT_DU="$(bytes_of /root)"
[ "$REPORT" -eq 1 ] && print_report_trees AFTER

{
    echo "BEFORE: root_used=$(human_mib "$BEFORE_ROOT_USED") MiB /root=$(human_mib "$BEFORE_ROOT_DU") MiB"
    if [ "$MODE" = apply ]; then
        echo "DELETED: $DELETED_COUNT paths, ~$(human_mib "$DELETED_BYTES") MiB (measured)"
    else
        echo "DELETED: 0 paths (dry-run), would remove ~$(human_mib "$EST_BYTES") MiB (estimated)"
    fi
    echo "AFTER:  root_used=$(human_mib "$AFTER_ROOT_USED") MiB /root=$(human_mib "$AFTER_ROOT_DU") MiB"
    if [ "${#DENY_SKIPPED[@]}" -gt 0 ]; then
        echo "DENY_SKIPPED: ${DENY_SKIPPED[*]}"
    else
        echo "DENY_SKIPPED: (none)"
    fi
    echo "MODE=$MODE"
} | tee "$REPORT_FILE" >&2

log "отчёт: $REPORT_FILE"
log "cleanup фазы 1–4 завершены (mode=$MODE)"
df -hT / | sed 's/^/    /' >&2

if [ "$PARTIAL_FAIL" -ne 0 ]; then
    exit 2
fi
exit 0
