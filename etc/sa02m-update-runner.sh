#!/bin/bash
# SA-02m update apply-core — installed as /usr/local/libexec/sa02m-update-runner.
#
# Network-free file/GitHub-shared apply path (plan §2.5–2.6):
#   - no install.sh / scripts/0*.sh / apt-get / git clone
#   - no rsync --delete on live trees
#   - no tar -xf -C /  (rollback restores journal backup paths only)
#   - imaging lock + the runtime watchdog held off (read back) for the apply window
#   - self-copy re-exec into $STATEDIR/runner/<txn>/runner before deploy
#
# Commands: apply (default) | recover
# shellcheck shell=bash
set -euo pipefail

STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
LOCKFILE="$STATEDIR/update.lock"
TXN_FILE="$STATEDIR/transaction.json"
LOGFILE="$STATEDIR/update.log"
PACKAGE_DEFAULT="$STATEDIR/incoming/package.sa02m"
IMAGING_LOCK="${SA02M_IMAGING_LOCK:-/run/sa02m-imaging.lock}"
VALIDATE_PY="${SA02M_UPDATE_VALIDATE_PY:-/opt/sa02m-update/lib/validate_package.py}"
VERSION_FILE="${SA02M_WEB_VERSION_FILE:-/var/www/network_config/VERSION}"
LEGACY_STATEDIR="${SA02M_WEB_BUILD_STATEDIR:-/var/lib/sa02m-web-build}"
# RuntimeWatchdogUSec (µs) READ BACK from the manager before the apply window
# held it off; empty = nothing was held, so nothing is restored. The old
# SA02M_RUNTIME_WATCHDOG_SEC seam restored a HARDCODED 15s over whatever the
# board really had, and only if the write worked at all (it never checked).
RUNTIME_WDT_PREV=""

# UPDATER_VERSION — what this runner reports against a package's min_updater.
# One home for "which release installed this runner": the stamp
# $STATEDIR/runner.version, written by every site that installs the runner
# binary — scripts/03-webserver.sh and scripts/update-www-only.sh
# (sa02m_stamp_runner_version in scripts/lib.sh) and this runner itself when a
# manifest deploys $RUNNER_BIN_DST (stamp_runner_version_after_deploy, below,
# journalled so an E_APPLY/E_HEALTH rollback restores the old stamp with the
# old binary). The stamp exists because the deployed VERSION file alone
# over-reports: scripts/update-www-only.sh on a delivery WITHOUT etc/ refreshes
# VERSION and not the runner, and such a board then claimed a runner it did
# not have (ship review 1.0.6.39, item 6). Derivation order: stamp → VERSION
# (a pre-1.0.6.40 board has no stamp yet; VERSION is right there because the
# runner ships in the same overlay) → the literal floor the first runner ever
# reported (a board with neither is a broken install, not an old one). The
# literal the derivation replaced was a 1.0.5.66 stamp every board reported
# forever, which made the packer's MIN_UPDATER unraisable (audit 2026-09-08,
# D2). Read once at start: apply deploys the new VERSION and stamp later, but
# the process running the compat gate is still the OLD runner, so the value is
# right. The env override stays for harnesses and the bench.
UPDATER_VERSION_FALLBACK=1.0.5.66
RUNNER_VERSION_FILE="$STATEDIR/runner.version"
RUNNER_BIN_DST="${SA02M_UPDATE_RUNNER_DST:-/usr/local/libexec/sa02m-update-runner}"
read_version_line() {  # $1=file → first "x.y[.z[.w]]" line, CRLF tolerated; empty when none
    if [ -f "$1" ]; then
        tr -d '\r' <"$1" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true
    fi
}
derive_updater_version() {
    local v=""
    v=$(read_version_line "$RUNNER_VERSION_FILE")
    [ -n "$v" ] || v=$(read_version_line "$VERSION_FILE")
    printf '%s\n' "${v:-$UPDATER_VERSION_FALLBACK}"
}
UPDATER_VERSION="${SA02M_UPDATER_VERSION:-$(derive_updater_version)}"

# Runner-owned preserve list (not in signed manifest) — plan §2.4.
# shellcheck disable=SC2034
PRESERVE_PATHS=(
    /etc/sa02m_web.env
    /etc/sa02m_*.conf
    /etc/network/interfaces.d/
    /etc/nginx/.htpasswd
    /etc/nginx/sites-enabled/000-sa02m-network_config
    /etc/sa02m-device-templates/
    /etc/sa02m-cloud/
    /var/lib/sa02m-flasher/
    /var/lib/sa02m-update/
    /etc/sa02m-alice-client.conf
    /etc/sa02m-alice-devices.conf
    /var/lib/sa02m-alice/
)

CMD="${1:-apply}"
IMAGING_HELD=0
LOCK_HELD=0

log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    mkdir -p "$STATEDIR" 2>/dev/null || true
    printf '%s %s\n' "$ts" "$*" | tee -a "$LOGFILE" >/dev/null
    printf '%s %s\n' "$ts" "$*" >&2
}

