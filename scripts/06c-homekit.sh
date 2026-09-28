#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  06c-homekit.sh  —  Apple HomeKit bridge (sa02m-homekit, clean-room)
# Opt-in module: install.sh runs it only with --with-homekit / SA02M_WITH_HOMEKIT=1
# or when the bridge is already installed (refresh keeps it current). Never in
# the factory image (Operator decision Q-A: optional install, «не
# сертифицировано Apple»). Contract: docs/contracts/homekit-bridge.md.
#
# Order is a floor, each step BEFORE the one that consumes it:
#   Alice package present (the bridge imports sa02m_alice.DeviceRegistry)
#   → peer packages carry every symbol this bridge imports (refreshed from
#     this tree through their own modules, else STOP before touching anything)
#   → apt python3-venv/paho → system user + groups → venv from the hash-pinned
#   lock → package tree → dirs (tmpfiles) → access helper → conf seed →
#   capture → unit → apply (app off) → status fallback → trigger + sudoers →
#   CGI → JS → access check (LAST: the grant is tried as the daemon, fatal).
# Idempotent: a re-run on a configured board refreshes code only — the conf,
# the pairing store and the operator's enable/run state are never touched; a
# venv already matching the lock is left alone.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [06c-homekit] Установка sa02m-homekit (Apple HomeKit) ==="

BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$BASE_DIR/opt/sa02m-homekit"
UNIT_SRC="$BASE_DIR/etc/systemd/system"
WEB_ROOT_DIR="${WEB_ROOT:-/var/www/network_config}"
WEB_CGI="$WEB_ROOT_DIR/cgi-bin"
INSTALL_DIR=/opt/sa02m-homekit
# Outside INSTALL_DIR on purpose: the rsync --delete below would remove a venv
# living inside the package tree (plan adversary pass, finding b).
VENV_DIR=/opt/sa02m-homekit-venv
WHEELHOUSE=/opt/vendor-installers/homekit
HK_USER=sa02m-homekit
UNIT=sa02m-homekit.service
# The unit's import roots for the sibling packages (PYTHONPATH in the unit;
# the rules root is scene_devices.RULES_DIR's default).
HK_ALICE_DIR=/opt/sa02m-alice
HK_RULES_DIR=/opt/sa02m-rules

if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
    log WARN "[06c-homekit] сборка образа: HomeKit не входит в заводской образ (устанавливается интегратором) — пропуск"
    exit 0
fi

# ── Shared dependency first: the Alice package ─────────────────────────────
# The bridge builds its accessories from sa02m_alice's DeviceRegistry over the
# one device document; without the package the daemon cannot import at all.
if [ ! -d "$HK_ALICE_DIR/sa02m_alice" ]; then
    log WARN "[06c-homekit] HomeKit needs Alice package: /opt/sa02m-alice отсутствует (06-alice.sh не выполнен) — HomeKit не установлен"
    exit 0
fi
if [ ! -d "$OPT_SRC/sa02m_homekit" ]; then
    log WARN "[06c-homekit] исходники $OPT_SRC/sa02m_homekit не найдены — пропуск"
    exit 0
fi

