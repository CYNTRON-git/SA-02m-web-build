#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# СА-02м  •  15-audio.sh  —  on-command audio recorder (sa02m-audio)
# Installed on EVERY board and shipped OFF (Operator decision F6): no process
# runs and nothing records until the Operator enables it on the card.
# Contract: docs/contracts/audio-recorder.md.
#
# Order is a floor, each step BEFORE the one that consumes it:
#   apt paho (05-mqtt.sh normally has it) → system user → package tree
#   → dirs (tmpfiles) → access helper → conf seed (rendered BY the installed
#   package) → capture → unit → apply (app off) → trigger + sudoers → CGI → JS
#   → access check (LAST: the grant is tried as the daemon, fatal).
# install.sh runs this after 06b-rules.sh: the recorder's upload path imports
# sa02m_rules.http_guard (shared dependency before its consumer).
# No pip, no venv: stdlib + apt python3-paho-mqtt, so OTA alone carries every
# later code update; only the user, the dirs and the conf seed need this
# installer (a board updated by OTA alone shows `not_installed`).
# Idempotent: a re-run on a configured board refreshes code only — the conf,
# the voice allow list, the recordings, the events and the operator's
# enable/run state are never touched (conf and allow list are seeded only
# when absent).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
check_root

log INFO "=== [15-audio] Установка sa02m-audio (запись звука по команде, выключена) ==="

BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPT_SRC="$BASE_DIR/opt/sa02m-audio"
UNIT_SRC="$BASE_DIR/etc/systemd/system"
WEB_ROOT_DIR="${WEB_ROOT:-/var/www/network_config}"
WEB_CGI="$WEB_ROOT_DIR/cgi-bin"
INSTALL_DIR=/opt/sa02m-audio
AU_USER=sa02m-audio
UNIT=sa02m-audio.service

if [ ! -d "$OPT_SRC/sa02m_audio" ]; then
    log WARN "[15-audio] исходники $OPT_SRC/sa02m_audio не найдены — пропуск"
    exit 0
fi

# ── System package: paho (the daemon's only non-stdlib import) ─────────────
# 05-mqtt.sh installs it on every board; re-checked here so the module stands
# alone. Missing after this ⇒ the daemon reports `missing_deps`.
python3 -c "import paho.mqtt" 2>/dev/null || sa02m_pkg_install_tier optional python3-paho-mqtt

# ── Unprivileged system user ───────────────────────────────────────────────
# No shell, no home, and in NO group but its own: group `audio` (the
# microphone) comes from the unit's SupplementaryGroups=audio — its one home —
# and www-data would let the recorder read the panel credentials. What the
# panel needs from the recorder is a mode, not a membership (setgid store and
# /run dirs, docs/contracts/audio-recorder.md §13; gate
# `daemon-least-privilege`).
if ! id -u "$AU_USER" >/dev/null 2>&1; then
    if useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --comment "SA-02m audio recorder" "$AU_USER" >>"$LOG_FILE" 2>&1; then
        log OK "создан системный пользователь $AU_USER"
    else
        log WARN "[15-audio] не удалось создать пользователя $AU_USER — запись звука не установлена"
        exit 0
    fi
fi
# A hand-added www-data membership is taken back out (the running daemon
# picks the groups up at the restart `sa02m_svc_apply` does below).
_au_groups=$(id -nG "$AU_USER" 2>/dev/null) || _au_groups=""
case " $_au_groups " in
    *" www-data "*)
        if gpasswd -d "$AU_USER" www-data >>"$LOG_FILE" 2>&1; then
            log OK "$AU_USER исключён из группы www-data"
        else
            log WARN "[15-audio] не удалось исключить $AU_USER из группы www-data (gpasswd -d $AU_USER www-data)"
        fi ;;
esac

# ── Package tree (BEFORE the conf seed: the seed is rendered by it) ────────
install -d -m 0755 -o root -g root "$INSTALL_DIR"
log INFO "Копирую $OPT_SRC → $INSTALL_DIR"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
    "$OPT_SRC/" "$INSTALL_DIR/"
chown -R root:root "$INSTALL_DIR"
chmod -R u=rwX,go=rX "$INSTALL_DIR"

# ── Runtime dirs (boot-persistent home: the package's tmpfiles.d copy) ─────
# The conf lives in the package, not under etc/tmpfiles.d/: its lines name the
# sa02m-audio account that only this module creates.
install -m 0644 -o root -g root \
    "$OPT_SRC/tmpfiles.d/sa02m-audio.conf" /etc/tmpfiles.d/sa02m-audio.conf
sed -i 's/\r$//' /etc/tmpfiles.d/sa02m-audio.conf
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/sa02m-audio.conf >>"$LOG_FILE" 2>&1 \
        || log WARN "[15-audio] systemd-tmpfiles --create вернул ошибку — каталоги создаю напрямую"