die() {
    local code=$1
    shift
    log "ERROR [$code]: $*"
    txn_patch "stage=error" "result=failed" "error_code=$code" "error_message=$*" "finished_at=$(utc_now)" || true
    cleanup_imaging_lock || true
    exit 1
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

ensure_dirs() {
    mkdir -p \
        "$STATEDIR/incoming" \
        "$STATEDIR/staging" \
        "$STATEDIR/rollback" \
        "$STATEDIR/state" \
        "$STATEDIR/runner" \
        "$STATEDIR/backup-export"
    chmod 0755 "$STATEDIR" 2>/dev/null || true
    chmod 0770 "$STATEDIR/incoming" 2>/dev/null || true
    chmod 0750 "$STATEDIR/staging" "$STATEDIR/rollback" "$STATEDIR/state" "$STATEDIR/runner" 2>/dev/null || true
}

migrate_legacy_state() {
    local f
    for f in deployed_commit deployed_at deployed_version; do
        if [ -f "$LEGACY_STATEDIR/$f" ] && [ ! -f "$STATEDIR/state/$f" ]; then
            install -m 644 "$LEGACY_STATEDIR/$f" "$STATEDIR/state/$f" 2>/dev/null || true
            log "legacy migrate: $LEGACY_STATEDIR/$f -> state/$f"
        fi
    done
    printf '%s\n' "$UPDATER_VERSION" >"$STATEDIR/state/updater_version"
    chmod 644 "$STATEDIR/state/updater_version" 2>/dev/null || true
}

acquire_lock() {
    mkdir -p "$STATEDIR"
    exec 9>"$LOCKFILE"
    if ! flock -n 9; then
        log "ERROR [E_LOCK]: another update holds $LOCKFILE"
        exit 1
    fi
    printf '%s\n' "$$" >&9
    LOCK_HELD=1
}

# --- transaction.json helpers (temp → fdatasync → rename) --------------------

txn_exists() { [ -f "$TXN_FILE" ]; }

txn_get() {
    local key=$1
    python3 -c 'import json,sys
p,k=sys.argv[1],sys.argv[2]
try:
  d=json.load(open(p,encoding="utf-8"))
except Exception:
  sys.exit(0)
v=d.get(k,"")
if v is None: v=""
if isinstance(v,bool):
  print("true" if v else "false")
elif isinstance(v,(dict,list)):
  print(json.dumps(v,ensure_ascii=False))
else:
  print(v)
' "$TXN_FILE" "$key" 2>/dev/null || true
}

txn_patch() {
    # args: key=value ...
    TXN_FILE="$TXN_FILE" python3 - "$@" <<'PY'
import json, os, sys, time

path = os.environ["TXN_FILE"]
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

def coerce(v):
    if v in ("true", "false"):
        return v == "true"
    if v.isdigit():
        return int(v)
    if v in ("null", ""):
        return None if v == "null" else v
    try:
        if v.startswith("{") or v.startswith("["):
            return json.loads(v)
    except Exception:
        pass
    return v

for arg in sys.argv[1:]:
    if "=" not in arg:
        continue
    k, v = arg.split("=", 1)
    data[k] = coerce(v)

now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
data.setdefault("schema_version", 1)
data["updated_at"] = now

tmp = path + ".tmp.%d" % os.getpid()
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    f.write("\n")
    f.flush()
    os.fdatasync(f.fileno())
os.replace(tmp, path)
dirfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
try:
    os.fsync(dirfd)
finally:
    os.close(dirfd)
PY
}

# --- imaging lock / watchdog (mandatory, plan §2.6) --------------------------

# ── BEGIN sa02m-runtime-watchdog (shared block — keep BYTE-IDENTICAL) ──────
# One home for "hold the systemd manager's hardware watchdog off while the
# live filesystem is being rewritten". Two callers cannot share a file:
# install.sh sources scripts/lib.sh out of an extracted tree, while
# etc/sa02m-update-runner.sh runs standalone on the device, where scripts/ is
# not deployed. So the block is duplicated by construction and pinned
# byte-for-byte by scripts/dev/test-watchdog-hold.sh (the `cmp` idiom
# .ai-dev/quality/checks/watchdog-cap.sh already uses for the policy file).
# Nothing here logs: the two callers have different log() signatures — each
# logs what it got back.
#
# Policy home of the value being held off: etc/systemd/sa02m-watchdog.conf
# `[Manager] RuntimeWatchdogSec=15s` → /etc/systemd/system.conf.d/. At runtime
# the manager exposes it as the D-Bus property RuntimeWatchdogUSec (µs); on the
# bench board it reads 15000000. That property is WRITABLE only on systemd
# >= 250 — on an older manager `systemctl set-property --runtime Manager …`
# and the bus write are both silent no-ops. Nothing here trusts a write: the
# value is READ BACK and the caller is told what is really in force (the
# previous guard, etc/sa02m-update-runner.sh:216 before 1.0.6.41, logged
# «RuntimeWatchdogSec=0» without ever checking — quality-gate-rigor.md).
# The bus write is an OVERRIDE, so it survives the `daemon-reload` the modules
# do; restoring writes the previous value back as an override (the configured
# policy applies again from the next boot).
# `systemctl daemon-reexec` would also re-read the config and is deliberately
# NOT used here: re-execing PID 1 mid-install is the leading hard-reset
# candidate of the 8D run-note (.ai-dev/8d/bench-136-reset.md D4).
sa02m_runtime_watchdog_usec() {   # prints RuntimeWatchdogUSec in µs; rc=1 when unreadable
    local v=""
    command -v busctl >/dev/null 2>&1 || return 1
    v=$(busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
            org.freedesktop.systemd1.Manager RuntimeWatchdogUSec 2>/dev/null) || return 1
    v=${v##* }                      # "t 15000000" → "15000000"
    case "$v" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$v"
}

# <µs> → prints the value ACTUALLY in force after the attempt; rc=0 ONLY when
# that equals the requested one, rc=1 when the manager cannot be read at all
# (no busctl, chroot, dead D-Bus), rc=2 on a bad argument.
sa02m_runtime_watchdog_set() {
    local want=${1:-} now
    case "$want" in ''|*[!0-9]*) return 2 ;; esac
    now=$(sa02m_runtime_watchdog_usec) || return 1
    if [ "$now" != "$want" ] && command -v busctl >/dev/null 2>&1; then
        busctl set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
            org.freedesktop.systemd1.Manager RuntimeWatchdogUSec t "$want" >/dev/null 2>&1 || true
        now=$(sa02m_runtime_watchdog_usec) || return 1
    fi
    if [ "$now" != "$want" ] && command -v systemctl >/dev/null 2>&1; then
        systemctl set-property --runtime Manager "RuntimeWatchdogSec=${want}us" >/dev/null 2>&1 || true
        now=$(sa02m_runtime_watchdog_usec) || return 1
    fi
    printf '%s\n' "$now"
    [ "$now" = "$want" ]
}
# ── END sa02m-runtime-watchdog ─────────────────────────────────────────────

install_imaging_lock() {
    date -Iseconds >"$IMAGING_LOCK"
    sync
    systemctl stop net-watchdog sa02m-watchdog-feed 2>/dev/null || true
    local now=""
    RUNTIME_WDT_PREV=$(sa02m_runtime_watchdog_usec 2>/dev/null) || RUNTIME_WDT_PREV=""
    if [ -n "$RUNTIME_WDT_PREV" ] && [ "$RUNTIME_WDT_PREV" != 0 ]; then
        if now=$(sa02m_runtime_watchdog_set 0); then
            log "imaging lock installed ($IMAGING_LOCK); runtime watchdog held off (was ${RUNTIME_WDT_PREV}us, now ${now}us)"
        else
            RUNTIME_WDT_PREV=""
            log "imaging lock installed ($IMAGING_LOCK); WARNING: runtime watchdog still ${now:-unknown}us — the manager refused the change, the apply runs with it armed"
        fi
    else
        log "imaging lock installed ($IMAGING_LOCK); runtime watchdog ${RUNTIME_WDT_PREV:-unreadable} — nothing to hold off"
    fi
    IMAGING_HELD=1
    txn_patch "imaging_lock=true" || true
}

cleanup_imaging_lock() {
    if [ "$IMAGING_HELD" != "1" ] && [ ! -f "$IMAGING_LOCK" ]; then
        return 0
    fi
    local now="" wdt="не менялся"
    if [ -n "$RUNTIME_WDT_PREV" ]; then
        if now=$(sa02m_runtime_watchdog_set "$RUNTIME_WDT_PREV"); then
            wdt="restored to ${now}us"
        else
            wdt="RESTORE FAILED: wanted ${RUNTIME_WDT_PREV}us, in force ${now:-unknown}us"
        fi
        RUNTIME_WDT_PREV=""
    fi
    systemctl start net-watchdog 2>/dev/null || true
    # sa02m-watchdog-feed is optional; leave stopped if it was inactive.
    rm -f "$IMAGING_LOCK"
    IMAGING_HELD=0
    txn_patch "imaging_lock=false" || true
    log "imaging lock cleared; runtime watchdog $wdt"
}

# --- cancel / preflight ------------------------------------------------------

cancel_requested() { [ -f "$STATEDIR/cancel" ]; }

honour_cancel_if_early() {
    local stage
    stage=$(txn_get stage)
    case "$stage" in
        uploaded|validating|backing_up)
            if cancel_requested; then
                rm -f "$STATEDIR/cancel"
                wipe_incoming_staging
                txn_patch "stage=cancelled" "result=cancelled" "error_code=E_CANCEL" \
                    "error_message=cancelled by operator" "finished_at=$(utc_now)"
                cleanup_imaging_lock || true
                log "cancelled at stage=$stage"
                exit 0
            fi
            ;;
    esac
}

wipe_incoming_staging() {
    local txn
    txn=$(txn_get id)
    rm -f "$STATEDIR/incoming/package.partial" 2>/dev/null || true
    # Keep package.sa02m on cancel-after-upload? Plan recover wipes incoming on
    # uploaded/validating/cancelled. Apply-cancel does the same for early stages.
    rm -f "$PACKAGE_DEFAULT" 2>/dev/null || true
    if [ -n "$txn" ]; then
        rm -rf "$STATEDIR/staging/$txn" "$STATEDIR/runner/$txn" 2>/dev/null || true
    fi
}

preflight_commands() {
    local cmds=(
        /bin/bash /usr/bin/python3 /usr/bin/openssl
        /usr/bin/tar /usr/bin/gzip /usr/bin/sha256sum
        /usr/bin/flock /usr/bin/systemctl /usr/bin/install
        /usr/local/libexec/sa02m-update-runner
    )
    local c
    for c in "${cmds[@]}"; do
        if [ ! -x "$c" ] && [ ! -e "$c" ]; then
            # During bootstrap the installed path may be this script under /etc
            # before 03-webserver copies it; accept $0 as the runner.
            if [ "$c" = /usr/local/libexec/sa02m-update-runner ]; then
                continue
            fi
            die E_CMD "missing command: $c"
        fi
    done
}

preflight_space() {
    local pkg=$1
    local pkg_size free need
    pkg_size=$(stat -c%s "$pkg" 2>/dev/null || stat -f%z "$pkg")
    free=$(df -B1 --output=avail "$STATEDIR" | tail -1 | tr -d ' ')
    need=$((pkg_size * 3))
    if [ "$need" -lt 67108864 ]; then
        need=67108864
    fi
    if [ "${free:-0}" -lt "$need" ]; then
        die E_SPACE "free_bytes=$free need=$need"
    fi
}

# --- self-copy re-exec before deploy -----------------------------------------

self_reexec_before_deploy() {
    if [ "${SA02M_UPDATE_REEXEC:-0}" = "1" ]; then
        return 0
    fi
    local txn dest src
    txn=$(txn_get id)
    [ -n "$txn" ] || die E_INTERNAL "missing transaction id before re-exec"
    dest="$STATEDIR/runner/$txn/runner"
    mkdir -p "$STATEDIR/runner/$txn"
    src=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")
    install -m 755 "$src" "$dest"
    sync
    log "self-copy re-exec: $dest"
    export SA02M_UPDATE_REEXEC=1
    export SA02M_UPDATE_STATEDIR="$STATEDIR"
    exec "$dest" apply
}

# --- GitHub overlay handoff (source=github, unsigned internal manifest) ------