# >>> peer-probe  (extracted and run by opt/sa02m-homekit/tests/test_peers.py)
# ── Peer packages: every symbol this bridge imports ────────────────────────
# A www-only update (scripts/update-www-only.sh) or an older full install can
# leave /opt/sa02m-alice (or /opt/sa02m-rules) a release behind this bridge:
# the daemon then died on an absent method (bench 1.135, 2026-09-28). The list
# has ONE home — sa02m_homekit.peers.REQUIRED_PEER_SYMBOLS — probed with the
# unit's interpreter (the venv once it exists, else the python3 it is built
# from) and the unit's import roots; `-I` so neither the operator's cwd nor an
# inherited PYTHONPATH can answer for the installed packages. Outdated ⇒ the
# peer is refreshed from THIS tree through its own module (idempotent; it
# restarts only its active units, docs/contracts/installer-refresh-policy.md),
# unless the operator skipped that module; still outdated ⇒ stop here, before
# the new bridge lands next to the old package. Rules are optional (§1 of the
# contract: no rules stack = no scenes), so an old rules package only warns.
HK_PEER_OUT=""
hk_peer_probe() {
    local py=python3 rc=0
    [ -x "$VENV_DIR/bin/python" ] && py="$VENV_DIR/bin/python"
    HK_PEER_OUT=$(timeout 60 "$py" -I - "$OPT_SRC" "$HK_ALICE_DIR" "$HK_RULES_DIR" 2>&1 <<'PY'
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from sa02m_homekit import peers
sys.exit(peers.cli(["--rules-dir", sys.argv[3]]))
PY
) || rc=$?
    return "$rc"
}
hk_peer_log() {
    local line
    while IFS= read -r line; do
        [ -n "$line" ] && log "$1" "[06c-homekit]   $line"
    done <<<"$HK_PEER_OUT"
}
hk_peer_rc=0
hk_peer_probe || hk_peer_rc=$?
if [ "$hk_peer_rc" = 10 ] || [ "$hk_peer_rc" = 11 ]; then
    log WARN "[06c-homekit] установленные пакеты старше моста HomeKit:"
    hk_peer_log WARN
    case "$HK_PEER_OUT" in
        *"outdated sa02m_alice"*)
            if [ "${SA02M_SKIP_ALICE:-0}" != 1 ] && [ -f "$SCRIPT_DIR/06-alice.sh" ] \
               && [ -d "$BASE_DIR/opt/sa02m-alice/sa02m_alice" ]; then
                log INFO "[06c-homekit] обновляю пакет Алисы из этого дерева (06-alice.sh)"
                bash "$SCRIPT_DIR/06-alice.sh" || log WARN "[06c-homekit] 06-alice.sh завершился с ошибкой"
            fi ;;
    esac
    case "$HK_PEER_OUT" in
        *"outdated-optional sa02m_rules"*)
            if [ "${SA02M_SKIP_RULES:-0}" != 1 ] && [ -f "$SCRIPT_DIR/06b-rules.sh" ] \
               && [ -d "$BASE_DIR/opt/sa02m-rules/sa02m_rules" ]; then
                log INFO "[06c-homekit] обновляю пакет сценариев из этого дерева (06b-rules.sh)"
                bash "$SCRIPT_DIR/06b-rules.sh" || log WARN "[06c-homekit] 06b-rules.sh завершился с ошибкой"
            fi ;;
    esac
    hk_peer_rc=0
    hk_peer_probe || hk_peer_rc=$?
fi
case "$hk_peer_rc" in
    0) log OK "[06c-homekit] пакеты Алисы и сценариев совместимы с мостом" ;;
    10)
        hk_peer_log ERR
        log ERR "[06c-homekit] пакет Алисы старше моста HomeKit — обновите пакет Алисы: запустите 06-alice.sh или полную установку (install.sh --refresh). HomeKit не изменён"
        exit 1 ;;
    11)
        hk_peer_log WARN
        log WARN "[06c-homekit] пакет сценариев старше моста — сцены в HomeKit недоступны; обновите: 06b-rules.sh или полная установка" ;;
    12)
        hk_peer_log WARN
        log WARN "[06c-homekit] пакет Алисы не импортируется — мост покажет «нет компонентов»; нужна полная установка (install.sh)" ;;
    2)
        hk_peer_log ERR
        log ERR "[06c-homekit] список символов моста пуст или испорчен (sa02m_homekit/peers.py) — проверка ничего не доказывает, установка остановлена"
        exit 1 ;;
    *)
        hk_peer_log WARN
        log WARN "[06c-homekit] проверка пакетов не выполнена (код $hk_peer_rc) — мост проверит их сам при запуске" ;;
esac
# <<< peer-probe

# ── System packages ────────────────────────────────────────────────────────
python3 -c "import paho.mqtt" 2>/dev/null || sa02m_pkg_install_tier optional python3-paho-mqtt
python3 -c "import ensurepip, venv" 2>/dev/null || sa02m_pkg_install_tier optional python3-venv
# The locked cryptography wheel is installed --no-deps: its C backend
# `_cffi_backend` comes from apt, never from PyPI (requirements.lock header).
python3 -c "import _cffi_backend" 2>/dev/null || sa02m_pkg_install_tier optional python3-cffi-backend

