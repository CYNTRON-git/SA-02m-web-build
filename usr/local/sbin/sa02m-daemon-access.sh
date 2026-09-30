#!/bin/bash
# sa02m-daemon-access.sh check|apply homekit|homeconnect
#
# The least-privilege grant of the HomeKit bridge / Home Connect client, and
# the PROOF that it works. Contracts: docs/contracts/homekit-bridge.md §13,
# docs/contracts/home-connect.md §11; threat model docs/threat-model.md §3.
#
# Neither daemon is in group www-data (that group reads the panel credentials,
# the gateway YAML and every Alice conf). Read is granted by plain Unix groups
# — no POSIX ACL: the product RT kernel has none, and an ACL grant there
# silently did nothing (bench 1.135, 2026-09-28):
#   * the conf dir is www-data:<daemon> 2750 — the CGI (owner) saves the conf
#     atomically, and setgid makes every file created there the daemon's
#     group; the conf is www-data:<daemon> 0640 (the daemon reads, never writes);
#   * HomeKit only: the Alice device document is www-data:sa02m-alice-devices
#     0640 — a group of exactly www-data and sa02m-homekit, set up by
#     scripts/06c-homekit.sh — and /etc/sa02m-alice carries o+x (traverse, no
#     listing; the other Alice confs stay unreadable to the bridge).
#
#   apply  heal, then check. Dirs: the `d` lines of the package's OWN tmpfiles
#          conf (/opt/…/tmpfiles.d — the one home of the dir modes, current
#          after an OTA, which never copies it to /etc/tmpfiles.d); files: the
#          owner/group/mode above, set through an O_NOFOLLOW fd on a regular,
#          singly-linked file only, and only on a file ALREADY owned by
#          www-data or by root with group www-data / the target group — both
#          conf dirs are www-data-writable, so www-data can rename any file it
#          reaches onto the name: a symlink, a hard link, a FIFO, a root:root or
#          any foreign file is refused with ОШИБКА (exit 1), never taken over.
#          A stale ACL an older install left is removed.
#   (both) a file probed for read must be a REGULAR file: a FIFO at the conf
#          name passes `test -r` and blocks the daemon that opens it.
#   check  only the probes: every access the daemon and the CGI need is TRIED
#          as that account — setpriv with the account's uid, gid and /etc/group
#          groups (what systemd gives the unit) — bounded by timeout; so is
#          every access the daemon must NOT have.
#
# Exit 0 only when every probe answered as required; 1 on any failed probe
# (one Russian line per failure naming the path, owner:group and mode), 2 on
# bad usage. A failed run never prints the success line.
#
# Callers: scripts/06c-homekit.sh and scripts/06d-homeconnect.sh as their LAST
# step (fatal), and each unit's `ExecStartPre=-+` (every start, non-fatal: the
# daemon then reports `conf_unreadable` on the card and these lines are in its
# journal). Harness: scripts/dev/test-daemon-access.sh runs this file with its
# absolute paths re-pointed into a sandbox and `id`/`getent` as PATH shims.
set -u

VERB=${1:-}
KIND=${2:-}
WEB_USER=www-data
DEVDOC_GROUP=
ALICE_DIR=
DEVDOC=
case "$KIND" in
    homekit)
        DAEMON=sa02m-homekit
        CONF_DIR=/etc/sa02m-homekit
        CONF=/etc/sa02m-homekit/sa02m-homekit.conf
        VAR_DIR=/var/lib/sa02m-homekit
        RUN_DIR=/run/sa02m-homekit
        PKG_TMPFILES=/opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf
        DEVDOC_GROUP=sa02m-alice-devices
        ALICE_DIR=/etc/sa02m-alice
        DEVDOC=/etc/sa02m-alice/sa02m-alice-devices.conf
        # What the grant must NOT reach: the panel credentials, the other Alice confs.
        DENY_READ="/etc/sa02m_web.env /etc/sa02m-alice/sa02m-alice-client.conf /etc/sa02m-alice/sa02m-alice-server.conf"
        ;;
    homeconnect)
        DAEMON=sa02m-homeconnect
        CONF_DIR=/etc/sa02m-homeconnect
        CONF=/etc/sa02m-homeconnect/sa02m-homeconnect.conf
        VAR_DIR=/var/lib/sa02m-homeconnect
        RUN_DIR=/run/sa02m-homeconnect
        PKG_TMPFILES=/opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf
        DENY_READ="/etc/sa02m_web.env /etc/sa02m-alice/sa02m-alice-devices.conf"
        ;;
    *)
        echo "usage: $0 check|apply homekit|homeconnect" >&2
        exit 2 ;;
esac
case "$VERB" in
    check|apply) ;;
    *) echo "usage: $0 check|apply homekit|homeconnect" >&2; exit 2 ;;
esac

fails=0
probes=0
fail() { printf 'ОШИБКА: %s\n' "$1"; fails=$((fails + 1)); }
note() { printf '%s\n' "$1"; }
desc() { stat -c '%U:%G %a' "$1" 2>/dev/null || echo '?'; }