prepare_github_overlay() {
    local txn=$1
    local overlay_src
    overlay_src=$(txn_get overlay_path)
    [ -n "$overlay_src" ] && [ -d "$overlay_src" ] || die E_TAR "github overlay_path missing"
    local stage_dir="$STATEDIR/staging/$txn"
    mkdir -p "$stage_dir/overlay" "$stage_dir/backups" "$stage_dir/meta"
    # Materialize overlay (repo checkout) into staging for per-file deploy.
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "$overlay_src/" "$stage_dir/overlay/" || die E_APPLY "rsync overlay failed"
    else
        rm -rf "$stage_dir/overlay"
        mkdir -p "$stage_dir/overlay"
        cp -a "$overlay_src/." "$stage_dir/overlay/" || die E_APPLY "cp overlay failed"
    fi
    OVERLAY="$stage_dir/overlay" META="$stage_dir/meta" \
    TARGET_VER="$(txn_get target_version)" TARGET_COMMIT="$(txn_get target_commit)" \
    PREV_VER="$(txn_get previous_version)" \
    python3 <<'PY' || die E_COMPAT "github manifest build failed"
import json, os, re
from pathlib import Path

overlay = Path(os.environ["OVERLAY"])
meta = Path(os.environ["META"])
meta.mkdir(parents=True, exist_ok=True)

DST_RE = re.compile(
    r"^/(var/www/network_config/|usr/local/(sbin|lib|libexec)/|"
    r"opt/sa02m-[a-z0-9-]+/|opt/mplc4/|"
    r"etc/systemd/system/sa02m-|"
    r"etc/nginx/|etc/tmpfiles\.d/|etc/sudoers\.d/|"
    r"etc/default/sa02m-|"
    r"etc/sa02m-update/trusted-keys/|"
    r"etc/dhcp/dhclient-exit-hooks\.d/eth1-default-route$)"
)
# Git-tracked MPLC plugins (firmware/mplc4/) → /opt/mplc4/<name>. Closed set —
# never map arbitrary firmware/* into the live RT tree.
MPLC_OTA_PLUGINS = frozenset({
    "mplc_cyntron.so",
    "mplc_protocol_fast_modbus.so",
})

def map_dst(rel: str):
    rel = rel.replace("\\", "/").lstrip("./")
    if rel.startswith("www/network_config/"):
        # rel is www/network_config/... → /var/www/network_config/...
        return "/var/" + rel
    if rel.startswith("opt/sa02m-"):
        return "/" + rel
    if rel.startswith("usr/local/"):
        return "/" + rel
    if rel.startswith("etc/sa02m-update/trusted-keys/"):
        return "/" + rel
    if rel.startswith("etc/tmpfiles.d/"):
        return "/" + rel
    if rel.startswith("etc/sudoers.d/"):
        return "/" + rel
    if rel.startswith("etc/systemd/system/") and "sa02m-" in rel:
        return "/" + rel
    if rel.startswith("etc/systemd/") and "sa02m-" in Path(rel).name:
        return "/etc/systemd/system/" + Path(rel).name
    name = Path(rel).name
    # MPLC RT plugins (licence publisher + Fast Modbus) — otherwise web OTA
    # never refreshes them and the licence card falls back to a log scrape.
    if rel.startswith("firmware/mplc4/") and name in MPLC_OTA_PLUGINS:
        return "/opt/mplc4/" + name
    # Helpers etc/sa02m-*.sh → /usr/local/sbin or lib
    if rel.startswith("etc/") and name.startswith("sa02m-"):
        if name.endswith("-lib.sh") or name in ("sa02m-web-build-lib.sh", "sa02m-web-auth-lib.sh"):
            return "/usr/local/lib/" + name
        # Privileged helpers sudo grants by an explicit path must land at THAT
        # path — a stripped extension deploys a twin nobody calls and leaves the
        # granted file stale (audit B1 deploy gap). Gate: sudoers-pin-contract.
        if name == "sa02m-mqtt-external-info.py":
            return "/usr/local/sbin/" + name
        if name.endswith(".sh") or name in (
            "sa02m-web-update-check", "sa02m-web-update-apply",
            "sa02m-set-cpu-profile", "sa02m-set-storage-auto-format",
        ):
            base = name[:-3] if name.endswith(".sh") and name not in (
                "sa02m-web-reboot.sh", "sa02m-web-restart-services.sh",
                "sa02m-web-service-ctl.sh", "sa02m-web-root-cmd.sh",
                "sa02m-rs485-stats.sh", "sa02m-kernel-select.sh",
                "sa02m-cpu-profile.sh", "sa02m-web-backup.sh",
                "sa02m-restore-backup.sh",
            ) else name
            # Keep .sh for scripts that install with extension
            if name in (
                "sa02m-web-reboot.sh", "sa02m-web-restart-services.sh",
                "sa02m-web-service-ctl.sh", "sa02m-web-root-cmd.sh",
                "sa02m-rs485-stats.sh", "sa02m-kernel-select.sh",
                "sa02m-cpu-profile.sh", "sa02m-web-backup.sh",
                "sa02m-restore-backup.sh", "sa02m-update-runner.sh",
                "sa02m-update-inspect.sh", "sa02m-factory-reset-runner.sh",
                "sa02m-iface-conf-write.sh", "sa02m-usb-power.sh",
                "sa02m-ensure-eth1-dhcp-hook.sh",
                "sa02m-conf-rm.sh", "sa02m-mplc-project-deploy.sh",
            ):
                if "runner" in name or "inspect" in name or "factory-reset" in name:
                    return "/usr/local/libexec/" + name.replace(".sh", "")
                return "/usr/local/sbin/" + name
            return "/usr/local/sbin/" + base
    # eth1 DHCP default-route exit-hook (panel/installer parity on OTA boards)
    if rel == "etc/dhcp/dhclient-exit-hooks.d/eth1-default-route":
        return "/etc/dhcp/dhclient-exit-hooks.d/eth1-default-route"
    return None

def deploy_mode(rel: str, dst: str) -> str:
    # Mode follows DESTINATION role, not source extension. Extension-less helpers
    # (sa02m-set-storage-auto-format, sa02m-set-cpu-profile) land in
    # /usr/local/sbin and must stay executable — see private/BUG-ota-mode-0644.md.
    if dst.startswith("/usr/local/sbin/") or dst.startswith("/usr/local/libexec/"):
        return "0755"
    if dst.startswith("/usr/local/bin/"):
        return "0755"
    if dst.startswith("/etc/dhcp/dhclient-exit-hooks.d/"):
        return "0755"
    if rel.endswith(".cgi") or rel.endswith(".sh"):
        return "0755"
    if dst.startswith("/opt/mplc4/") and rel.endswith(".so"):
        return "0755"
    return "0644"

deploy = []
for p in overlay.rglob("*"):
    if not p.is_file():
        continue
    rel = p.relative_to(overlay).as_posix()
    if "__pycache__" in rel or rel.endswith(".pyc"):
        continue
    dst = map_dst(rel)
    if not dst or not DST_RE.match(dst):
        continue
    mode = deploy_mode(rel, dst)
    owner = "www-data:www-data" if dst.startswith("/var/www/") else "root:root"
    deploy.append({"src": rel, "dst": dst, "mode": mode, "owner": owner})

ver = os.environ.get("TARGET_VER") or ""
if not ver:
    vf = overlay / "www/network_config/VERSION"
    if vf.is_file():
        for line in vf.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and re.match(r"^\d+(\.\d+){2,3}$", s):
                ver = s
                break
commit = os.environ.get("TARGET_COMMIT") or ("0" * 40)
manifest = {
    "schema_version": 1,
    "product": "SA-02m",
    "model": "A40i",
    "arch": "armv7l",
    "version": ver or "0.0.0.0",
    "repo_commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ("0" * 40),
    "built_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    "signing_key_id": "github-unsigned",
    "min_updater": "1.0.5.66",
    "min_version": "1.0.5.60",
    "payload": {"size": 0, "sha256": "0" * 64, "uncompressed_size_max": 1},
    "preflight": {
        "commands": ["/bin/bash", "/usr/bin/python3", "/usr/bin/install", "/usr/bin/systemctl"],
        "free_bytes_min": 67108864,
        "free_bytes_multiplier": 3,
    },
    "deploy": deploy,
    "services": {
        "daemon_reload": True,
        "stop_before_apply": ["sa02m-flasher"],
        "enable": [
            "sa02m-devices-api.service",
            "sa02m-devices-logger.service",
            # Boot-time DNS belt (docs/contracts/boot-network-dns.md). Deploying
            # the unit file is NOT enough: without an entry here no
            # multi-user.target.wants symlink is ever created and the unit stays
            # inert forever. Enable only, never restart[]: it is a Type=oneshot
            # whose job is the NEXT boot, and restart[] feeds the health check.
            "sa02m-dns-ensure.service",
        ],
        "restart": [
            "fcgiwrap",
            "nginx",
            "sa02m-flasher",
            "sa02m-devices-api",
            "sa02m-devices-logger",
            # Reload MPLC plugins after OTA (mplc_cyntron.so publishes licence).
            # Masked/disabled units are skipped by the health loop; restart uses
            # the same soft || true path as other optional services.
            "mplc4",
            # Telemetry owns its own device id and its legacy-retained clear
            # (1.0.6.22), so the new .py on disk changes nothing until the
            # process restarts. Safe here: it touches /proc, /sys and i2c bus 2
            # only, never an RS-485 port — unlike sa02m-modbus-mqtt, which
            # stays OUT of this list under the port-lease invariant.
            # HONEST LIMIT: this manifest is built by the runner ALREADY on the
            # board, so the entry first bites on the update AFTER the one that
            # delivers it. Landing 1.0.6.22 over the web leaves the old process
            # running until a reboot or `systemctl restart sa02m-telemetry`.
            # Must stay in step with scripts/pack-offline-update.py.
            "sa02m-telemetry",
            # Scenario engine holds /opt/sa02m-rules in memory: an OTA that
            # delivers new rules code must bounce it, or scenarios keep running
            # the stale engine until reboot (1.0.6.37 bench incident class:
            # cloud scenario push silently stripped trigger/end on stale code).
            # Core unit — enabled+started by 06b-rules.sh on every full
            # install, so the restart||start semantics of this list fits (same
            # as sa02m-telemetry). Must stay in step with
            # scripts/pack-offline-update.py.
            "sa02m-rules",
        ],
        # Conditional restarts (never-widen). `systemctl restart` on an INACTIVE
        # unit STARTS it, so opt-in units must never go into restart[]: the
        # Alice family ships disabled by default (scripts/06-alice.sh `app off`)
        # and an OTA must not turn the board's Alice/cloud profile on. All three
        # hold the scenario channel code in memory (sa02m_alice +
        # sa02m_rules.store from /opt/sa02m-rules) — without the restart a
        # deploy of new /opt code leaves them stale (the 1.0.6.37 incident).
        # HONEST LIMIT (same as sa02m-telemetry above): built by the on-board
        # runner, so these entries first bite on the update AFTER the one that
        # delivers this runner. Must stay in step with
        # scripts/pack-offline-update.py.
        "restart_if_active": [
            "sa02m-alice-client",
            "sa02m-alice-config",
            "sa02m-cloud-control",
        ],
        # Change-gated conditional restart: unit -> /opt prefix watched in the
        # apply journal. sa02m-modbus-mqtt owns the RS-485 port lease: restart
        # it only when the bridge code actually changed (a www-only patch must
        # not bounce industrial polling) and only when it is already running —
        # a stopped bridge may be stopped FOR a flasher lease, never start it
        # (the port-lease invariant that keeps it out of restart[] above).
        # Must stay in step with scripts/pack-offline-update.py.
        "restart_if_changed": {
            "sa02m-modbus-mqtt": "/opt/sa02m-modbus-mqtt/",
        },
        "health": {
            "http_url": "http://127.0.0.1:9999/login.html",
            "units_active": ["nginx", "fcgiwrap", "sa02m-devices-api"],
            "version_file": "/var/www/network_config/VERSION",
        },
    },
    "delete": [],
    "migrations": [],
}
(meta / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
(meta / "signature_ok").write_text("0\n", encoding="utf-8")
print("OK github overlay deploy=%d version=%s" % (len(deploy), ver))
PY
    printf '1' >"$stage_dir/meta/github_overlay"
    log "github overlay prepared txn=$txn files via staging"
}

# --- validate + extract (Python module or bootstrap) -------------------------

run_validate_and_extract() {
    local pkg=$1
    local txn=$2
    local stage_dir="$STATEDIR/staging/$txn"
    mkdir -p "$stage_dir/overlay" "$stage_dir/backups" "$stage_dir/meta"

    local allow_unsigned=0
    if [ "$(txn_get source)" = "github" ]; then
        allow_unsigned=1
    fi

    if [ -f "$VALIDATE_PY" ] && [ "$allow_unsigned" != "1" ]; then
        PACKAGE="$pkg" STAGING="$stage_dir" \
        INSTALLED="$(read_installed_version)" UPDATER="$UPDATER_VERSION" \
        VALIDATE_PY="$VALIDATE_PY" \
        python3 <<'PY' || die E_SIG "validate_package extract failed"
import json, os, sys
from pathlib import Path

lib = Path(os.environ["VALIDATE_PY"]).resolve().parent
root = lib.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(lib))
try:
    from lib.validate_package import validate_package
    from lib import PackageError
except ImportError:
    from validate_package import validate_package  # type: ignore
    from __init__ import PackageError  # type: ignore

staging = Path(os.environ["STAGING"])
meta = staging / "meta"
meta.mkdir(parents=True, exist_ok=True)
try:
    result = validate_package(
        Path(os.environ["PACKAGE"]),
        trusted_keys_dir=Path("/etc/sa02m-update/trusted-keys"),
        installed_version=os.environ.get("INSTALLED") or None,
        runner_version=os.environ.get("UPDATER") or None,
        extract_to=staging,
        check_compat=bool(os.environ.get("INSTALLED")),
    )
except PackageError as exc:
    print("%s: %s" % (exc.code, exc.message), file=sys.stderr)
    sys.exit(2)
(meta / "manifest.json").write_text(
    json.dumps(result["manifest"], indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
(meta / "signature_ok").write_text("1\n" if result.get("signature_ok") else "0\n", encoding="utf-8")
print("OK version=%s" % result.get("version"))
PY
        return 0
    fi

    # Bootstrap extract (no module, or unsigned GitHub path): trailer + members +
    # payload hash + safe inner tar. File path requires Ed25519 unless allow_unsigned.
    STAGING="$stage_dir" PACKAGE="$pkg" \
    INSTALLED="$(read_installed_version)" UPDATER="$UPDATER_VERSION" \
    ALLOW_UNSIGNED="$allow_unsigned" \
    python3 <<'PY' || die E_TAR "bootstrap extract failed"
import hashlib, json, os, re, sys, tarfile

FOOTER = b"SA02M_UPDATE_END_V1" + b"\0\0"  # 21 bytes (magic 19 + NUL NUL)
FOOTER_LEN = len(FOOTER)
staging = os.environ["STAGING"]
package = os.environ["PACKAGE"]
installed = os.environ.get("INSTALLED") or ""
updater = os.environ.get("UPDATER") or ""
allow_unsigned = os.environ.get("ALLOW_UNSIGNED", "0") == "1"
overlay = os.path.join(staging, "overlay")
meta = os.path.join(staging, "meta")
os.makedirs(overlay, exist_ok=True)
os.makedirs(meta, exist_ok=True)

DST_RE = re.compile(
    r"^/(var/www/network_config/|usr/local/(sbin|lib|libexec)/|"
    r"opt/sa02m-[a-z0-9-]+/|opt/mplc4/|"
    r"etc/systemd/system/sa02m-|"
    r"etc/nginx/|etc/tmpfiles\.d/|etc/sudoers\.d/|"
    r"etc/default/sa02m-|"
    r"etc/sa02m-update/trusted-keys/|"
    r"etc/dhcp/dhclient-exit-hooks\.d/eth1-default-route$)"
)
DEL_RE = re.compile(
    r"^/(var/www/network_config|opt/sa02m-[a-z0-9-]+|usr/local/|etc/systemd/system/sa02m-)/"
)
PRESERVE_PREFIXES = (
    "/etc/sa02m_web.env",
    "/etc/network/interfaces.d/",
    "/etc/nginx/.htpasswd",
    "/etc/nginx/sites-enabled/000-sa02m-network_config",
    "/etc/sa02m-device-templates/",
    "/etc/sa02m-cloud/",
    "/var/lib/sa02m-flasher/",
    "/var/lib/sa02m-update/",
    "/etc/sa02m-alice-client.conf",
    "/etc/sa02m-alice-devices.conf",
    "/var/lib/sa02m-alice/",
)

def fail(code, msg):
    print("%s: %s" % (code, msg), file=sys.stderr)
    sys.exit(2)

size = os.path.getsize(package)
if size < 1024 + FOOTER_LEN:
    fail("E_TRAILER", "too small")
with open(package, "rb") as f:
    f.seek(-FOOTER_LEN, 2)
    footer = f.read(FOOTER_LEN)
if footer != FOOTER:
    fail("E_TRAILER", "bad trailer")
tar_size = size - FOOTER_LEN
if tar_size % 512 != 0:
    fail("E_TAR", "tar_size alignment")

required = {"manifest.json", "manifest.sig", "payload.tar.gz", "payload.sha256"}
members = {}
with open(package, "rb") as f:
    blob = f.read(tar_size)
with tarfile.open(fileobj=__import__("io").BytesIO(blob), mode="r:") as tf:
    for m in tf:
        name = m.name.split("/")[-1]
        if name not in required:
            fail("E_TAR", "unexpected member %s" % m.name)
        if m.isfile() is False:
            fail("E_TAR", "non-file member %s" % m.name)
        if m.name in members or name in members:
            fail("E_TAR", "duplicate member")
        ef = tf.extractfile(m)
        if ef is None:
            fail("E_TAR", "unreadable %s" % name)
        data = ef.read()
        members[name] = data
if set(members) != required:
    fail("E_TAR", "members=%s" % sorted(members))

manifest = json.loads(members["manifest.json"].decode("utf-8"))
for k in ("schema_version", "product", "model", "arch", "version",
          "repo_commit", "signing_key_id", "min_updater", "min_version",
          "payload", "deploy"):
    if k not in manifest:
        fail("E_COMPAT", "manifest missing %s" % k)

payload_meta = manifest["payload"]
want_hash = str(payload_meta.get("sha256", "")).lower()
want_size = int(payload_meta.get("size", -1))
got_hash = hashlib.sha256(members["payload.tar.gz"]).hexdigest()
member_hash = members["payload.sha256"].decode("utf-8").split()[0].strip().lower()
if got_hash != want_hash or member_hash != want_hash:
    fail("E_HASH", "payload sha256 mismatch")
if want_size >= 0 and len(members["payload.tar.gz"]) != want_size:
    fail("E_HASH", "payload size mismatch")

# Ed25519 required for file-path apply; GitHub OTA may set allow_unsigned (plan §2.9).
import base64, subprocess
sig_ok = False
if allow_unsigned:
    sig_ok = False
else:
    key_id = str(manifest.get("signing_key_id", ""))
    key_path = "/etc/sa02m-update/trusted-keys/%s.pem" % key_id
    if not os.path.isfile(key_path):
        keys = sorted(
            p for p in (
                os.path.join("/etc/sa02m-update/trusted-keys", n)
                for n in (os.listdir("/etc/sa02m-update/trusted-keys")
                          if os.path.isdir("/etc/sa02m-update/trusted-keys") else [])
                if n.endswith(".pem")
            )
            if os.path.isfile(p)
        )[:8]
        if not keys:
            fail("E_SIG", "no trusted key for signing_key_id=%s" % key_id)
        key_path = keys[0]
    canon = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    msg = b"SA02M-MANIFEST-V1\0" + canon
    sig_b64 = members["manifest.sig"].decode("utf-8").strip()
    sig_raw = base64.b64decode(sig_b64)
    msg_path = os.path.join(meta, "sigmsg")
    sig_path = os.path.join(meta, "manifest.sig.raw")
    with open(msg_path, "wb") as f:
        f.write(msg)
    with open(sig_path, "wb") as f:
        f.write(sig_raw)
    key_candidates = [key_path]
    if os.path.isdir("/etc/sa02m-update/trusted-keys"):
        for n in sorted(os.listdir("/etc/sa02m-update/trusted-keys")):
            if n.endswith(".pem"):
                kp = os.path.join("/etc/sa02m-update/trusted-keys", n)
                if kp not in key_candidates:
                    key_candidates.append(kp)
            if len(key_candidates) >= 8:
                break
    verified = False
    for kp in key_candidates:
        r = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", kp,
             "-rawin", "-in", msg_path, "-sigfile", sig_path],
            capture_output=True,
        )
        if r.returncode == 0:
            verified = True
            break
    if not verified:
        fail("E_SIG", "signature verify failed")
    sig_ok = True

def semver_tuple(s):
    parts = [int(x) for x in str(s).split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])