# ── Unprivileged system user (D11) ─────────────────────────────────────────
# No shell, no home, and never in www-data: that group would let this LAN
# listener read the panel credentials, the gateway YAML and every Alice conf
# (audit 2026-09-28). Its only other group is sa02m-alice-devices (below), which
# reads exactly one file. Everything else it needs is a mode, not a membership
# (setgid /run and conf dirs, docs/contracts/homekit-bridge.md §13); gate
# `daemon-least-privilege`. www-data is NOT in the daemon's group either, so it
# never reads the pairing store (P4).
if ! id -u "$HK_USER" >/dev/null 2>&1; then
    if useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --comment "SA-02m HomeKit bridge" "$HK_USER" >>"$LOG_FILE" 2>&1; then
        log OK "создан системный пользователь $HK_USER"
    else
        log WARN "[06c-homekit] не удалось создать пользователя $HK_USER — HomeKit не установлен"
        exit 0
    fi
fi
# Upgrade: a board installed before 1.0.6.57's least-privilege change has the
# account in www-data — take it out (the running bridge picks the new groups up
# at the restart `sa02m_svc_apply` does below).
_hk_groups=$(id -nG "$HK_USER" 2>/dev/null) || _hk_groups=""
case " $_hk_groups " in
    *" www-data "*)
        if gpasswd -d "$HK_USER" www-data >>"$LOG_FILE" 2>&1; then
            log OK "$HK_USER исключён из группы www-data"
        else
            log WARN "[06c-homekit] не удалось исключить $HK_USER из группы www-data — мост читает лишнее (gpasswd -d $HK_USER www-data)"
        fi ;;
esac
# Read on the Alice device document — and on nothing else in /etc/sa02m-alice —
# through a group of exactly two members: the bridge and www-data. www-data
# must be a member because the Alice CGI replaces the document by rename, and a
# non-root writer can give its new file only a group it belongs to (the Alice
# writer keeps the old file's group). No POSIX ACL: the product RT kernel has
# none (bench 1.135, 2026-09-28). The document's owner/group/mode are set by
# usr/local/sbin/sa02m-daemon-access.sh at the end of this module.
HK_DEVDOC_GROUP=sa02m-alice-devices
if ! getent group "$HK_DEVDOC_GROUP" >/dev/null 2>&1; then
    if groupadd --system "$HK_DEVDOC_GROUP" >>"$LOG_FILE" 2>&1; then
        log OK "создана группа $HK_DEVDOC_GROUP (чтение документа устройств Алисы мостом HomeKit)"
    else
        log WARN "[06c-homekit] не удалось создать группу $HK_DEVDOC_GROUP — мост не прочитает документ устройств"
    fi
fi
_hk_groups=$(id -nG "$HK_USER" 2>/dev/null) || _hk_groups=""
case " $_hk_groups " in
    *" $HK_DEVDOC_GROUP "*) ;;
    *)
        gpasswd -a "$HK_USER" "$HK_DEVDOC_GROUP" >>"$LOG_FILE" 2>&1 \
            || log WARN "[06c-homekit] не удалось добавить $HK_USER в $HK_DEVDOC_GROUP" ;;
esac
_hk_web_groups=$(id -nG www-data 2>/dev/null) || _hk_web_groups=""
case " $_hk_web_groups " in
    *" $HK_DEVDOC_GROUP "*) ;;
    *)
        if gpasswd -a www-data "$HK_DEVDOC_GROUP" >>"$LOG_FILE" 2>&1; then
            # The CGI inherits fcgiwrap's groups, fixed when fcgiwrap started:
            # until it restarts, a card save would hand the document back to
            # group www-data and cut the bridge off again.
            sa02m_svc_restart_if_active fcgiwrap.service
        else
            log WARN "[06c-homekit] не удалось добавить www-data в $HK_DEVDOC_GROUP — сохранение с карточки Алисы отнимет у моста документ устройств"
        fi ;;
esac

