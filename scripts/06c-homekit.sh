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
#   → apt python3-venv/paho → system user → venv from the hash-pinned lock
#   → package tree → dirs (tmpfiles) → conf seed → capture → unit → apply
#   (app off) → status fallback → trigger + sudoers → CGI → JS.
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

if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
    log WARN "[06c-homekit] сборка образа: HomeKit не входит в заводской образ (устанавливается интегратором) — пропуск"
    exit 0
fi

# ── Shared dependency first: the Alice package ─────────────────────────────
# The bridge builds its accessories from sa02m_alice's DeviceRegistry over the
# one device document; without the package the daemon cannot import at all.
if [ ! -d /opt/sa02m-alice/sa02m_alice ]; then
    log WARN "[06c-homekit] HomeKit needs Alice package: /opt/sa02m-alice отсутствует (06-alice.sh не выполнен) — HomeKit не установлен"
    exit 0
fi
if [ ! -d "$OPT_SRC/sa02m_homekit" ]; then
    log WARN "[06c-homekit] исходники $OPT_SRC/sa02m_homekit не найдены — пропуск"
    exit 0
fi

# ── System packages ────────────────────────────────────────────────────────
python3 -c "import paho.mqtt" 2>/dev/null || sa02m_pkg_install_tier optional python3-paho-mqtt
python3 -c "import ensurepip, venv" 2>/dev/null || sa02m_pkg_install_tier optional python3-venv
# The locked cryptography wheel is installed --no-deps: its C backend
# `_cffi_backend` comes from apt, never from PyPI (requirements.lock header).
python3 -c "import _cffi_backend" 2>/dev/null || sa02m_pkg_install_tier optional python3-cffi-backend

# ── Unprivileged system user (D11) ─────────────────────────────────────────
# No shell, no home. www-data as a supplementary group lets the daemon read the
# device document (0660 root:www-data) and hand the web card its 0640 /run
# files; www-data is NOT in the daemon's group, so it never reads the pairing
# store (promise P4).
if ! id -u "$HK_USER" >/dev/null 2>&1; then
    if useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --comment "SA-02m HomeKit bridge" "$HK_USER" >>"$LOG_FILE" 2>&1; then
        log OK "создан системный пользователь $HK_USER"
    else
        log WARN "[06c-homekit] не удалось создать пользователя $HK_USER — HomeKit не установлен"
        exit 0
    fi
fi
_hk_groups=$(id -nG "$HK_USER" 2>/dev/null) || _hk_groups=""
case " $_hk_groups " in
    *" www-data "*) : ;;
    *) usermod -a -G www-data "$HK_USER" >>"$LOG_FILE" 2>&1 \
           || log WARN "[06c-homekit] не удалось добавить $HK_USER в группу www-data — мост не прочитает документ устройств" ;;
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
# this module creates (journal noise + an empty 0770 /etc/sa02m-homekit on every
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
# absent in a stripped rootfs).
install -d -m 0700 -o "$HK_USER" -g "$HK_USER" /var/lib/sa02m-homekit
install -d -m 0750 -o "$HK_USER" -g www-data /run/sa02m-homekit
install -d -m 0770 -o root -g www-data /etc/sa02m-homekit

# ── Conf seed — only if absent (the card owns it afterwards) ───────────────
# /etc/sa02m-homekit is root:www-data 0770, so www-data can plant any name
# here, and chmod/chgrp/sed -i follow (or read through) a symlink: a planted
# `sa02m-homekit.conf -> /etc/sudoers.d/x` would get group www-data + 0660
# (root escalation). Root therefore creates the seed with O_CREAT|O_EXCL|
# O_NOFOLLOW (a planted name makes the create fail, never followed) and
# re-asserts group/mode only on a regular, singly-linked file through an
# O_NOFOLLOW fd; anything else is reported on stderr and left alone — the
# scripts/06-alice.sh pattern. Pinned by the quality row `alice-conf-homes`
# (section 11).
python3 - "$BASE_DIR/etc/sa02m-homekit/sa02m-homekit.conf" /etc/sa02m-homekit sa02m-homekit.conf www-data <<'PY' \
    || log WARN "[06c-homekit] конфиг /etc/sa02m-homekit/sa02m-homekit.conf не проверен (python3)"
import grp, os, stat, sys
src, conf_dir, name, group = sys.argv[1:5]
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
    os.fchown(fd, -1, gid)
    os.fchmod(fd, 0o660)
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
os.fchown(fd, -1, gid)
os.fchmod(fd, 0o660)
os.close(fd)
print(f"INFO: {path} already exists — kept, group/mode re-asserted")
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

if [ "$HK_DEPS" = ok ]; then
    log OK "=== [06c-homekit] sa02m-homekit установлен (служба выключена по умолчанию) ==="
else
    log WARN "=== [06c-homekit] sa02m-homekit установлен БЕЗ зависимостей (venv) — мост не запустится до повторной установки ==="
fi
log INFO "UI: Управление → Apple HomeKit → «Включить». Документация: docs/HOMEKIT_INTEGRATION.md"
