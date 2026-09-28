#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  06d-homeconnect.sh  —  BSH Home Connect client (sa02m-homeconnect)
# Opt-in module: install.sh runs it only with --with-homeconnect /
# SA02M_WITH_HOMECONNECT=1 or when the client is already installed (refresh
# keeps it current). Never in the factory image. Read-only release (Operator
# decision Q-E). Contract: docs/contracts/home-connect.md.
#
# Order is a floor, each step BEFORE the one that consumes it:
#   apt paho (05-mqtt.sh normally has it) → system user → package tree
#   → dirs (tmpfiles) → conf seed (rendered BY the installed package)
#   → capture → unit → apply (app off) → trigger + sudoers → CGI → JS.
# No pip, no venv: the daemon is stdlib + apt python3-paho-mqtt, so OTA alone
# can carry every later code update; only the user, the dirs and the conf seed
# need this installer (a board updated by OTA alone shows `not_installed`).
# Idempotent: a re-run on a configured board refreshes code only — the conf,
# the tokens, the call budget and the operator's enable/run state are never
# touched.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [06d-homeconnect] Установка sa02m-homeconnect (BSH Home Connect, только чтение) ==="

BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$BASE_DIR/opt/sa02m-homeconnect"
UNIT_SRC="$BASE_DIR/etc/systemd/system"
WEB_ROOT_DIR="${WEB_ROOT:-/var/www/network_config}"
WEB_CGI="$WEB_ROOT_DIR/cgi-bin"
INSTALL_DIR=/opt/sa02m-homeconnect
HC_USER=sa02m-homeconnect
UNIT=sa02m-homeconnect.service

if [ -n "${SA02M_ROOTFS_BUILD:-}" ]; then
    log WARN "[06d-homeconnect] сборка образа: Home Connect не входит в заводской образ (устанавливается интегратором) — пропуск"
    exit 0
fi
if [ ! -d "$OPT_SRC/sa02m_homeconnect" ]; then
    log WARN "[06d-homeconnect] исходники $OPT_SRC/sa02m_homeconnect не найдены — пропуск"
    exit 0
fi

# ── System package: paho (the daemon's only non-stdlib import) ─────────────
# 05-mqtt.sh installs it on every board; re-checked here so the module stands
# alone. Missing after this ⇒ the daemon reports `missing_deps` (never a
# crash loop) and the card says so.
python3 -c "import paho.mqtt" 2>/dev/null || sa02m_pkg_install_tier optional python3-paho-mqtt

# ── Unprivileged system user (plan D11) ────────────────────────────────────
# No shell, no home. www-data as a supplementary group lets the daemon hand
# the web card its 0640 /run files; www-data is NOT in the daemon's group, so
# it never reads the tokens (promise P4).
if ! id -u "$HC_USER" >/dev/null 2>&1; then
    if useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --comment "SA-02m Home Connect client" "$HC_USER" >>"$LOG_FILE" 2>&1; then
        log OK "создан системный пользователь $HC_USER"
    else
        log WARN "[06d-homeconnect] не удалось создать пользователя $HC_USER — Home Connect не установлен"
        exit 0
    fi
fi
_hc_groups=$(id -nG "$HC_USER" 2>/dev/null) || _hc_groups=""
case " $_hc_groups " in
    *" www-data "*) : ;;
    *) usermod -a -G www-data "$HC_USER" >>"$LOG_FILE" 2>&1 \
           || log WARN "[06d-homeconnect] не удалось добавить $HC_USER в группу www-data — карточка не прочитает состояние входа" ;;
esac

# ── Package tree (BEFORE the conf seed: the seed is rendered by it) ────────
install -d -m 0755 -o root -g root "$INSTALL_DIR"
log INFO "Копирую $OPT_SRC → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
    "$OPT_SRC/" "$INSTALL_DIR/"
chown -R root:root "$INSTALL_DIR"
chmod -R u=rwX,go=rX "$INSTALL_DIR"

# ── Runtime dirs (boot-persistent home: the package's tmpfiles.d copy) ─────
# The conf lives in the package, not under etc/tmpfiles.d/: everything there is
# OTA'd to EVERY board, and its lines name the sa02m-homeconnect account that
# only this module creates. This installer is its only writer into
# /etc/tmpfiles.d/ (pinned by `alice-conf-homes` section 15).
install -m 0644 -o root -g root \
    "$OPT_SRC/tmpfiles.d/sa02m-homeconnect.conf" /etc/tmpfiles.d/sa02m-homeconnect.conf
sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-homeconnect.conf
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-homeconnect.conf >>"$LOG_FILE" 2>&1 \
        || log WARN "[06d-homeconnect] systemd-tmpfiles --create вернул ошибку — каталоги создаю напрямую"