fi
# Same modes as the tmpfiles entries (idempotent re-assert). The store and
# rec/: the daemon writes, the panel reads through the setgid group; tmp/ is
# the daemon's alone. The conf dir: www-data writes it (atomic save), setgid
# sa02m-audio makes the conf saved there the recorder's group.
install -d -m 2750 -o "$AU_USER" -g www-data /var/lib/sa02m-audio
install -d -m 2750 -o "$AU_USER" -g www-data /var/lib/sa02m-audio/rec
install -d -m 0700 -o "$AU_USER" -g "$AU_USER" /var/lib/sa02m-audio/tmp
install -d -m 2750 -o "$AU_USER" -g www-data /run/sa02m-audio
install -d -m 2750 -o www-data -g sa02m-audio /etc/sa02m-audio

# ── Access helper (BEFORE the unit: its ExecStartPre runs it) ──────────────
sa02m_atomic_install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-daemon-access.sh" /usr/local/sbin/sa02m-daemon-access.sh

# ── Conf + voice allow list seed — only if absent (the card / the Operator ─
# own them afterwards). The conf is the package's OWN render of the defaults
# (config.render(RecorderConfig()): enabled = false, storage = emmc); the
# allow list is a copy of /opt/sa02m-audio/voice-allow.default.json (its one
# home), seeded only after the installed voice.load_allow accepts it, and
# inert while the conf's voice_commands = false (the default). A re-run
# never overwrites either: that is why sa02m_atomic_install is NOT used here
# — it replaces the live file on every run (not idempotent for an
# Operator-edited file), and its rename-over + chown into this directory is
# exactly what a planted name could redirect. /etc/sa02m-audio is www-data's,
# so www-data can plant any name there, and chown/chmod follow a symlink:
# root creates each seed with O_CREAT|O_EXCL|O_NOFOLLOW and re-asserts
# owner/group/mode only on a regular, singly-linked file through an O_NOFOLLOW
# fd (the scripts/06d-homeconnect.sh pattern). `-I -B`: no module from the
# operator's cwd or PYTHONPATH, no __pycache__ written into /opt as root.
python3 -I -B - "$INSTALL_DIR" /etc/sa02m-audio www-data "$AU_USER" sa02m-audio.conf voice-allow.json <<'PY' \
    || log WARN "[15-audio] конфиг или белый список голосовых команд в /etc/sa02m-audio не проверены (python3)"
import grp, os, pwd, stat, sys
pkg, conf_dir, owner, group = sys.argv[1:5]
uid = pwd.getpwnam(owner).pw_uid
gid = grp.getgrnam(group).gr_gid
sys.path.insert(0, pkg)


def body_of(name):
    if name == "sa02m-audio.conf":
        from sa02m_audio import config
        return config.render(config.RecorderConfig()).encode("utf-8"), "enabled = false, storage = emmc"
    if name == "voice-allow.json":
        from sa02m_audio import voice
        src = os.path.join(pkg, "voice-allow.default.json")
        if voice.load_allow(src) is None:
            raise ValueError(f"{src} is not a valid allow list")
        with open(src, "rb") as fh:
            return fh.read(), "default LED controls + light_music, inert while voice_commands = false"
    raise ValueError(f"no seed for {name}")


def seed(dfd, name):
    path = f"{conf_dir}/{name}"
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    except FileExistsError:
        fd = None
    if fd is not None:
        try:
            body, what = body_of(name)
        except Exception:
            os.close(fd)
            os.unlink(name, dir_fd=dfd)
            raise
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view):]
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o640)
        os.fsync(fd)
        os.close(fd)
        print(f"OK: created {path} ({what})")
        return
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            raise OSError(f"{stat.filemode(st.st_mode)}, {st.st_nlink} link(s)")
    except OSError as e:
        print(f"WARN: {path}: not a regular singly-linked file ({e}) — left alone", file=sys.stderr)
        return
    os.fchown(fd, uid, gid)
    os.fchmod(fd, 0o640)
    os.close(fd)
    print(f"INFO: {path} already exists — kept, owner/group/mode re-asserted")