ver = str(manifest["version"])
min_v = str(manifest["min_version"])
min_u = str(manifest["min_updater"])
if installed:
    if semver_tuple(installed) < semver_tuple(min_v):
        fail("E_COMPAT", "installed %s < min_version %s" % (installed, min_v))
    if semver_tuple(ver) <= semver_tuple(installed):
        fail("E_COMPAT", "target %s not greater than installed %s" % (ver, installed))
if updater and semver_tuple(updater) < semver_tuple(min_u):
    fail("E_COMPAT", "updater %s < min_updater %s" % (updater, min_u))

for item in manifest.get("deploy", []):
    dst = item.get("dst", "")
    if not DST_RE.match(dst):
        fail("E_COMPAT", "dst not allowlisted: %s" % dst)
    for p in PRESERVE_PREFIXES:
        if dst == p or dst.startswith(p if p.endswith("/") else p + "/"):
            fail("E_COMPAT", "dst hits PRESERVE_PATHS: %s" % dst)
    if dst.startswith("/etc/sa02m_") and dst.endswith(".conf"):
        fail("E_COMPAT", "dst hits PRESERVE conf: %s" % dst)

for dpath in manifest.get("delete", []):
    if not DEL_RE.match(dpath):
        fail("E_COMPAT", "delete not allowlisted: %s" % dpath)