fi
# Same modes as the tmpfiles entries (idempotent re-assert; tmpfiles may be
# absent in a stripped rootfs).
install -d -m 0700 -o "$HC_USER" -g "$HC_USER" /var/lib/sa02m-homeconnect
install -d -m 0750 -o "$HC_USER" -g www-data /run/sa02m-homeconnect
install -d -m 0770 -o root -g www-data /etc/sa02m-homeconnect

# ── Conf seed — only if absent (the card owns it afterwards) ───────────────
# The seed is the package's OWN render of the defaults (config.render(
# ClientConfig()): enabled = false, no Client ID, host api) — one home, no
# template file to drift from the parser. /etc/sa02m-homeconnect is
# root:www-data 0770, so www-data can plant any name here, and chmod/chgrp
# follow a symlink: a planted `sa02m-homeconnect.conf -> /etc/sudoers.d/x`
# would get group www-data + 0660 (root escalation). Root therefore creates the
# seed with O_CREAT|O_EXCL|O_NOFOLLOW (a planted name makes the create fail,
# never followed) and re-asserts group/mode only on a regular, singly-linked
# file through an O_NOFOLLOW fd; anything else is reported on stderr and left
# alone — the scripts/06c-homekit.sh pattern. `-I`: root runs this from the
# operator's shell, so neither its cwd nor an inherited PYTHONPATH may inject a
# module; the package root is inserted explicitly (root-owned, just installed).
# `-B`: no __pycache__ written into /opt as root. Pinned by the quality row
# `alice-conf-homes` (section 14).
python3 -I -B - "$INSTALL_DIR" /etc/sa02m-homeconnect sa02m-homeconnect.conf www-data <<'PY' \
    || log WARN "[06d-homeconnect] конфиг /etc/sa02m-homeconnect/sa02m-homeconnect.conf не проверен (python3)"
import grp, os, stat, sys
pkg, conf_dir, name, group = sys.argv[1:5]
gid = grp.getgrnam(group).gr_gid
path = f"{conf_dir}/{name}"
dfd = os.open(conf_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
except FileExistsError:
    fd = None
if fd is not None:
    try:
        sys.path.insert(0, pkg)
        from sa02m_homeconnect import config
        body = config.render(config.ClientConfig()).encode("utf-8")
    except Exception:
        os.close(fd)
        os.unlink(name, dir_fd=dfd)
        raise
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
# client ships DISABLED + STOPPED (`app off`): nothing talks to the BSH cloud
# until the integrator enables it on the card. An operator's opt-in is
# preserved and a running client is restarted on the fresh code
# (docs/contracts/installer-refresh-policy.md).
sa02m_svc_capture "$UNIT"
sa02m_atomic_install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-homeconnect.service" /etc/systemd/system/
systemctl daemon-reload
sa02m_svc_apply "$UNIT" app off

# ── Privileged CGI helper + sudoers (before the CGI that calls them) ───────
sa02m_atomic_install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-homeconnect-web-trigger.sh" \
    /usr/local/sbin/sa02m-homeconnect-web-trigger.sh
sa02m_install_sudoers "$BASE_DIR/etc/sudoers.d/sa02m-homeconnect" /etc/sudoers.d/sa02m-homeconnect

# ── CGI ────────────────────────────────────────────────────────────────────
install -m 0755 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/cgi-bin/sa02m_homeconnect_api.cgi" \
    "$WEB_CGI/sa02m_homeconnect_api.cgi"
sed -i 's/\r$//' "$WEB_CGI/sa02m_homeconnect_api.cgi"

# ── WWW asset (homeconnect.js may also arrive via a www-only update) ───────
if [ -f "$BASE_DIR/www/network_config/static/js/app/homeconnect.js" ]; then
    install -d -m 0755 "$WEB_ROOT_DIR/static/js/app"
    install -m 0644 -o www-data -g www-data \
        "$BASE_DIR/www/network_config/static/js/app/homeconnect.js" \
        "$WEB_ROOT_DIR/static/js/app/homeconnect.js"
fi

if python3 -c "import paho.mqtt.client" 2>/dev/null; then
    log OK "=== [06d-homeconnect] sa02m-homeconnect установлен (служба выключена по умолчанию) ==="
else
    log WARN "=== [06d-homeconnect] sa02m-homeconnect установлен БЕЗ python3-paho-mqtt — клиент покажет «нет компонентов» ==="
fi
log INFO "UI: Управление → Home Connect → Client ID → «Включить» → «Подключить». Документация: docs/HOME_CONNECT_INTEGRATION.md"