# ── Venv from the hash-pinned lock (D1) — BEFORE the code that imports it ───
# A failure is not fatal: the package, unit and card still land, and the daemon
# reports `missing_deps` (the card says «нужна полная установка»).
# The probe imports what the daemon really loads: pyhap.accessory_driver pulls
# zeroconf and (through the HAP server) cryptography + _cffi_backend; a bare
# `import pyhap` would pass on a venv missing any of them. segno draws the QR.
if sa02m_venv_install_locked "$VENV_DIR" "$OPT_SRC/requirements.lock" "pyhap.accessory_driver, segno" "$WHEELHOUSE"; then
    HK_DEPS=ok
else
    HK_DEPS=missing
    log WARN "[06c-homekit] зависимости HomeKit не установлены — карточка покажет «нет компонентов»; повторите install.sh --with-homekit при сети или с $WHEELHOUSE"
fi

# ── Package tree ───────────────────────────────────────────────────────────
install -d -m 0755 -o root -g root "$INSTALL_DIR"
log INFO "Копирую $OPT_SRC → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
    "$OPT_SRC/" "$INSTALL_DIR/"
chown -R root:root "$INSTALL_DIR"
chmod -R u=rwX,go=rX "$INSTALL_DIR"

# ── Runtime dirs (boot-persistent home: the package's tmpfiles.d copy) ─────
# The conf lives in the package, not under etc/tmpfiles.d/: everything there is
# OTA'd to EVERY board, and its lines name the sa02m-homekit account that only
# this module creates (journal noise + an empty /etc/sa02m-homekit on every
# other board). This installer is its only writer into /etc/tmpfiles.d/
# (pinned by `alice-conf-homes` section 12).
install -m 0644 -o root -g root \
    "$OPT_SRC/tmpfiles.d/sa02m-homekit.conf" /etc/tmpfiles.d/sa02m-homekit.conf
sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-homekit.conf
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-homekit.conf >>"$LOG_FILE" 2>&1 \
        || log WARN "[06c-homekit] systemd-tmpfiles --create вернул ошибку — каталоги создаю напрямую"
fi
# Same modes as the tmpfiles entries (idempotent re-assert; tmpfiles may be
# absent in a stripped rootfs). The conf dir: www-data writes it (atomic save),
# setgid sa02m-homekit makes every file saved there the bridge's group.
install -d -m 0700 -o "$HK_USER" -g "$HK_USER" /var/lib/sa02m-homekit
install -d -m 2750 -o "$HK_USER" -g www-data /run/sa02m-homekit
install -d -m 2750 -o www-data -g sa02m-homekit /etc/sa02m-homekit

# ── Access helper (BEFORE the unit: its ExecStartPre runs it) ──────────────
sa02m_atomic_install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-daemon-access.sh" /usr/local/sbin/sa02m-daemon-access.sh

# ── Conf seed — only if absent (the card owns it afterwards) ───────────────
# The conf is www-data:sa02m-homekit 0640 (§13): the CGI owns and rewrites it,
# the bridge reads it through its group. /etc/sa02m-homekit is www-data's, so
# www-data can plant any name here, and chown/chmod/sed -i follow (or read
# through) a symlink: a planted `sa02m-homekit.conf -> /etc/sudoers.d/x` would
# be handed to www-data (root escalation). Root therefore creates the seed with
# O_CREAT|O_EXCL|O_NOFOLLOW (a planted name makes the create fail, never
# followed) and re-asserts owner/group/mode only on a regular, singly-linked
# file through an O_NOFOLLOW fd; anything else is reported on stderr and left
# alone — the scripts/06-alice.sh pattern. `-I`: root runs this from the
# operator's shell, so neither its cwd nor an inherited PYTHONPATH may inject a
# module. Pinned by the quality row `alice-conf-homes` (section 11).
python3 -I - "$BASE_DIR/etc/sa02m-homekit/sa02m-homekit.conf" /etc/sa02m-homekit sa02m-homekit.conf www-data "$HK_USER" <<'PY' \
    || log WARN "[06c-homekit] конфиг /etc/sa02m-homekit/sa02m-homekit.conf не проверен (python3)"