# Name -> numeric id through `id`/`getent` (the harness shims both).
uid_of()    { id -u "$1" 2>/dev/null; }
gid_of()    { id -g "$1" 2>/dev/null; }
groups_of() { id -G "$1" 2>/dev/null; }
group_gid() {
    local line
    line=$(getent group "$1" 2>/dev/null) || return 1
    line=${line#*:}
    line=${line#*:}
    [ -n "${line%%:*}" ] || return 1
    printf '%s\n' "${line%%:*}"
}

DAEMON_UID=$(uid_of "$DAEMON") && DAEMON_GID=$(gid_of "$DAEMON") || {
    fail "учётной записи $DAEMON нет — модуль не установлен"
    exit 1
}
WEB_UID=$(uid_of "$WEB_USER") || { fail "учётной записи $WEB_USER нет"; exit 1; }
WEB_GID=$(group_gid "$WEB_USER") || WEB_GID=
DEVDOC_GID=
if [ -n "$DEVDOC_GROUP" ]; then
    DEVDOC_GID=$(group_gid "$DEVDOC_GROUP") || DEVDOC_GID=
fi

# ── heal ───────────────────────────────────────────────────────────────────
if [ "$VERB" = apply ]; then
    ops=()
    if [ -f "$PKG_TMPFILES" ]; then
        while read -r typ path mode user group _rest; do
            [ "$typ" = d ] || continue
            case "$path" in
                "$CONF_DIR"|"$VAR_DIR"|"$RUN_DIR") ;;
                *) continue ;;
            esac
            u=$(uid_of "$user") && g=$(group_gid "$group") \
                || { fail "$PKG_TMPFILES: $user:$group для $path не существует"; continue; }
            ops+=("d:$path:$mode:$u:$g")
        done < "$PKG_TMPFILES"
    else
        fail "нет $PKG_TMPFILES — режимы каталогов не восстановлены (переустановите модуль)"
    fi
    # f:path:mode:uid:gid:root-gids — the last field is the owner rule (see
    # the python below): root-owned files are taken only with one of these groups.
    ops+=("f:$CONF:0640:$WEB_UID:$DAEMON_GID:${WEB_GID:-x},$DAEMON_GID")
    if [ -n "$DEVDOC" ]; then
        # Traverse only; the mode's home is etc/tmpfiles.d/sa02m-alice.conf —
        # ensured here too so an Alice copy older than that line cannot cut
        # the bridge off.
        ops+=("x:$ALICE_DIR")
        if [ -n "$DEVDOC_GID" ]; then
            ops+=("f:$DEVDOC:0640:$WEB_UID:$DEVDOC_GID:${WEB_GID:-x},$DEVDOC_GID")
        else
            fail "группы $DEVDOC_GROUP нет — документ устройств мосту не выдан (запустите scripts/06c-homekit.sh)"
        fi
    fi
    heal_rc=0
    python3 -I - "${ops[@]}" <<'PY' || heal_rc=$?
import errno, os, stat, sys

DIR_RD = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_RD = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
IGNORED = {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}


def drop_acl(fd, path, names):
    # Only an ACL the inode really carries: removing an absent POSIX ACL
    # "succeeds" on an ACL-capable kernel, which would report a removal.
    try:
        present = set(os.listxattr(fd))
    except OSError:
        return
    for name in names:
        if name not in present:
            continue
        try:
            os.removexattr(fd, name)
            print(f"удалён устаревший ACL {name} с {path}")
        except OSError as e:
            if e.errno not in IGNORED:
                print(f"ПРЕДУПРЕЖДЕНИЕ: {path}: ACL {name} не удалён ({e.strerror})")


errors = 0


def refuse(msg):
    global errors
    errors += 1
    print(f"ОШИБКА: {msg}")


for op in sys.argv[1:]:
    kind, path, *rest = op.split(":")
    try:
        if kind == "d":
            mode, uid, gid = int(rest[0], 8), int(rest[1]), int(rest[2])
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            fd = os.open(path, DIR_RD)
            try:
                drop_acl(fd, path, ("system.posix_acl_default", "system.posix_acl_access"))
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        elif kind == "x":
            fd = os.open(path, DIR_RD)
            try:
                drop_acl(fd, path, ("system.posix_acl_default", "system.posix_acl_access"))
                st = os.fstat(fd)
                if not st.st_mode & 0o001:
                    os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o001)
            finally:
                os.close(fd)
        elif kind == "f":
            mode, uid, gid = int(rest[0], 8), int(rest[1]), int(rest[2])
            root_gids = {int(x) for x in rest[3].split(",") if x.isdigit()}
            parent, name = os.path.split(path)
            dfd = os.open(parent, DIR_RD)
            try:
                try:
                    fd = os.open(name, FILE_RD, dir_fd=dfd)
                except FileNotFoundError:
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                        refuse(f"{path}: не обычный файл с одной ссылкой "
                               f"({stat.filemode(st.st_mode)}, {st.st_nlink}) — не тронут")
                        continue
                    # The dir is www-data's: it can rename ANY file it reaches
                    # in a non-sticky dir onto this name. Only a file already
                    # in the grant's shape changes hands — owned by www-data,
                    # or root with group www-data / the target group (every
                    # older layout); a root:root or foreign file is refused.
                    if not (st.st_uid == uid or (st.st_uid == 0 and st.st_gid in root_gids)):
                        refuse(f"{path}: владелец {st.st_uid}:{st.st_gid} — не www-data и не root "
                               f"с группой www-data или группой доступа; права не переданы "
                               f"(чужой файл под этим именем — проверьте, откуда он)")
                        continue
                    drop_acl(fd, path, ("system.posix_acl_access",))
                    os.fchown(fd, uid, gid)
                    os.fchmod(fd, mode)
                finally:
                    os.close(fd)
            finally:
                os.close(dfd)
    except OSError as e:
        # A symlink at the name (ELOOP) or any refusal: reported, never followed.
        refuse(f"{path}: права не восстановлены ({e.strerror})")