# Safe inner extract
raw = members["payload.tar.gz"]
max_un = int(payload_meta.get("uncompressed_size_max", 128 * 1024 * 1024))
import io
unc_total = 0
with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
    seen = set()
    safe_members = []
    for m in tf.getmembers():
        name = m.name.lstrip("./")
        if not name or name.startswith("/") or ".." in name.split("/"):
            fail("E_TAR_TRAV", name)
        if m.issym() or m.islnk():
            fail("E_TAR_SYMLINK", name)
        if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
            fail("E_TAR_DEVICE", name)
        if name in seen:
            fail("E_TAR", "duplicate %s" % name)
        seen.add(name)
        if name.lower().endswith((".tar", ".tar.gz", ".sa02m", ".tgz")):
            fail("E_TAR_BOMB", "nested archive %s" % name)
        if m.isfile():
            unc_total += max(m.size, 0)
            if unc_total > max_un:
                fail("E_TAR_BOMB", "uncompressed_size_max exceeded")
            if len(raw) > 0 and unc_total > 20 * len(raw):
                fail("E_TAR_BOMB", "ratio > 20")
        m.name = name
        safe_members.append(m)
    kwargs = {}
    if hasattr(tarfile, "data_filter"):
        kwargs["filter"] = "data"
    tf.extractall(overlay, members=safe_members, **kwargs)

# Cap modes
for root, dirs, files in os.walk(overlay):
    for d in dirs:
        os.chmod(os.path.join(root, d), 0o755)
    for fn in files:
        p = os.path.join(root, fn)
        mode = os.stat(p).st_mode & 0o777
        os.chmod(p, mode & 0o755 if (mode & 0o111) else 0o644)

with open(os.path.join(meta, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
with open(os.path.join(meta, "signature_ok"), "w", encoding="utf-8") as f:
    f.write("1\n" if sig_ok else "0\n")
print("OK version=%s sig_ok=%s" % (ver, sig_ok))
PY
}

read_installed_version() {
    if [ -f "$VERSION_FILE" ]; then
        tr -d '\r' <"$VERSION_FILE" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true
    fi
}

manifest_path() {
    local txn=$1
    printf '%s\n' "$STATEDIR/staging/$txn/meta/manifest.json"
}

# --- backup / apply / rollback -----------------------------------------------

build_rollback_archive() {
    local txn=$1
    local mf list archive
    mf=$(manifest_path "$txn")
    [ -f "$mf" ] || die E_INTERNAL "manifest missing for backup"
    list="$STATEDIR/staging/$txn/rollback.list"
    archive="$STATEDIR/rollback/pre-update-${txn}.tar.gz"
    python3 - "$mf" "$list" <<'PY'
import json, os, sys
mf, list_path = sys.argv[1], sys.argv[2]
manifest = json.load(open(mf, encoding="utf-8"))
paths = []
for item in manifest.get("deploy", []):
    dst = item.get("dst")
    if dst and os.path.lexists(dst) and not os.path.islink(dst) and os.path.isfile(dst):
        paths.append(dst)
with open(list_path, "w", encoding="utf-8") as f:
    for p in paths:
        f.write(p + "\n")
PY
    if [ ! -s "$list" ]; then
        python3 -c 'import tarfile,sys; tarfile.open(sys.argv[1],"w:gz").close()' "$archive"
    else
        # Create archive of absolute paths; restore never uses tar -C /.
        tar -czf "$archive" --verbatim-files-from -T "$list" || die E_APPLY "rollback archive failed"
    fi
    chmod 0600 "$archive"
    # FIFO retention: keep max 2
    ls -1t "$STATEDIR/rollback"/pre-update-*.tar.gz 2>/dev/null | tail -n +3 | while read -r old; do
        rm -f "$old"
    done
    txn_patch "rollback_archive=$archive"
    log "rollback archive: $archive"
}

journal_append() {
    local txn=$1
    shift
    local j="$STATEDIR/staging/$txn/journal.jsonl"
    python3 -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1]),ensure_ascii=False))' "$1" >>"$j"
}

atomic_install_file() {
    local src=$1 dst=$2 mode=$3 owner=$4
    local dstdir tmp
    dstdir=$(dirname "$dst")
    mkdir -p "$dstdir"
    tmp="${dst}.tmp.$$"
    # install copies mode/owner when possible
    if [ -n "$owner" ]; then
        install -m "$mode" -o "${owner%:*}" -g "${owner#*:}" "$src" "$tmp" 2>/dev/null \
            || install -m "$mode" "$src" "$tmp"
    else
        install -m "$mode" "$src" "$tmp"
    fi
    if command -v fdatasync >/dev/null 2>&1; then
        fdatasync "$tmp" 2>/dev/null || true
    else
        python3 -c 'import os,sys; fd=os.open(sys.argv[1],os.O_RDONLY); os.fdatasync(fd); os.close(fd)' "$tmp" 2>/dev/null || sync
    fi
    mv -f "$tmp" "$dst"
    python3 -c 'import os,sys; d=os.path.dirname(sys.argv[1]) or "."; fd=os.open(d,os.O_RDONLY); os.fsync(fd); os.close(fd)' "$dst" 2>/dev/null || sync
}

# Deploy-skip predicate: returns 0 (unchanged) ONLY when $dst already matches the
# manifest on ALL THREE axes — content, mode, and owner. Any mismatch, or any doubt
# (stat/cmp failure, unparseable mode), returns 1 (changed) so the caller deploys:
# fail-safe to deploying, never to a silently stale file. Caller ensures $dst exists
# (a missing dst is the create path, never a skip). Replaces the cp -a + install +
# 2x fsync + journal spawn a re-install of an identical file would otherwise cost.
is_unchanged() {
    local src=$1 dst=$2 mode=$3 owner=$4
    # Content — byte-identical (cmp is coreutils; present on device and in sandbox).
    cmp -s "$src" "$dst" || return 1
    # Mode — the manifest carries 4-digit "0755"/"0644"; stat '%a' returns "755"/
    # "644" (no leading zero). Validate both as pure octal, then compare numerically
    # so 0644 == 644 — a naive string == would treat every file as changed (skip
    # nothing). A right-content wrong-mode file (0644-vs-0755) mismatches here and is
    # re-deployed with the mode corrected.
    local live_mode
    live_mode=$(stat -c '%a' "$dst" 2>/dev/null) || return 1
    case "$live_mode" in ''|*[!0-7]*) return 1 ;; esac
    case "$mode"      in ''|*[!0-7]*) return 1 ;; esac
    [ "$((8#$live_mode))" -eq "$((8#$mode))" ] || return 1
    # Owner — only when the manifest owner is populated (matches the runner's own
    # owner-optional install fallback in atomic_install_file). Empty owner ⇒
    # content+mode only.
    if [ -n "$owner" ]; then
        local live_owner
        live_owner=$(stat -c '%U:%G' "$dst" 2>/dev/null) || return 1
        [ "$live_owner" = "$owner" ] || return 1
    fi
    return 0
}

# B1 deploy-gap: remove legacy sudoers + extension-less OTA helper twins after
# the manifest files land. Allow-list only — never glob /etc/sudoers.d/*.
cleanup_b1_deploy_artifacts() {
    local _name _path _p
    for _name in www-data sa02m-www.fragment; do
        _path="/etc/sudoers.d/$_name"
        if [ -f "$_path" ]; then
            rm -f "$_path" && log "cleanup: removed obsolete sudoers $_path"
        fi
    done
    for _p in /usr/local/sbin/sa02m-iface-conf-write /usr/local/sbin/sa02m-usb-power \
              /usr/local/sbin/sa02m-gateway-config-apply /usr/local/sbin/sa02m-mqtt-config-apply \
              /usr/local/sbin/sa02m-conf-rm /usr/local/sbin/sa02m-mplc-project-deploy; do
        if [ -f "$_p" ] && [ ! -L "$_p" ]; then
            rm -f "$_p" && log "cleanup: removed extension-less B1 helper twin $_p"
        fi
    done
    # OTA may land sa02m-* sudoers as 0644 (source tree mode); visudo -c then
    # WARN-fails even when syntax is OK. Harden known drop-ins we ship.
    for _name in sa02m-www sa02m-cloud sa02m-flasher sa02m-mqtt sa02m-gateway sa02m-alice; do
        _path="/etc/sudoers.d/$_name"
        if [ -f "$_path" ]; then
            chmod 0440 "$_path" 2>/dev/null || true
        fi
    done
    if command -v visudo >/dev/null 2>&1; then
        visudo -c >/dev/null 2>&1 || log "WARN: visudo -c after B1 cleanup"
    fi
}