import grp, os, pwd, stat, sys
src, conf_dir, name, owner, group = sys.argv[1:6]
uid = pwd.getpwnam(owner).pw_uid
gid = grp.getgrnam(group).gr_gid
path = f"{conf_dir}/{name}"
dfd = os.open(conf_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
except FileExistsError:
    fd = None
if fd is not None:
    with open(src, "rb") as fh:
        body = fh.read().replace(b"\r\n", b"\n")
    view = memoryview(body)
    while view:
        view = view[os.write(fd, view):]
    os.fchown(fd, uid, gid)
    os.fchmod(fd, 0o640)
    os.fsync(fd)
    os.close(fd)
    print(f"OK: created {path} (enabled = false)")
    sys.exit(0)
try:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise OSError(f"{stat.filemode(st.st_mode)}, {st.st_nlink} link(s)")
except OSError as e:
    print(f"WARN: {path}: not a regular singly-linked file ({e}) — left alone", file=sys.stderr)
    sys.exit(0)
os.fchown(fd, uid, gid)
os.fchmod(fd, 0o640)
os.close(fd)
print(f"INFO: {path} already exists — kept, owner/group/mode re-asserted")
PY

# ── systemd ────────────────────────────────────────────────────────────────
# Capture BEFORE the unit lands — the only reliable first-install signal. The
# bridge ships DISABLED + STOPPED (`app off`): nothing new listens on the LAN
# until the integrator enables it on the card (promise P3). An operator's
# opt-in is preserved and a running bridge is restarted on the fresh code
# (docs/contracts/installer-refresh-policy.md).
sa02m_svc_capture "$UNIT"
sa02m_atomic_install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-homekit.service" /etc/systemd/system/
systemctl daemon-reload
sa02m_svc_apply "$UNIT" app off

# ── Privileged CGI helper + sudoers (before the CGI that calls them) ───────
sa02m_atomic_install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-homekit-web-trigger.sh" \
    /usr/local/sbin/sa02m-homekit-web-trigger.sh
sa02m_install_sudoers "$BASE_DIR/etc/sudoers.d/sa02m-homekit" /etc/sudoers.d/sa02m-homekit

# ── CGI ────────────────────────────────────────────────────────────────────
install -m 0755 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/cgi-bin/sa02m_homekit_api.cgi" \
    "$WEB_CGI/sa02m_homekit_api.cgi"
sed -i 's/\r$//' "$WEB_CGI/sa02m_homekit_api.cgi"

# ── WWW asset (homekit.js may also arrive via a www-only update) ───────────
if [ -f "$BASE_DIR/www/network_config/static/js/app/homekit.js" ]; then
    install -d -m 0755 "$WEB_ROOT_DIR/static/js/app"
    install -m 0644 -o www-data -g www-data \
        "$BASE_DIR/www/network_config/static/js/app/homekit.js" \
        "$WEB_ROOT_DIR/static/js/app/homekit.js"
fi

# ── Access check — LAST: the grant is TRIED as the bridge, never assumed ────
# Owner/group/mode re-asserted on its conf and the Alice device document, then
# every read the bridge needs (and the writes of its own dirs, the CGI's save)
# attempted as that account; so are reads it must NOT have (panel credentials,
# the other Alice confs). The ACL grant this replaces passed every line check
# and read nothing on the product kernel (bench 1.135, 2026-09-28).
hk_access_rc=0
HK_ACCESS_OUT=$(timeout 120 /usr/local/sbin/sa02m-daemon-access.sh apply homekit 2>&1) || hk_access_rc=$?
if [ "$hk_access_rc" != 0 ]; then
    while IFS= read -r _line; do
        [ -n "$_line" ] && log ERR "[06c-homekit]   $_line"
    done <<<"$HK_ACCESS_OUT"
    log ERR "[06c-homekit] мост HomeKit не получил доступ к своим файлам (код $hk_access_rc) — установка не завершена; проверка: docs/deployment.md «Проверка на стенде»"
    exit 1
fi
while IFS= read -r _line; do
    [ -n "$_line" ] && log INFO "[06c-homekit]   $_line"
done <<<"$HK_ACCESS_OUT"

if [ "$HK_DEPS" = ok ]; then
    log OK "=== [06c-homekit] sa02m-homekit установлен (служба выключена по умолчанию) ==="
else
    log WARN "=== [06c-homekit] sa02m-homekit установлен БЕЗ зависимостей (venv) — мост не запустится до повторной установки ==="
fi
log INFO "UI: Управление → Apple HomeKit → «Включить». Документация: docs/HOMEKIT_INTEGRATION.md"