sys.exit(3 if errors else 0)
PY
    case "$heal_rc" in
        0) ;;
        3) fails=$((fails + 1)) ;;   # the python printed each ОШИБКА itself
        *) fail "восстановление прав не выполнено (python3, код $heal_rc)" ;;
    esac
fi

# ── check ──────────────────────────────────────────────────────────────────
PROBE_WITH=
if command -v setpriv >/dev/null 2>&1; then
    PROBE_WITH=setpriv
elif command -v runuser >/dev/null 2>&1; then
    PROBE_WITH=runuser
fi

# probe yes|no <user> r|w|x <path> — r: read a file (or list+traverse a dir);
# w: create in a dir; x: traverse a dir.
probe() {
    local want=$1 user=$2 op=$3 path=$4 uid gid grps rc=0 t verb
    case "$op" in
        # A file must be REGULAR: a FIFO at the conf name passes `test -r` and
        # blocks the daemon that opens it.
        r) t='test -r "$1" && { if test -d "$1"; then test -x "$1"; else test -f "$1"; fi; }'; verb="читать" ;;
        w) t='test -w "$1" && test -x "$1"'; verb="писать в" ;;
        x) t='test -x "$1"'; verb="входить в" ;;
    esac
    if [ ! -e "$path" ]; then
        # A file absent is not a grant failure (an absent conf reads as
        # disabled); a directory the daemon writes must exist.
        if [ "$want" = yes ] && [ "$op" != r ]; then fail "нет каталога $path"; fi
        return 0
    fi
    uid=$(uid_of "$user") && gid=$(gid_of "$user") && grps=$(groups_of "$user") \
        || { fail "учётной записи $user нет — $path не проверен"; return 0; }
    probes=$((probes + 1))
    case "$PROBE_WITH" in
        setpriv) timeout 5 setpriv --reuid="$uid" --regid="$gid" --groups="${grps// /,}" \
                     -- /bin/sh -c "$t" _ "$path" || rc=$? ;;
        runuser) timeout 5 runuser -u "$user" -- /bin/sh -c "$t" _ "$path" || rc=$? ;;
        *)       fail "нет ни setpriv, ни runuser — доступ $user к $path не проверен"; return 0 ;;
    esac
    if [ "$rc" = 124 ]; then
        fail "проверка доступа $user к $path зависла (timeout 5 с)"
    elif [ "$want" = yes ] && [ "$rc" != 0 ]; then
        fail "$user не может $verb $path ($(desc "$path"))"
    elif [ "$want" = no ] && [ "$rc" = 0 ]; then
        fail "$user может $verb $path — лишний доступ ($(desc "$path"))"
    fi
}

case " $(groups_of "$DAEMON") " in
    *" ${WEB_GID:-none} "*) fail "$DAEMON состоит в группе $WEB_USER (gpasswd -d $DAEMON $WEB_USER)" ;;
esac

probe yes "$DAEMON" r "$CONF"
probe yes "$DAEMON" w "$VAR_DIR"
probe yes "$DAEMON" w "$RUN_DIR"
probe yes "$WEB_USER" r "$CONF"
probe yes "$WEB_USER" w "$CONF_DIR"
probe no "$WEB_USER" x "$VAR_DIR"
if [ -n "$DEVDOC" ]; then
    probe yes "$DAEMON" r "$DEVDOC"
    probe yes "$WEB_USER" r "$DEVDOC"
    probe yes "$WEB_USER" w "$ALICE_DIR"
fi
for f in $DENY_READ; do
    probe no "$DAEMON" r "$f"
done

# Non-vacuity: the three dir probes always run where the module is installed.
if [ "$probes" -lt 3 ] && [ "$fails" = 0 ]; then
    fail "выполнено только $probes проверок доступа — проверять нечего"
fi
if [ "$fails" -gt 0 ]; then
    printf 'ОШИБКА: права %s не в порядке (%d) — docs/deployment.md «Проверка на стенде»\n' "$DAEMON" "$fails"
    exit 1
fi
note "доступ $DAEMON подтверждён: $probes проверок от имени $DAEMON и $WEB_USER"
exit 0