apply_deploy_items() {
    local txn=$1
    local mf overlay total done item_json
    mf=$(manifest_path "$txn")
    overlay="$STATEDIR/staging/$txn/overlay"
    total=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1],encoding="utf-8")).get("deploy",[])))' "$mf")
    txn_patch "files_total=$total" "files_done=0" "progress_pct=0"
    done=0

    while IFS= read -r item_json; do
        [ -n "$item_json" ] || continue
        local src_rel dst mode owner src_abs bak
        src_rel=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("src",""))' "$item_json")
        dst=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("dst",""))' "$item_json")
        mode=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("mode","0644"))' "$item_json")
        owner=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("owner",""))' "$item_json")
        src_abs="$overlay/$src_rel"
        if [ ! -f "$src_abs" ]; then
            log "ERROR: missing staged src: $src_rel"
            return 1
        fi
        bak=""
        if [ -e "$dst" ] && is_unchanged "$src_abs" "$dst" "$mode" "$owner"; then
            # Already installed identically (content + mode + owner) — skip the
            # backup, the journal line, AND the install entirely. Suppressing all
            # three together keeps content ⇄ backup ⇄ journal consistent, so a later
            # rollback never tries to restore a backup that was never taken. Still
            # counted as done below so files_done reaches files_total (the recover-
            # verify invariant); the log line keeps an all-skipped run visible.
            log "apply: skip unchanged $dst"
        else
            if [ -e "$dst" ]; then
                bak="$STATEDIR/staging/$txn/backups/$(printf '%s' "$dst" | sha256sum | awk '{print $1}')"
                mkdir -p "$(dirname "$bak")"
                cp -a "$dst" "$bak"
                journal_append "$txn" "{\"op\":\"replace\",\"dst\":\"$dst\",\"backup\":\"$bak\",\"mode\":\"$mode\",\"owner\":\"$owner\"}"
            else
                journal_append "$txn" "{\"op\":\"create\",\"dst\":\"$dst\",\"mode\":\"$mode\",\"owner\":\"$owner\"}"
            fi
            if ! atomic_install_file "$src_abs" "$dst" "$mode" "$owner"; then
                log "ERROR: atomic install failed: $dst"
                return 1
            fi
        fi
        done=$((done + 1))
        local pct=0
        if [ "$total" -gt 0 ]; then
            pct=$((done * 100 / total))
        fi
        # Patch txn every 10 files (and on the last): per-file JSON rewrite + fsync
        # made 1.0.6.1→latest apply take ~12 min with no log lines between re-exec
        # and verifying — UI looked frozen on "updating packages".
        if [ "$done" -eq "$total" ] || [ $((done % 10)) -eq 0 ]; then
            txn_patch "files_done=$done" "progress_pct=$pct"
            log "apply: files $done/$total (${pct}%)"
        fi
    done < <(python3 -c 'import json,sys
for it in json.load(open(sys.argv[1],encoding="utf-8")).get("deploy",[]):
    print(json.dumps(it,ensure_ascii=False))
' "$mf")
    txn_patch "files_done=$done" "progress_pct=100"
    return 0
}

# Stamp "which release installed this runner" when THIS transaction deployed
# the runner binary (the UPDATER_VERSION block at the top names the homes).
# Goes THROUGH THE JOURNAL — a replace/create record with the old stamp backed
# up — so rollback_from_journal (E_APPLY, E_HEALTH, recover after power loss)
# restores the pre-update stamp together with the pre-update binary and a
# board never reports a runner it rolled back from. A manifest that does not
# deploy $RUNNER_BIN_DST (a www-only pack) leaves the stamp alone: that is the
# case the stamp exists for. Soft on an unusable manifest version (no write,
# one log line, apply continues — the stale stamp is at worst one release old
# and the next runner deploy corrects it); hard on a failed write (caller rolls
# back — the state dir is the transaction's own home, so it must be writable).
# Called from cmd_apply, NOT from inside apply_deploy_items: the dev harnesses
# extract that function as a single slice and must keep running it unchanged.
stamp_runner_version_after_deploy() {
    local txn=$1
    local mf ver n src bak
    mf=$(manifest_path "$txn")
    n=$(python3 -c 'import json,sys
m=json.load(open(sys.argv[1],encoding="utf-8"))
print(sum(1 for it in m.get("deploy",[]) if it.get("dst")==sys.argv[2]))' "$mf" "$RUNNER_BIN_DST")
    if [ "${n:-0}" -eq 0 ]; then
        log "runner stamp: manifest does not deploy $RUNNER_BIN_DST - stamp unchanged"
        return 0
    fi
    ver=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("version",""))' "$mf")
    if ! [[ "$ver" =~ ^[0-9]+(\.[0-9]+){1,3}$ ]]; then
        log "WARN: runner stamp: manifest version '$ver' unusable - stamp unchanged"
        return 0
    fi
    src="$STATEDIR/staging/$txn/runner.version.new"
    printf '%s\n' "$ver" >"$src"
    if [ -e "$RUNNER_VERSION_FILE" ]; then
        bak="$STATEDIR/staging/$txn/backups/$(printf '%s' "$RUNNER_VERSION_FILE" | sha256sum | awk '{print $1}')"
        mkdir -p "$(dirname "$bak")"
        cp -a "$RUNNER_VERSION_FILE" "$bak"
        journal_append "$txn" "{\"op\":\"replace\",\"dst\":\"$RUNNER_VERSION_FILE\",\"backup\":\"$bak\",\"mode\":\"0644\",\"owner\":\"root:root\"}"
    else
        journal_append "$txn" "{\"op\":\"create\",\"dst\":\"$RUNNER_VERSION_FILE\",\"mode\":\"0644\",\"owner\":\"root:root\"}"
    fi
    if ! atomic_install_file "$src" "$RUNNER_VERSION_FILE" 0644 root:root; then
        rm -f "$src"
        log "ERROR: runner stamp: install of $RUNNER_VERSION_FILE failed"
        return 1
    fi
    rm -f "$src"
    log "runner stamp: $RUNNER_VERSION_FILE = $ver (manifest deployed $RUNNER_BIN_DST)"
    return 0
}

apply_deletes() {
    local txn=$1
    local mf dpath bak
    mf=$(manifest_path "$txn")
    while IFS= read -r dpath; do
        [ -n "$dpath" ] || continue
        if [ -e "$dpath" ]; then
            bak="$STATEDIR/staging/$txn/backups/del-$(printf '%s' "$dpath" | sha256sum | awk '{print $1}')"
            mkdir -p "$(dirname "$bak")"
            if [ -f "$dpath" ]; then
                cp -a "$dpath" "$bak"
                journal_append "$txn" "{\"op\":\"delete\",\"dst\":\"$dpath\",\"backup\":\"$bak\"}"
                rm -f "$dpath"
            else
                log "ERROR: delete target not a regular file: $dpath"
                return 1
            fi
        fi
    done < <(python3 -c 'import json,sys
for p in json.load(open(sys.argv[1],encoding="utf-8")).get("delete",[]):
    print(p)
' "$mf")
    return 0
}

run_migrations() {
    local txn=$1
    local mf
    mf=$(manifest_path "$txn")
    local count
    count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1],encoding="utf-8")).get("migrations",[])))' "$mf")
    if [ "$count" = "0" ]; then
        return 0
    fi
    # Hash-checked migrations require the Python helper for script materialization.
    if [ ! -f "$VALIDATE_PY" ]; then
        log "ERROR [E_CMD]: migrations present but validate_package.py missing"
        return 1
    fi
    if ! python3 "$VALIDATE_PY" migrate --staging "$STATEDIR/staging/$txn"; then
        log "ERROR [E_APPLY]: migration failed"
        return 1
    fi
    return 0
}

rollback_from_journal() {
    local txn=$1
    local j="$STATEDIR/staging/$txn/journal.jsonl"
    log "rollback from journal txn=$txn"
    txn_patch "stage=rolling_back" "result=pending"
    if [ -f "$j" ]; then
        python3 - "$j" <<'PY'
import json, os, shutil, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    lines = [ln.strip() for ln in f if ln.strip()]
for line in reversed(lines):
    rec = json.loads(line)
    op = rec.get("op")
    dst = rec.get("dst")
    bak = rec.get("backup")
    if op in ("replace", "delete") and bak and os.path.isfile(bak) and dst:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copy2(bak, dst)
    elif op == "create" and dst and os.path.lexists(dst):
        if os.path.isfile(dst):
            os.remove(dst)
PY
    else
        # Fall back to rollback archive members → temp → install (never tar -C /).
        local archive
        archive=$(txn_get rollback_archive)
        if [ -n "$archive" ] && [ -f "$archive" ]; then
            local tmp
            # $STATEDIR/staging is guaranteed by ensure_dirs, but staging/$txn is
            # gone after a power loss (tmpfs/lost staging) — the very case recover
            # exists for. mktemp under staging itself so extraction never fails.
            tmp=$(mktemp -d "$STATEDIR/staging/rollback-extract.XXXXXX")
            # -p preserves each member's mode/owner so the restore keeps exec bits
            # (else systemd 203/EXEC on the restored scripts) and restrictive perms.
            tar -xpzf "$archive" -C "$tmp" || true
            # Archive stored absolute paths; walk and restore each with its real mode.
            find "$tmp" -type f | while IFS= read -r f; do
                local rel="${f#"$tmp"}"
                if [ -n "$rel" ]; then
                    mkdir -p "$(dirname "$rel")"
                    install -m "$(stat -c '%a' "$f")" -o "$(stat -c '%u' "$f")" \
                        -g "$(stat -c '%g' "$f")" "$f" "$rel"
                fi
            done
            rm -rf "$tmp"
        fi
    fi
    restart_after_rollback "$txn" || true
    txn_patch "stage=rolled_back" "result=rolled_back" "error_code=E_APPLY" \
        "finished_at=$(utc_now)"
    cleanup_imaging_lock || true
    log "rollback complete"
}