dfd = os.open(conf_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
failed = 0
for name in sys.argv[5:]:
    try:
        seed(dfd, name)
    except Exception as e:
        print(f"WARN: {conf_dir}/{name}: not seeded ({e})", file=sys.stderr)
        failed = 1
sys.exit(failed)
PY

# ── systemd ────────────────────────────────────────────────────────────────
# Capture BEFORE the unit lands — the only reliable first-install signal. The
# recorder ships DISABLED + STOPPED (`app off`); an operator's opt-in is kept
# and a running recorder is restarted on the fresh code
# (docs/contracts/installer-refresh-policy.md).
sa02m_svc_capture "$UNIT"
sa02m_atomic_install -m 0644 -o root -g root \
    "$UNIT_SRC/sa02m-audio.service" /etc/systemd/system/
systemctl daemon-reload
sa02m_svc_apply "$UNIT" app off

# ── Privileged CGI helper + sudoers (before the CGI that calls them) ───────
sa02m_atomic_install -m 0755 -o root -g root \
    "$BASE_DIR/usr/local/sbin/sa02m-audio-web-trigger.sh" \
    /usr/local/sbin/sa02m-audio-web-trigger.sh
sa02m_install_sudoers "$BASE_DIR/etc/sudoers.d/sa02m-audio" /etc/sudoers.d/sa02m-audio

# ── 1884 ACL: no direct recording command from the external listener ───────
# A board installed before this module carries an ACL without the deny line
# (05-mqtt.sh seeds the template only when no ACL exists). The line goes INTO
# the `user mqttuser` block: a `pattern deny` is not applied to a user that
# has a grant (mosquitto 2.0.18, bench 1.135, 2026-10-15). Idempotent: the
# exact line present ⇒ nothing changes and nothing is reloaded. An ACL that
# is not a regular file, or has no mqttuser block (a hand-written one), is
# left alone with a WARN. mosquitto re-reads acl_file on SIGHUP (its unit's
# ExecReload), bounded by timeout, and only when the file changed.
# >>> ACL MIGRATION (extracted by scripts/dev/test-mqtt-acl-audio.sh)
AU_ACL_DENY='topic deny /devices/sa02m-audio/controls/+/on'
au_acl_migrate() {
    local acl=$1 tmp
    if [ ! -e "$acl" ] && [ ! -L "$acl" ]; then
        log INFO "[15-audio] $acl нет — внешний слушатель MQTT не настроен, ACL не дополняется"
        return 0
    fi
    if [ -L "$acl" ] || [ ! -f "$acl" ]; then
        log WARN "[15-audio] $acl — не обычный файл, не тронут; добавьте вручную: $AU_ACL_DENY"
        return 0
    fi
    if awk -v d="$AU_ACL_DENY" '{ sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "") } $0 == d { f = 1 } END { exit !f }' "$acl"; then
        return 0
    fi
    if ! grep -Eq '^[[:space:]]*user[[:space:]]+mqttuser[[:space:]]*$' "$acl"; then
        log WARN "[15-audio] в $acl нет блока «user mqttuser» — запрет записи звука с порта 1884 не добавлен; добавьте вручную: $AU_ACL_DENY"
        return 0
    fi
    tmp=$(mktemp "$acl.au.XXXXXX") || { log WARN "[15-audio] mktemp рядом с $acl не удался — ACL не дополнен"; return 0; }
    awk -v d="$AU_ACL_DENY" '{ print } /^[[:space:]]*user[[:space:]]+mqttuser[[:space:]]*$/ && !done { print d; done = 1 }' \
        "$acl" > "$tmp"
    if ! sa02m_atomic_install -m 0640 -o root -g mosquitto "$tmp" "$acl"; then
        rm -f -- "$tmp"
        log WARN "[15-audio] $acl не обновлён — запрет записи звука с порта 1884 не действует"
        return 0
    fi
    rm -f -- "$tmp"
    log OK "в $acl добавлен запрет прямой команды записи звука с порта 1884"
    if systemctl is-active --quiet mosquitto; then
        timeout 10 systemctl reload mosquitto >>"$LOG_FILE" 2>&1 \
            || log WARN "[15-audio] systemctl reload mosquitto не удался — запрет вступит в силу после перезапуска брокера"
    fi
    return 0
}
# <<< ACL MIGRATION
au_acl_migrate /etc/mosquitto/acl/default.conf

# ── CGI ────────────────────────────────────────────────────────────────────
install -m 0755 -o www-data -g www-data \
    "$BASE_DIR/www/network_config/cgi-bin/sa02m_audio_api.cgi" \
    "$WEB_CGI/sa02m_audio_api.cgi"
sed -i 's/\r$//' "$WEB_CGI/sa02m_audio_api.cgi"

# ── WWW asset (audio.js may also arrive via a www-only update) ─────────────
if [ -f "$BASE_DIR/www/network_config/static/js/app/audio.js" ]; then
    install -d -m 0755 "$WEB_ROOT_DIR/static/js/app"
    install -m 0644 -o www-data -g www-data \
        "$BASE_DIR/www/network_config/static/js/app/audio.js" \
        "$WEB_ROOT_DIR/static/js/app/audio.js"
fi

# ── Access check — LAST: the grant is TRIED as the recorder, never assumed ──
au_access_rc=0
AU_ACCESS_OUT=$(timeout 120 /usr/local/sbin/sa02m-daemon-access.sh apply audio 2>&1) || au_access_rc=$?
if [ "$au_access_rc" != 0 ]; then
    while IFS= read -r _line; do
        [ -n "$_line" ] && log ERR "[15-audio]   $_line"
    done <<<"$AU_ACCESS_OUT"
    log ERR "[15-audio] запись звука не получила доступ к своему конфигу и хранилищу (код $au_access_rc) — установка не завершена"
    exit 1
fi
while IFS= read -r _line; do
    [ -n "$_line" ] && log INFO "[15-audio]   $_line"
done <<<"$AU_ACCESS_OUT"

log OK "=== [15-audio] sa02m-audio установлен (служба выключена по умолчанию) ==="
log INFO "UI: Управление → Запись звука → «Включить». Контракт: docs/contracts/audio-recorder.md"