# After the files are restored, the units restart_services_and_health bounced
# are still running the NEW code from memory over the OLD files on disk — an
# E_HEALTH rollback used to leave them that way until reboot (audit 2026-09-08,
# D9; the bounced set since 1.0.6.37 is 8 units, not 4). Same discipline as the
# apply path minus the health gate: daemon-reload, restart[] unconditionally
# (nginx via -t + reload, as on apply), restart_if_active[] and
# restart_if_changed{} only when active (never-widen — a rollback must not start
# a unit the operator keeps off), the change gate read from the same journal the
# restore just replayed (absent ⇒ changed). Every step is soft: a rollback that
# restored the files never fails on a restart. No manifest (recover after a lost
# staging) ⇒ logged, nothing bounced — the operator reboots.
restart_after_rollback() {
    local txn=$1 mf u prefix
    mf=$(manifest_path "$txn" 2>/dev/null) || mf=""
    if [ -z "$mf" ] || [ ! -f "$mf" ]; then
        log "rollback: no manifest for txn=$txn — units NOT restarted (reboot to drop in-memory code)"
        return 0
    fi
    log "rollback: daemon-reload + restart sets on the restored tree..."
    _systemctl_bounded 60 daemon-reload || true
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        case "$u" in
            nginx|nginx.service)
                if nginx -t 2>/dev/null; then
                    _systemctl_bounded 30 reload nginx || _systemctl_bounded 45 restart nginx || true
                else
                    log "rollback: nginx -t failed on the restored config"
                fi
                continue
                ;;
        esac
        _systemctl_bounded 60 restart "$u" || _systemctl_bounded 45 start "$u" || true
        log "restarted after rollback: $u"
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("restart",[]):
    print(u)
' "$mf" 2>/dev/null || true)
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        if systemctl is-active --quiet "$u"; then
            _systemctl_bounded 60 restart "$u" || true
            log "restarted after rollback (if-active): $u"
        fi
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("restart_if_active",[]):
    print(u)
' "$mf" 2>/dev/null || true)
    while IFS=$'\t' read -r u prefix; do
        [ -n "$u" ] && [ -n "${prefix:-}" ] || continue
        _journal_has_dst_prefix "$txn" "$prefix" || continue
        if systemctl is-active --quiet "$u"; then
            _systemctl_bounded 60 restart "$u" || true
            log "restarted after rollback (changed, if-active): $u"
        fi
    done < <(python3 -c 'import json,sys
m=json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("restart_if_changed",{})
for u,p in m.items():
    print(u+"\t"+p)
' "$mf" 2>/dev/null || true)
    return 0
}

# systemctl restart can block indefinitely when stop waits on busy CGI children
# (web UI polls web_update_apply.cgi during verify). Always bound the wait.
_systemctl_bounded() {
    local secs=$1
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" systemctl "$@" 2>/dev/null
    else
        systemctl "$@" 2>/dev/null
    fi
}

# True when the apply journal recorded a deployed/replaced/deleted dst under
# $prefix (skipped-unchanged files never reach the journal — apply_deploy_items
# suppresses the journal line together with the install). Journal absent
# (power-loss recover with tmpfs staging) => CHANGED: the failure mode this
# gate exists for is STALE in-memory code, not a spare restart.
_journal_has_dst_prefix() {
    local txn=$1 prefix=$2
    local j="$STATEDIR/staging/$txn/journal.jsonl"
    [ -f "$j" ] || return 0
    python3 - "$j" "$prefix" <<'PY'
import json, sys
j, p = sys.argv[1], sys.argv[2]
found = False
with open(j, encoding="utf-8") as f:
    for ln in f:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        d = rec.get("dst", "")
        if isinstance(d, str) and d.startswith(p):
            found = True
            break
sys.exit(0 if found else 1)
PY
}

# Returns 0 on success, 1 on failure (does not exit — caller may rollback).
restart_services_and_health() {
    local txn=$1
    local mf
    mf=$(manifest_path "$txn")
    local daemon_reload
    daemon_reload=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("daemon_reload",True) else "false")' "$mf")
    if [ "$daemon_reload" = "true" ]; then
        log "health: daemon-reload..."
        _systemctl_bounded 60 daemon-reload || true
    fi
    # Apply the freshly deployed tmpfiles confs without waiting for a reboot:
    # the enroll CGI needs the www-data-writable Alice state dir right away, and
    # the login throttle in lib_web_auth.sh fails OPEN until /run/sa02m-web-login
    # exists — www-data cannot create either dir itself (security review
    # 1.0.6.24, F1). Named list, not a glob: /etc/tmpfiles.d/ also holds distro
    # files this runner has no business re-applying mid-update.
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        for _tf in sa02m-alice.conf sa02m-web-login.conf; do
            [ -f "/etc/tmpfiles.d/$_tf" ] || continue
            timeout 30 systemd-tmpfiles --create "/etc/tmpfiles.d/$_tf" 2>/dev/null || true
        done
    fi
    # Enable new/updated units so they survive reboot (e.g. sa02m-devices-*).
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        _systemctl_bounded 60 enable "$u" || true
        log "enabled after apply: $u"
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("enable",[]):
    print(u)
' "$mf")
    # Ordered: fcgiwrap → nginx -t && reload → other restart[] (e.g. sa02m-flasher).
    # Bound fcgiwrap restart: UI polling keeps CGI children alive and can stall
    # an unbounded systemctl restart for many minutes (looks like "stuck on verifying").
    log "health: restarting fcgiwrap..."
    if ! _systemctl_bounded 45 restart fcgiwrap \
        && ! _systemctl_bounded 45 restart fcgiwrap.service; then
        log "health: fcgiwrap restart timed out - SIGTERM workers and continue"
        _systemctl_bounded 15 kill -s SIGTERM fcgiwrap || true
        _systemctl_bounded 30 start fcgiwrap || _systemctl_bounded 30 start fcgiwrap.service || true
    fi
    log "health: nginx -t / reload..."
    if nginx -t 2>/dev/null; then
        _systemctl_bounded 30 reload nginx || _systemctl_bounded 45 restart nginx || true
    else
        log "health: nginx -t failed"
        return 1
    fi
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        case "$u" in
            fcgiwrap|fcgiwrap.service|nginx|nginx.service) continue ;;
        esac
        log "health: restarting $u..."
        _systemctl_bounded 60 restart "$u" || _systemctl_bounded 45 start "$u" || true
        log "restarted after apply: $u"
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("restart",[]):
    print(u)
' "$mf")

    # restart_if_active[] — opt-in units (never-widen): `systemctl restart` on
    # an INACTIVE unit STARTS it, so the plain restart[] loop above would widen
    # an operator's OFF (the Alice family ships disabled — scripts/06-alice.sh
    # `app off`). Here: restart ONLY a currently-active unit, never start one.
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        if systemctl is-active --quiet "$u"; then
            log "health: restarting (if-active) $u..."
            _systemctl_bounded 60 restart "$u" || true
            log "restarted after apply: $u"
        else
            log "health: $u not active — conditional restart skipped"
        fi
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("restart_if_active",[]):
    print(u)
' "$mf")

    # restart_if_changed{unit: prefix} — the same if-active discipline, plus a
    # change gate on the unit's /opt tree: when every file under the prefix
    # deployed skip-identical, the service must not be bounced (sa02m-modbus-mqtt
    # holds the RS-485 port lease — a restart mid-polling is a bus hiccup the
    # operator did not ask for when the update never touched the bridge).
    while IFS=$'\t' read -r u prefix; do
        [ -n "$u" ] && [ -n "${prefix:-}" ] || continue
        if ! _journal_has_dst_prefix "$txn" "$prefix"; then
            log "health: $u — $prefix unchanged, conditional restart skipped"
            continue
        fi
        if systemctl is-active --quiet "$u"; then
            log "health: restarting (changed, if-active) $u..."
            _systemctl_bounded 60 restart "$u" || true
            log "restarted after apply: $u"
        else
            log "health: $u not active — conditional restart skipped"
        fi
    done < <(python3 -c 'import json,sys
m=json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("restart_if_changed",{})
for u,p in m.items():
    print(u+"\t"+p)
' "$mf")

    local http_url version_want version_file
    http_url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("health",{}).get("http_url",""))' "$mf")
    version_file=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("health",{}).get("version_file","/var/www/network_config/VERSION"))' "$mf")
    version_want=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["version"])' "$mf")

    while IFS= read -r u; do
        [ -n "$u" ] || continue
        if ! systemctl is-active --quiet "$u"; then
            # An operator-disabled required unit is NOT a health failure: the
            # operator deliberately took it out of service (never-widen). A shared
            # HardPy stand masks sa02m-devices-api because the stand app serves
            # :8765 instead — requiring it active would wrongly roll back every
            # update there. masked / masked-runtime / disabled ⇒ skip; an ENABLED
            # unit that is merely down still fails (a real regression).
            local _en_state
            _en_state=$(systemctl is-enabled "$u" 2>/dev/null || true)
            case "$_en_state" in
                masked|masked-runtime|disabled)
                    log "health: $u is $_en_state (operator-disabled) — not required active"
                    continue
                    ;;
            esac
            log "health: unit not active: $u"
            return 1
        fi
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("health",{}).get("units_active",[]):
    print(u)
' "$mf")

    if [ -n "$http_url" ]; then
        if command -v curl >/dev/null 2>&1; then
            if ! curl -fsS -o /dev/null --max-time 10 "$http_url"; then
                log "health: http failed: $http_url"
                return 1
            fi
        fi
    fi
    if [ -f "$version_file" ]; then
        local got
        got=$(tr -d '\r' <"$version_file" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
        if [ -n "$version_want" ] && [ "$got" != "$version_want" ]; then
            log "health: version_file=$got want=$version_want"
            return 1
        fi
    fi
    return 0
}

commit_markers() {
    local txn=$1
    local mf ver commit
    mf=$(manifest_path "$txn")
    ver=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["version"])' "$mf")
    commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("repo_commit",""))' "$mf")
    printf '%s\n' "$ver" >"$STATEDIR/state/deployed_version"
    printf '%s\n' "$commit" >"$STATEDIR/state/deployed_commit"
    utc_now >"$STATEDIR/state/deployed_at"
    chmod 644 "$STATEDIR/state/deployed_version" "$STATEDIR/state/deployed_commit" "$STATEDIR/state/deployed_at" 2>/dev/null || true
    # Mirror legacy paths for old status.js
    mkdir -p "$LEGACY_STATEDIR"
    cp -a "$STATEDIR/state/deployed_commit" "$LEGACY_STATEDIR/deployed_commit" 2>/dev/null || true
    cp -a "$STATEDIR/state/deployed_at" "$LEGACY_STATEDIR/deployed_at" 2>/dev/null || true
}

stop_before_apply() {
    local txn=$1
    local mf
    mf=$(manifest_path "$txn")
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        systemctl stop "$u" 2>/dev/null || true
        log "stopped before apply: $u"
    done < <(python3 -c 'import json,sys
for u in json.load(open(sys.argv[1],encoding="utf-8")).get("services",{}).get("stop_before_apply",[]):
    print(u)
' "$mf")
}

# --- main apply / recover ----------------------------------------------------

cmd_apply() {
    ensure_dirs
    migrate_legacy_state
    acquire_lock

    if ! txn_exists; then
        log "apply: no transaction.json — no-op"
        exit 0
    fi

    local stage result
    stage=$(txn_get stage)
    result=$(txn_get result)
    case "$stage" in
        idle|done|error|cancelled|rolled_back)
            log "apply: stage=$stage — no-op"
            exit 0
            ;;
    esac

    honour_cancel_if_early

    local pkg txn previous source overlay_src
    pkg=$(txn_get package_path)
    [ -n "$pkg" ] || pkg=$PACKAGE_DEFAULT
    txn=$(txn_get id)
    previous=$(read_installed_version)
    source=$(txn_get source)
    overlay_src=$(txn_get overlay_path)

    if [ -z "$txn" ]; then
        die E_INTERNAL "transaction id empty"
    fi

    # Resume after self-copy re-exec: skip validate/backup if overlay ready.
    if [ "${SA02M_UPDATE_REEXEC:-0}" = "1" ] && [ -f "$(manifest_path "$txn")" ]; then
        log "apply: resume after re-exec txn=$txn"
    else
        txn_patch "stage=validating" "previous_version=${previous:-}" "progress_pct=5"
        honour_cancel_if_early
        preflight_commands
        if [ "$source" = "github" ] && [ -n "$overlay_src" ] && [ -d "$overlay_src" ]; then
            log "apply: source=github overlay=$overlay_src"
            prepare_github_overlay "$txn"
            txn_patch "signature_ok=false" "progress_pct=15"
        else
            [ -f "$pkg" ] || die E_TAR "package missing: $pkg"
            preflight_space "$pkg"
            run_validate_and_extract "$pkg" "$txn"
            local sig_ok=false
            if [ -f "$STATEDIR/staging/$txn/meta/signature_ok" ] && [ "$(cat "$STATEDIR/staging/$txn/meta/signature_ok")" = "1" ]; then
                sig_ok=true
            fi
            local target_ver target_commit
            target_ver=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["version"])' "$(manifest_path "$txn")")
            target_commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("repo_commit",""))' "$(manifest_path "$txn")")
            txn_patch "target_version=$target_ver" "target_commit=$target_commit" "signature_ok=$sig_ok" "progress_pct=15"
        fi

        honour_cancel_if_early
        txn_patch "stage=backing_up" "progress_pct=25"
        honour_cancel_if_early
        build_rollback_archive "$txn"

        stop_before_apply "$txn"
        install_imaging_lock
        txn_patch "stage=applying" "progress_pct=35"
        self_reexec_before_deploy
    fi

    # Post re-exec (or if re-exec was skipped somehow)
    if [ ! -f "$IMAGING_LOCK" ]; then
        install_imaging_lock
    else
        IMAGING_HELD=1
    fi

    txn_patch "stage=applying" "progress_pct=40"
    # Extract already done before re-exec; deploy now.
    if ! apply_deploy_items "$txn"; then
        rollback_from_journal "$txn"
        log "ERROR [E_APPLY]: deploy failed (rolled back)"
        exit 1
    fi
    if ! stamp_runner_version_after_deploy "$txn"; then
        rollback_from_journal "$txn"
        log "ERROR [E_APPLY]: runner stamp failed (rolled back)"
        exit 1
    fi
    cleanup_b1_deploy_artifacts
    if ! apply_deletes "$txn"; then
        rollback_from_journal "$txn"
        log "ERROR [E_APPLY]: delete failed (rolled back)"
        exit 1
    fi
    if ! run_migrations "$txn"; then
        rollback_from_journal "$txn"
        log "ERROR [E_APPLY]: migrations failed (rolled back)"
        exit 1
    fi

    txn_patch "stage=verifying" "progress_pct=85"
    if ! restart_services_and_health "$txn"; then
        rollback_from_journal "$txn"
        log "ERROR [E_HEALTH]: health gate failed (rolled back)"
        exit 1
    fi

    txn_patch "stage=committing" "progress_pct=95"
    commit_markers "$txn"
    txn_patch "stage=done" "result=success" "progress_pct=100" "error_code=null" \
        "error_message=null" "finished_at=$(utc_now)"
    cleanup_imaging_lock
    sync
    log "DONE: update applied successfully txn=$txn"
    exit 0
}

cmd_recover() {
    ensure_dirs
    migrate_legacy_state
    acquire_lock

    if ! txn_exists; then
        log "recover: no transaction — no-op"
        exit 0
    fi

    local stage txn
    stage=$(txn_get stage)
    txn=$(txn_get id)
    log "recover: stage=$stage txn=$txn"

    case "$stage" in
        uploaded|validating|cancelled)
            wipe_incoming_staging
            txn_patch "stage=error" "result=failed" "error_code=E_POWER" \
                "error_message=power loss before apply" "finished_at=$(utc_now)"
            ;;
        backing_up)
            local archive
            archive=$(txn_get rollback_archive)
            if [ -n "$archive" ] && [ -f "$archive" ]; then
                rollback_from_journal "$txn"
            else
                txn_patch "stage=error" "result=failed" "error_code=E_POWER" \
                    "error_message=power loss during backup" "finished_at=$(utc_now)"
            fi
            cleanup_imaging_lock || true
            ;;
        applying)
            install_imaging_lock || true
            rollback_from_journal "$txn"
            ;;
        verifying)
            # Deploy finished (files on disk) but health/restart aborted — prefer
            # completing like committing, not rolling back a good tree. Fall back
            # to rollback only when the health gate still fails.
            install_imaging_lock || true
            if [ -n "$txn" ] && [ -f "$(manifest_path "$txn")" ]; then
                local files_done files_total ver_got ver_want
                files_done=$(txn_get files_done)
                files_total=$(txn_get files_total)
                ver_want=$(txn_get target_version)
                ver_got=$(tr -d '\r' <"$VERSION_FILE" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
                if [ -n "$files_total" ] && [ "$files_total" -gt 0 ] 2>/dev/null \
                    && [ "$files_done" = "$files_total" ] \
                    && [ -n "$ver_want" ] && [ "$ver_got" = "$ver_want" ]; then
                    log "recover: verifying with deploy complete ($files_done/$files_total, VERSION=$ver_got) - finish health"
                    if restart_services_and_health "$txn"; then
                        commit_markers "$txn"
                        txn_patch "stage=done" "result=success" "progress_pct=100" \
                            "finished_at=$(utc_now)"
                        cleanup_imaging_lock || true
                    else
                        log "recover: health still failing after complete deploy - rollback"
                        rollback_from_journal "$txn"
                    fi
                else
                    log "recover: verifying incomplete (done=$files_done total=$files_total ver=$ver_got want=$ver_want) - rollback"
                    rollback_from_journal "$txn"
                fi
            else
                rollback_from_journal "$txn"
            fi
            ;;
        committing)
            # Health OK → complete markers; else rollback.
            if [ -n "$txn" ] && [ -f "$(manifest_path "$txn")" ]; then
                if restart_services_and_health "$txn"; then
                    commit_markers "$txn"
                    txn_patch "stage=done" "result=success" "progress_pct=100" \
                        "finished_at=$(utc_now)"
                    cleanup_imaging_lock || true
                else
                    rollback_from_journal "$txn"
                fi
            else
                rollback_from_journal "$txn"
            fi
            ;;
        rolling_back)
            install_imaging_lock || true
            rollback_from_journal "$txn"
            ;;
        done|error|idle|rolled_back)
            log "recover: no-op for stage=$stage"
            cleanup_imaging_lock || true
            ;;
        *)
            log "recover: unknown stage=$stage — mark E_POWER"
            txn_patch "stage=error" "result=failed" "error_code=E_POWER" \
                "error_message=unknown stage after power loss" "finished_at=$(utc_now)"
            cleanup_imaging_lock || true
            ;;
    esac
    exit 0
}

# Trap: never leave the runtime watchdog held off if we held the lock.
on_exit() {
    local ec=$?
    if [ "$IMAGING_HELD" = "1" ] && [ "$ec" -ne 0 ]; then
        # Failed mid-apply: keep imaging lock only if still rolling; otherwise clear.
        local stage
        stage=$(txn_get stage 2>/dev/null || echo error)
        case "$stage" in
            applying|verifying|rolling_back|committing) ;;
            *) cleanup_imaging_lock || true ;;
        esac
    fi
}
trap on_exit EXIT

case "$CMD" in
    apply) cmd_apply ;;
    recover) cmd_recover ;;
    version) printf '%s\n' "$UPDATER_VERSION" ;;
    *)
        echo "usage: $0 apply|recover|version" >&2
        exit 2
        ;;
esac
