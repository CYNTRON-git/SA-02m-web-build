#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Mutation coverage: cased in .ai-dev/quality/checks/comment-mutation-proof.sh (no exemption — the
# three cases each turn this gate RED without root: the boot tmpfiles `Z` line (section G), the
# runner's `web_root_prepare` call (F2a), the block's unlink in the scripts/lib.sh copy (A + B1).
# The root-only sections C/D/E/F1 cannot be cased: the proof runs non-root in CI, where they SKIP.
# test-web-root-own.sh — the served web tree is root-owned and no root writer
# follows a link planted in it. Quality row `web-root-own`.
#
# Why (Operator decision 2026-09-28, «Каталог — root, запись только где
# нужно»): /var/www/network_config was `chown -R www-data`, and root wrote into
# it BY NAME — `cp -a` (update-www-only.sh, the legacy OTA fallback) writes
# THROUGH a destination symlink and copies the source mode onto its target;
# the installer's wipe→copy window, `find -exec chmod`, the runner's GNU
# `install` (fchmodat of a predictable tmp by name) and its rollback replay
# (copy2 + os.chown of a predictable `<dst>.rb.<pid>`, and copy2 FOLLOWING a
# symlinked backup) all turn one planted link into a root write/chmod/read of
# any file. Same runner: txn_patch opened a predictable
# `transaction.json.tmp.<pid>` by name in the 0775 root:www-data state dir.
# Nothing the web does at runtime writes under the web root, so the fix is the
# whole tree root:root (the sa02m-web-root-own shared block) plus fd-safe root
# writers as defence in depth.
#
# Sections (root-only ones need a real uid switch to model www-data — the
# attacker is uid 65534 via setpriv; a non-root host reports them as SKIP):
#   A  the block is byte-identical in its three homes (scripts/lib.sh,
#      etc/sa02m-web-update-apply.sh, etc/sa02m-update-runner.sh), non-vacuous.
#   B  the block itself (non-root: own uid as the target owner; root: 0:0 over
#      a tree owned by 65534): links / fifos / multi-link files removed, victim
#      untouched, modes normalised, idempotent, symlinked ROOT refused, absent
#      ROOT rc 0, parent g/o-write stripped, sticky parent refused.
#   C  installer block (scripts/03-webserver.sh «Деплой файлов», extracted and
#      retargeted): tree ends root-owned, nothing g/o-writable, no link; and an
#      attacker winning the wipe→copy race (planting static/js/app.js -> victim
#      as 65534 right after the wipe) cannot — ROOT.
#   D  update-www-only block: a pre-planted static/js/app.js -> victim link
#      (the shipped name) is removed before `cp -a`, victim untouched — ROOT.
#   E  legacy OTA block (etc/sa02m-web-update-apply.sh): root-owned result and
#      the copy-race plant refused — ROOT.
#   F  runner: F1 a manifest owner www-data:www-data under WEB_ROOT lands
#      root:root (a non-web dst keeps its owner) — ROOT; F2 web_root_prepare
#      runs before stop_before_apply in cmd_apply and migrates; F3 a tmp swapped
#      for a link between mktemp and the write is refused (new path); F4 strace:
#      no chmod/chown syscall names the tmp (GNU install's fchmodat-by-name is
#      the RED); F5 rollback replay: a symlinked backup is restored as the link,
#      never read through, and a spray of `<dst>.rb.<pid>` links is never
#      followed; F6 txn_patch: a spray of `transaction.json.tmp.<pid>` links is
#      never followed and a symlinked transaction.json is refused, not read.
#   G  static sweep of every root writer into the web root (all files naming
#      WEB_ROOT or /var/www/network_config): no www-data ownership handed to
#      the tree; the deploy map's web rule and the github builder say
#      root:root; the boot tmpfiles line is present.
#
# Drive-to-failure: WEB_ROOT_OWN_SRC_ROOT=<pristine tree of main> — e.g.
#   d=$(mktemp -d); git archive main | tar -x -C "$d"
#   WEB_ROOT_OWN_SRC_ROOT="$d" bash scripts/dev/test-web-root-own.sh
# Every function is extracted if present, so the pre-fix run reaches the
# assertions instead of aborting. Run with the harness canary dir first in
# PATH when on a shared host (the extracted blocks call cp/find/chmod/chown/
# python3 only, all inside $T; every ROOT handed to the block is checked to lie
# under $T first).
#
# Run: bash scripts/dev/test-web-root-own.sh   (bash, python3, coreutils;
#   strace for F4 — SKIP without it; setpriv + root for C/D/E/F1/B-root).
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

SRC_ROOT="${WEB_ROOT_OWN_SRC_ROOT:-.}"
command -v python3 >/dev/null 2>&1 || { echo "SKIP  python3 unavailable"; exit 0; }
T=$(mktemp -d) || exit 1
# The attacker (uid 65534) must be able to TRAVERSE to the web root, or a
# pre-fix run would refuse its plant for the wrong reason (a 0700 $T).
chmod 0755 "$T"
cleanup() { rm -rf "$T"; }
trap cleanup EXIT

fails=0; skips=0
ok()   { printf 'ok    %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; skips=$((skips + 1)); }

IS_ROOT=0; [ "$(id -u)" = 0 ] && IS_ROOT=1
CAN_SWITCH=0
if [ "$IS_ROOT" = 1 ] && command -v setpriv >/dev/null 2>&1 \
   && setpriv --reuid=65534 --regid=65534 --clear-groups true 2>/dev/null; then
    CAN_SWITCH=1
fi
as_attacker() { setpriv --reuid=65534 --regid=65534 --clear-groups "$@"; }

for f in scripts/lib.sh etc/sa02m-web-update-apply.sh etc/sa02m-update-runner.sh \
         scripts/03-webserver.sh scripts/update-www-only.sh; do
    cp "$SRC_ROOT/$f" "$T/$(basename "$f").src" 2>/dev/null \
        || { echo "FAIL  cannot read $SRC_ROOT/$f"; exit 1; }
done

# ── A. byte-identical block in three homes ──────────────────────────────────
block_of() {
    awk '/^# ── BEGIN sa02m-web-root-own /{f=1} f{print} f&&/^# ── END sa02m-web-root-own /{exit}' "$1"
}
HAS_BLOCK=1
for h in lib.sh sa02m-web-update-apply.sh sa02m-update-runner.sh; do
    block_of "$T/$h.src" > "$T/block.$h"
    if ! grep -q '^sa02m_web_root_own() {' "$T/block.$h" || ! grep -q '^def walk(dfd, path):' "$T/block.$h" \
       || ! grep -q '^# ── END sa02m-web-root-own ' "$T/block.$h"; then
        bad "A $h carries no complete sa02m-web-root-own block"
        HAS_BLOCK=0
    fi
done
if [ "$HAS_BLOCK" = 1 ]; then
    if cmp -s "$T/block.lib.sh" "$T/block.sa02m-web-update-apply.sh" \
       && cmp -s "$T/block.lib.sh" "$T/block.sa02m-update-runner.sh"; then
        ok "A the sa02m-web-root-own block is byte-identical in its three homes ($(wc -l < "$T/block.lib.sh") lines)"
    else
        bad "A the three sa02m-web-root-own copies differ — keep them BYTE-IDENTICAL"
    fi
fi

# Every ROOT handed to the block must lie under $T — the harness never lets a
# shipped routine walk a path it did not create.
own() {
    case "$1" in "$T"/*) ;; *) echo "harness refused ROOT outside \$T: $1" >&2; return 99 ;; esac
    ( . "$T/block.lib.sh"; sa02m_web_root_own "$@" )
}
tree_violations() {   # ROOT UID GID → prints offending entries (empty = clean)
    python3 - "$@" <<'PY'
import os, stat, sys
root, uid, gid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
bad = []
for dp, dns, fns in os.walk(root, followlinks=False):
    for n in [None] + dns + fns:
        p = dp if n is None else os.path.join(dp, n)
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            bad.append("link " + p)
        elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            bad.append("special " + p)
        else:
            if (st.st_uid, st.st_gid) != (uid, gid):
                bad.append("owner %d:%d %s" % (st.st_uid, st.st_gid, p))
            if st.st_mode & 0o7022:
                bad.append("mode %o %s" % (stat.S_IMODE(st.st_mode), p))
            if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
                bad.append("links %d %s" % (st.st_nlink, p))
print("\n".join(bad))
PY
}
victim_state() {   # one line: owner mode size bytes-hash
    printf '%s %s' "$(stat -c '%u:%g %a %s' "$1" 2>/dev/null)" "$(sha256sum < "$1" 2>/dev/null | cut -c1-16)"
}

# ── B. the block itself ─────────────────────────────────────────────────────
if [ "$HAS_BLOCK" = 1 ]; then
    if [ "$IS_ROOT" = 1 ]; then OU=0; OG=0; ARGS=(); else OU=$(id -u); OG=$(id -g); ARGS=("$OU" "$OG"); fi
    B="$T/b"; mkdir -p "$B/www/nc/static/js" "$B/www/nc/cgi-bin"
    printf 'SECRET\n' > "$B/victim"; chmod 0600 "$B/victim"
    printf 'a\n' > "$B/www/nc/static/js/a.js"; chmod 0666 "$B/www/nc/static/js/a.js"
    printf 'c\n' > "$B/www/nc/cgi-bin/x.cgi"; chmod 4775 "$B/www/nc/cgi-bin/x.cgi"
    ln -s "$B/victim" "$B/www/nc/static/js/evil.js"
    ln "$B/victim" "$B/www/nc/hard"
    mkfifo "$B/www/nc/fifo"
    chmod 0777 "$B/www/nc/static"; chmod 0775 "$B/www"
    [ "$IS_ROOT" = 1 ] && chown -h 65534:65534 "$B/www" "$B/www/nc" "$B/www/nc/static" \
        "$B/www/nc/static/js" "$B/www/nc/static/js/a.js" "$B/www/nc/cgi-bin" "$B/www/nc/cgi-bin/x.cgi" \
        "$B/www/nc/static/js/evil.js" "$B/www/nc/fifo"
    vb=$(victim_state "$B/victim")
    out=$(own "$B/www/nc" "${ARGS[@]}" 2>&1); rc=$?
    viol=$(tree_violations "$B/www/nc" "$OU" "$OG")
    [ "$rc" -eq 0 ] && [ -z "$viol" ] \
        && ok "B1 links / fifo / multi-link file removed, tree $OU:$OG with 0755/0644 (rc 0)" \
        || bad "B1 rc=$rc, left: $(printf '%s' "$viol" | tr '\n' ';') | $out"
    [ "$(victim_state "$B/victim")" = "$vb" ] \
        && ok "B2 the victim behind the link and the hard link is untouched (owner, mode, bytes)" \
        || bad "B2 victim changed: $vb -> $(victim_state "$B/victim")"
    m1=$(stat -c '%a' "$B/www/nc/static/js/a.js"); m2=$(stat -c '%a' "$B/www/nc/cgi-bin/x.cgi")
    [ "$m1" = 644 ] && [ "$m2" = 755 ] \
        && ok "B3 modes normalised: 0666 -> 644, setuid 4775 -> 755" \
        || bad "B3 modes a.js=$m1 x.cgi=$m2 (want 644 / 755)"
    pm=$(stat -c '%a %u' "$B/www")
    [ "$pm" = "755 $OU" ] && ok "B4 parent lost group/other write and its non-root owner ($pm)" \
                          || bad "B4 parent is '$pm' (want '755 $OU')"
    out=$(own "$B/www/nc" "${ARGS[@]}" 2>&1); rc=$?
    case "$out" in *"0 entries settled, 0 removed, 0 failed"*) idem=1 ;; *) idem=0 ;; esac
    [ "$rc" -eq 0 ] && [ "$idem" = 1 ] && ok "B5 a second run changes nothing (idempotent)" \
                                       || bad "B5 second run rc=$rc: $out"
    ln -s "$B/www/nc" "$B/www/link"
    [ "$IS_ROOT" = 1 ] && chown -h 65534:65534 "$B/www/link"
    out=$(own "$B/www/link" "${ARGS[@]}" 2>&1); rc=$?
    [ "$rc" -ne 0 ] && [ -L "$B/www/link" ] && ok "B6 a symlinked ROOT is refused (rc=$rc), never walked" \
                                          || bad "B6 symlinked ROOT rc=$rc: $out"
    out=$(own "$B/nothere/nc" "${ARGS[@]}" 2>&1); rc=$?
    [ "$rc" -eq 0 ] && ok "B7 an absent ROOT is rc 0 (nothing to do)" || bad "B7 absent ROOT rc=$rc: $out"
    mkdir -p "$B/sticky/nc"; chmod 1777 "$B/sticky"
    out=$(own "$B/sticky/nc" "${ARGS[@]}" 2>&1); rc=$?
    [ "$rc" -ne 0 ] && [ "$(stat -c '%a' "$B/sticky")" = 1777 ] \
        && ok "B8 a sticky shared parent is refused and never re-moded" \
        || bad "B8 sticky parent rc=$rc mode=$(stat -c '%a' "$B/sticky"): $out"
    if [ "$IS_ROOT" = 0 ]; then skip "B root-owner half (65534 -> 0:0) needs root; ran with the own uid as the target"; fi
else
    bad "B skipped — no block to test (see A)"
fi

# ── fake served tree used by C/D/E ──────────────────────────────────────────
SRCW="$T/src/www/network_config"
mkdir -p "$SRCW/cgi-bin" "$SRCW/static/js" "$SRCW/static/css"
printf '<html>new</html>\n' > "$SRCW/index.html"; printf 'login\n' > "$SRCW/login.html"
printf '#!/bin/bash\necho x\n' > "$SRCW/cgi-bin/a.cgi"; chmod 755 "$SRCW/cgi-bin/a.cgi"
printf "const APP_VERSION = '9.9.9.9';\n" > "$SRCW/static/js/app.js"
printf 'body{}\n' > "$SRCW/static/css/main.css"
printf '9.9.9.9\n' > "$SRCW/VERSION"
chmod -R go-w "$T/src"

# old_tree ROOT — a pre-1.0.6.55 web root: owned by the web user (65534 here)
old_tree() {
    mkdir -p "$1/static/js" "$1/cgi-bin"
    printf 'old\n' > "$1/index.html"; printf 'old\n' > "$1/static/js/app.js"
    chmod 0775 "$1" "$1/static" "$1/static/js"
    chown -R 65534:65534 "$1"
}

# extract_block SRC START_RE END_AFTER_RE END_RE — START line through the first
# END line that follows a line matching END_AFTER_RE.
extract_block() {
    awk -v s="$2" -v a="$3" -v e="$4" '
        index($0, s) == 1 { f = 1 }
        f { print }
        f && $0 ~ a { p = 1 }
        f && p && $0 ~ e { exit }' "$1"
}
retarget() {   # FILE FROM TO — replace literally, fail the section when absent
    python3 - "$@" <<'PY'
import sys
p, a, b = sys.argv[1:4]
s = open(p).read()
if a not in s:
    sys.exit(1)
open(p, "w").write(s.replace(a, b))
PY
}

# ── C. installer block ──────────────────────────────────────────────────────
# (awk -v processes backslash escapes, so the regexes use [$] / [|], never \$ / \|)
C_END='^(chown -R www-data:www-data "[$]WEB_ROOT"|sa02m_web_root_secure "[$]WEB_ROOT" [|][|] exit 1)$'
extract_block "$T/03-webserver.sh.src" 'log INFO "Деплой файлов в $WEB_ROOT"' '^# Permissions' "$C_END" > "$T/c.sh"
if ! grep -q '^cp -r "\$WWW_DIR/\." "\$WEB_ROOT/"$' "$T/c.sh" || ! tail -n1 "$T/c.sh" | grep -Eq "$C_END"; then
    bad "C could not extract the installer's web deploy block — the anchors moved; fix this harness"
elif ! retarget "$T/c.sh" 'STATEDIR=/var/lib/sa02m-web-build' "STATEDIR=$T/c-state"; then
    bad "C retarget of STATEDIR failed — the block changed; fix this harness"
elif grep -q '/var/\|/usr/local' "$T/c.sh"; then
    bad "C extracted block still names a host path: $(grep -n '/var/\|/usr/local' "$T/c.sh" | head -3 | tr '\n' ' ')"
elif [ "$CAN_SWITCH" = 0 ]; then
    skip "C installer block needs root + setpriv (models www-data as uid 65534)"
else
    run_c() {   # ROOT [race] → rc
        (
            set +e
            WEB_ROOT=$1; WWW_DIR=$SRCW; SCRIPT_DIR="$T/c-scripts"; LOG_FILE=/dev/null
            mkdir -p "$SCRIPT_DIR"
            log() { :; }
            [ "$HAS_BLOCK" = 1 ] && . "$T/block.lib.sh"
            # sa02m_web_root_secure is lib.sh's own caller of the block
            eval "$(awk '/^sa02m_web_root_secure\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$T/lib.sh.src")"
            if [ "${2:-}" = race ]; then
                # The attacker wins the wipe→copy window: right after the wipe,
                # as the web user, it plants static/js/app.js -> victim.
                mkdir() {
                    if [ "${*: -1}" = "$WEB_ROOT/static/js" ] && [ ! -e "$T/c-raced" ]; then
                        : > "$T/c-raced"
                        as_attacker sh -c 'mkdir -p "$1/static/js" && ln -s "$2" "$1/static/js/app.js"' _ "$WEB_ROOT" "$T/c-victim" 2>/dev/null \
                            && : > "$T/c-planted"
                    fi
                    command mkdir "$@"
                }
            fi
            . "$T/c.sh"
        )
    }
    old_tree "$T/c1/www/network_config"; ln -s "$T/c-victim" "$T/c1/www/network_config/static/js/evil.js"
    printf 'SECRET\n' > "$T/c-victim"; chmod 0600 "$T/c-victim"; vc=$(victim_state "$T/c-victim")
    run_c "$T/c1/www/network_config" >/dev/null 2>&1; rc=$?
    viol=$(tree_violations "$T/c1/www/network_config" 0 0)
    if [ "$rc" -eq 0 ] && [ -z "$viol" ] && cmp -s "$SRCW/static/js/app.js" "$T/c1/www/network_config/static/js/app.js"; then
        ok "C1 installer: the tree ends root:root, 0755/0644, no link, files deployed"
    else
        bad "C1 installer rc=$rc, left: $(printf '%s' "$viol" | head -4 | tr '\n' ';')"
    fi
    old_tree "$T/c2/www/network_config"
    run_c "$T/c2/www/network_config" race >/dev/null 2>&1; rc=$?
    if [ ! -e "$T/c-raced" ]; then
        bad "C2 the race hook never fired — the block no longer creates static/js; fix this harness"
    elif [ -e "$T/c-planted" ] || [ "$(victim_state "$T/c-victim")" != "$vc" ]; then
        bad "C2 installer: the web user planted a link after the wipe (planted=$([ -e "$T/c-planted" ] && echo yes || echo no)), victim $vc -> $(victim_state "$T/c-victim")"
    else
        ok "C2 installer: the web user cannot plant into the tree between wipe and copy (EACCES), victim untouched"
    fi
fi

# ── D. update-www-only block ────────────────────────────────────────────────
extract_block "$T/update-www-only.sh.src" 'log INFO "Копирование $WWW_DIR → $WEB_ROOT"' '^find "[$]WEB_ROOT/cgi-bin"' "$C_END" > "$T/d.sh"
if ! grep -q '^cp -a "\$WWW_DIR/\." "\$WEB_ROOT/"$' "$T/d.sh" || ! tail -n1 "$T/d.sh" | grep -Eq "$C_END"; then
    bad "D could not extract the update-www-only copy block — the anchors moved; fix this harness"
elif ! retarget "$T/d.sh" 'STATEDIR=/var/lib/sa02m-web-build' "STATEDIR=$T/d-state" \
     || ! retarget "$T/d.sh" '/usr/local/lib/sa02m-web-build-lib.sh' "$T/d-no-lib.sh"; then
    bad "D retarget failed — the block changed; fix this harness"
elif grep -q '/var/\|/usr/local' "$T/d.sh"; then
    bad "D extracted block still names a host path: $(grep -n '/var/\|/usr/local' "$T/d.sh" | head -3 | tr '\n' ' ')"
elif [ "$CAN_SWITCH" = 0 ]; then
    skip "D update-www-only block needs root (a pre-1.0.6.55 tree owned by the web user)"
else
    D="$T/d/www/network_config"; old_tree "$D"
    printf 'SECRET\n' > "$T/d-victim"; chmod 0600 "$T/d-victim"; vd=$(victim_state "$T/d-victim")
    rm -f "$D/static/js/app.js"
    as_attacker ln -s "$T/d-victim" "$D/static/js/app.js"
    (
        set +e
        WEB_ROOT=$D; WWW_DIR=$SRCW; SCRIPT_DIR="$T/d-scripts"; LOG_FILE=/dev/null
        mkdir -p "$SCRIPT_DIR"
        log() { :; }
        [ "$HAS_BLOCK" = 1 ] && . "$T/block.lib.sh"
        eval "$(awk '/^sa02m_web_root_secure\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$T/lib.sh.src")"
        . "$T/d.sh"
    ) >/dev/null 2>&1; rc=$?
    if [ "$(victim_state "$T/d-victim")" = "$vd" ]; then
        ok "D1 update-www-only: a planted static/js/app.js -> victim is not written through by cp -a (victim untouched)"
    else
        bad "D1 update-www-only: cp -a / chmod went THROUGH the planted link — victim $vd -> $(victim_state "$T/d-victim")"
    fi
    viol=$(tree_violations "$D" 0 0)
    [ "$rc" -eq 0 ] && [ -z "$viol" ] && cmp -s "$SRCW/static/js/app.js" "$D/static/js/app.js" \
        && ok "D2 update-www-only: tree root:root, no link, the real app.js deployed" \
        || bad "D2 update-www-only rc=$rc, left: $(printf '%s' "$viol" | head -4 | tr '\n' ';')"
fi

# ── E. legacy OTA block ─────────────────────────────────────────────────────
E_END='^(chown -R www-data:www-data "[$]WEB_ROOT" 2>/dev/null [|][|] true|web_root_own_logged [|][|] .*)$'
extract_block "$T/sa02m-web-update-apply.sh.src" 'log "Деплой веб-файлов в $WEB_ROOT..."' '^# Права доступа' "$E_END" > "$T/e.sh"
if ! grep -q 'cp -a "\$TMPDIR/repo/www/network_config/\." "\$WEB_ROOT/"' "$T/e.sh" || ! tail -n1 "$T/e.sh" | grep -Eq "$E_END"; then
    bad "E could not extract the legacy deploy block — the anchors moved; fix this harness"
elif grep -q '/var/\|/usr/local' "$T/e.sh"; then
    bad "E extracted block names a host path: $(grep -n '/var/\|/usr/local' "$T/e.sh" | head -3 | tr '\n' ' ')"
elif [ "$CAN_SWITCH" = 0 ]; then
    skip "E legacy OTA block needs root + setpriv"
else
    E="$T/e/www/network_config"; old_tree "$E"
    printf 'SECRET\n' > "$T/e-victim"; chmod 0600 "$T/e-victim"; ve=$(victim_state "$T/e-victim")
    mkdir -p "$T/e-tmp/repo/www"; cp -a "$T/src/www/network_config" "$T/e-tmp/repo/www/"
    (
        set +e
        WEB_ROOT=$E; TMPDIR="$T/e-tmp"; LOGFILE="$T/e.log"; STATUS_FILE="$T/e.status"
        log() { printf '%s\n' "$*" >> "$LOGFILE"; }
        [ "$HAS_BLOCK" = 1 ] && . "$T/block.lib.sh"
        eval "$(awk '/^web_root_own_logged\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$T/sa02m-web-update-apply.sh.src")"
        # The attacker wins the window between the (pre-)migration and the copy.
        plant() {
            if [ ! -e "$T/e-raced" ]; then
                : > "$T/e-raced"
                as_attacker sh -c 'mkdir -p "$1/static/js" && rm -f "$1/static/js/app.js" && ln -s "$2" "$1/static/js/app.js"' _ "$WEB_ROOT" "$T/e-victim" 2>/dev/null \
                    && : > "$T/e-planted"
            fi
        }
        # Hook whichever copy the block takes; an rsync function is defined only
        # when the real one exists (`command -v rsync` also finds functions).
        if command -v rsync >/dev/null 2>&1; then rsync() { plant; command rsync "$@"; }; fi
        cp() { plant; command cp "$@"; }
        . "$T/e.sh"
    ) >/dev/null 2>&1; rc=$?
    if [ ! -e "$T/e-raced" ]; then
        bad "E1 the copy hook never fired — fix this harness"
    elif [ -e "$T/e-planted" ] || [ "$(victim_state "$T/e-victim")" != "$ve" ]; then
        bad "E1 legacy OTA: the web user planted before the copy (planted=$([ -e "$T/e-planted" ] && echo yes || echo no)), victim $ve -> $(victim_state "$T/e-victim")"
    else
        ok "E1 legacy OTA: the tree is root-owned before the copy — the web user's plant is refused, victim untouched"
    fi
    viol=$(tree_violations "$E" 0 0)
    [ "$rc" -eq 0 ] && [ -z "$viol" ] && ok "E2 legacy OTA: tree ends root:root, no link" \
        || bad "E2 legacy OTA rc=$rc, left: $(printf '%s' "$viol" | head -4 | tr '\n' ';')"
fi

# ── F. runner ───────────────────────────────────────────────────────────────
RSRC="$T/sa02m-update-runner.sh.src"
rx() { awk -v start="$1() {" 'index($0,start)==1{f=1} f{print} f&&/^\}/{exit}' "$RSRC"; }
: > "$T/f.sh"
for fn in atomic_install_file journal_append is_unchanged apply_deploy_items rollback_from_journal txn_patch web_root_prepare die cmd_apply; do
    if grep -q "^$fn() {" "$RSRC"; then rx "$fn" >> "$T/f.sh"; fi
done
for fn in atomic_install_file journal_append apply_deploy_items rollback_from_journal txn_patch; do
    grep -q "^$fn() {" "$T/f.sh" || { echo "FAIL  could not extract $fn() from the runner — the marker moved; fix this harness"; exit 1; }
done
[ "$HAS_BLOCK" = 1 ] && cat "$T/block.sa02m-update-runner.sh" >> "$T/f.sh"
fenv() {   # common stubs for the extracted runner functions
    STATEDIR="$T/fstate"; LOGF="$T/f.log"; mkdir -p "$STATEDIR"
    log() { printf '%s\n' "$*" >> "$LOGF"; }
    manifest_path() { printf '%s\n' "$STATEDIR/staging/$1/meta/manifest.json"; }
    cleanup_imaging_lock() { :; }
    restart_after_rollback() { :; }
    utc_now() { echo 1970-01-01T00:00:00Z; }
    export SA02M_UPDATE_PROGRESS_S=0
}

# F1 owner forcing under WEB_ROOT
if [ "$IS_ROOT" = 0 ]; then
    skip "F1 owner forcing needs root (a non-root chown is best-effort by design)"
else
    (
        set +e
        . "$T/f.sh"; fenv; txn_patch() { :; }
        WEB_ROOT="$T/f1/www/network_config"
        TX=F1; ov="$STATEDIR/staging/$TX/overlay"; mkdir -p "$ov" "$STATEDIR/staging/$TX/meta" "$T/f1/other"
        printf 'web\n' > "$ov/index.html"; printf 'conf\n' > "$ov/x.conf"
        printf '{"deploy": [{"src": "index.html", "dst": "%s/index.html", "mode": "0644", "owner": "www-data:www-data"}, {"src": "x.conf", "dst": "%s/x.conf", "mode": "0644", "owner": "65534:65534"}]}\n' \
            "$WEB_ROOT" "$T/f1/other" > "$(manifest_path "$TX")"
        if apply_deploy_items "$TX"; then :; else echo "apply rc=$?" >> "$LOGF"; fi
    ) >/dev/null 2>&1
    o1=$(stat -c '%u:%g %a' "$T/f1/www/network_config/index.html" 2>/dev/null); o2=$(stat -c '%u:%g' "$T/f1/other/x.conf" 2>/dev/null)
    [ "$o1" = "0:0 644" ] && [ "$o2" = "65534:65534" ] \
        && ok "F1 a manifest owner www-data:www-data under WEB_ROOT lands root:root 0644; a non-web dst keeps its owner" \
        || bad "F1 web file '$o1' (want 0:0 644), non-web owner '$o2' (want 65534:65534)"
fi

# F2 web_root_prepare before stop_before_apply, and it migrates
if ! grep -q '^web_root_prepare() {' "$T/f.sh"; then
    bad "F2 the runner has no web_root_prepare — nothing re-owns the web root before an OTA deploy"
else
    order=$(awk '/^cmd_apply\(\) \{/{f=1} f&&/^[[:space:]]*web_root_prepare[[:space:]]*$/{print "P" NR} f&&/^[[:space:]]*stop_before_apply "\$txn"/{print "S" NR} f&&/^\}/{exit}' "$RSRC" | tr '\n' ' ')
    case "$order" in "P"*" S"*) ok "F2a cmd_apply runs web_root_prepare before stop_before_apply ($order)" ;;
                     *) bad "F2a cmd_apply order is '$order' (want web_root_prepare before stop_before_apply)" ;; esac
    F2R="$T/f2/www/network_config"; mkdir -p "$F2R/static"; ln -s "$T/f2-victim" "$F2R/static/l.js"; printf 'x\n' > "$T/f2-victim"
    # Root: the shipped web_root_prepare itself. Non-root: it passes 0:0, which
    # only root can set — drive the same runner-copy routine with the own uid.
    out=$( set +e; . "$T/f.sh"; fenv; txn_patch() { :; }; WEB_ROOT=$F2R
           if [ "$IS_ROOT" = 1 ]; then web_root_prepare
           else sa02m_web_root_own "$WEB_ROOT" "$(id -u)" "$(id -g)"; fi 2>&1; echo "rc=$?" )
    if [ ! -L "$F2R/static/l.js" ] && [ -e "$T/f2-victim" ]; then
        ok "F2b the prepare step removes a planted link from the web root before the deploy"
    else
        bad "F2b planted link still present after web_root_prepare: $out"
    fi
fi

# F3 a tmp swapped for a link between mktemp and the write is refused (new path)
F3="$T/f3"; mkdir -p "$F3/live"; printf 'SECRET\n' > "$F3/victim"; chmod 0600 "$F3/victim"; v3=$(victim_state "$F3/victim")
printf 'new\n' > "$F3/src"; printf 'old\n' > "$F3/live/app.js"
( set +e; . "$T/f.sh"; fenv
  mktemp() { local t; t=$(command mktemp "$@") || return 1; rm -f "$t"; ln -s "$F3/victim" "$t"; printf '%s\n' "$t"; }
  atomic_install_file "$F3/src" "$F3/live/app.js" 0644 ""; echo "rc=$?" > "$F3/rc" ) >/dev/null 2>&1
if grep -q '^rc=0$' "$F3/rc" 2>/dev/null || [ "$(victim_state "$F3/victim")" != "$v3" ] || [ "$(cat "$F3/live/app.js")" != old ]; then
    bad "F3 a swapped tmp was written/renamed: $(cat "$F3/rc" 2>/dev/null), victim $v3 -> $(victim_state "$F3/victim"), app.js='$(cat "$F3/live/app.js")'"
else
    ok "F3 a tmp replaced by a link before the write is refused (rc 1), victim and dst untouched"
fi

# F4 strace: no chmod/chown syscall names the tmp
if ! command -v strace >/dev/null 2>&1 || ! strace -f -qq -o /dev/null true 2>/dev/null; then
    skip "F4 strace unavailable or cannot trace here"
else
    F4="$T/f4"; mkdir -p "$F4/live"; printf 'new\n' > "$F4/src"
    strace -f -qq -e trace=%file -o "$F4/trace" bash -c '. "$1"; log() { :; }; atomic_install_file "$2/src" "$2/live/app.js" 0644 "$3"' _ \
        "$T/f.sh" "$F4" "$(id -un):$(id -gn)" >/dev/null 2>&1
    meta=$(grep -E '(^|[^a-z])(f?chmod(at2?)?|f?chown(at)?|lchown)\(' "$F4/trace" 2>/dev/null)
    byname=$(printf '%s\n' "$meta" | grep -F 'app.js.tmp.')
    if [ -z "$meta" ]; then
        bad "F4 strace saw no chmod/chown syscall at all — the trace is vacuous"
    elif [ -n "$byname" ]; then
        bad "F4 chmod/chown BY NAME on the tmp: $(printf '%s' "$byname" | head -2 | tr '\n' ' ')"
    else
        ok "F4 no chmod/chown syscall names the tmp ($(printf '%s\n' "$meta" | wc -l) metadata syscall(s), all through /proc/self/fd)"
    fi
fi

# The pid a process started NOW gets is just past the last one handed out:
# /proc/sys/kernel/ns_last_pid when the kernel has it, else a throwaway child's.
next_pid_hint() {
    local p=""
    p=$(cat /proc/sys/kernel/ns_last_pid 2>/dev/null) || p=""
    case "$p" in ''|*[!0-9]*) p=$(sh -c 'echo $$') ;; esac
    case "$p" in ''|*[!0-9]*) p=0 ;; esac
    printf '%s\n' "$p"
}
# spray PREFIX TARGET — links PREFIX<n> -> TARGET for the next 3000 pids, made
# by ONE process: an `ln` per name would burn through the very pids it sprays.
spray() {
    python3 - "$1" "$2" "$(next_pid_hint)" <<'PY'
import os, sys
prefix, target, last = sys.argv[1], sys.argv[2], int(sys.argv[3])
for n in range(last + 1, last + 3001):
    try:
        os.symlink(target, prefix + str(n))
    except FileExistsError:
        pass
PY
}
# F5 rollback replay
F5="$T/f5"; mkdir -p "$F5/live" "$F5/state/staging/TX/backups"
printf 'SECRET-F5\n' > "$F5/secret"; chmod 0600 "$F5/secret"
printf 'VICTIM\n' > "$F5/victim"; chmod 0600 "$F5/victim"; v5=$(victim_state "$F5/victim")
ln -s "$F5/secret" "$F5/state/staging/TX/backups/bl"          # cp -a of a planted dst link
printf 'OLD\n' > "$F5/state/staging/TX/backups/bf"; chmod 0644 "$F5/state/staging/TX/backups/bf"
printf 'NEW\n' > "$F5/live/a.js"; printf 'NEW\n' > "$F5/live/b.js"
printf '{"op": "replace", "dst": "%s", "backup": "%s"}\n{"op": "replace", "dst": "%s", "backup": "%s"}\n' \
    "$F5/live/a.js" "$F5/state/staging/TX/backups/bl" "$F5/live/b.js" "$F5/state/staging/TX/backups/bf" \
    > "$F5/state/staging/TX/journal.jsonl"
last=$(next_pid_hint)
[ "$last" -gt 0 ] && spray "$F5/live/b.js.rb." "$F5/victim"
( set +e; . "$T/f.sh"; fenv; STATEDIR="$F5/state"; txn_patch() { :; }; txn_get() { :; }
  rollback_from_journal TX ) >/dev/null 2>&1
if grep -q 'SECRET-F5' "$F5/live/a.js" 2>/dev/null && [ ! -L "$F5/live/a.js" ]; then
    bad "F5a the replay READ THROUGH a symlinked backup: the secret's bytes landed in the served file"
elif [ -L "$F5/live/a.js" ] || [ "$(cat "$F5/live/a.js" 2>/dev/null)" = NEW ]; then
    ok "F5a a symlinked backup is restored as the link (or refused), never read through"
else
    bad "F5a unexpected a.js state: $(ls -l "$F5/live/a.js" 2>&1)"
fi
if [ "$last" -eq 0 ]; then
    skip "F5b no pid hint — spray not run"
elif [ "$(victim_state "$F5/victim")" != "$v5" ]; then
    bad "F5b a predictable <dst>.rb.<pid> link was followed — victim $v5 -> $(victim_state "$F5/victim")"
elif [ "$(cat "$F5/live/b.js" 2>/dev/null)" != OLD ] || [ -L "$F5/live/b.js" ]; then
    bad "F5b b.js not restored from its regular backup: $(ls -l "$F5/live/b.js" 2>&1)"
else
    ok "F5b 3000 planted <dst>.rb.<pid> links never followed; the regular backup restored (victim untouched)"
fi

# F6 txn_patch
F6="$T/f6"; mkdir -p "$F6"
printf 'VICTIM\n' > "$F6/victim"; chmod 0600 "$F6/victim"; v6=$(victim_state "$F6/victim")
printf '{"stage": "applying"}\n' > "$F6/transaction.json"
last=$(next_pid_hint)
[ "$last" -gt 0 ] && spray "$F6/transaction.json.tmp." "$F6/victim"
( set +e; . "$T/f.sh"; fenv; TXN_FILE="$F6/transaction.json"; txn_patch "progress_pct=50" ) >/dev/null 2>&1; rc=$?
if [ "$last" -eq 0 ]; then
    skip "F6a no pid hint — spray not run"
elif [ "$(victim_state "$F6/victim")" != "$v6" ]; then
    bad "F6a a predictable transaction.json.tmp.<pid> link was followed — victim $v6 -> $(victim_state "$F6/victim")"
elif ! grep -q '"progress_pct": 50' "$F6/transaction.json" || [ -L "$F6/transaction.json" ]; then
    bad "F6a the patch did not land in a regular transaction.json (rc=$rc)"
else
    ok "F6a 3000 planted transaction.json.tmp.<pid> links never followed; the patch landed (mode $(stat -c '%a' "$F6/transaction.json"))"
fi
F6B="$T/f6b"; mkdir -p "$F6B"
printf '{"stage": "applying", "secret": "S3CR3T-F6"}\n' > "$F6B/root-only.json"; chmod 0600 "$F6B/root-only.json"
ln -s "$F6B/root-only.json" "$F6B/transaction.json"
( set +e; . "$T/f.sh"; fenv; TXN_FILE="$F6B/transaction.json"; txn_patch "progress_pct=50" ) >/dev/null 2>&1; rc=$?
leak=$(find "$F6B" -type f ! -name root-only.json -exec grep -l 'S3CR3T-F6' {} + 2>/dev/null)
if [ -n "$leak" ] || [ "$rc" -eq 0 ]; then
    bad "F6b a symlinked transaction.json was read through (rc=$rc, secret copied into: ${leak:-none})"
else
    ok "F6b a symlinked transaction.json is refused (rc=$rc), its target never copied out"
fi

# ── G. static sweep of the root writers ─────────────────────────────────────
python3 - "$SRC_ROOT" <<'PY' > "$T/g.out"
import os, re, sys, json
root = sys.argv[1]
fails = []
homes = []
for base in ("scripts", "etc", "install.sh"):
    p = os.path.join(root, base)
    files = [p] if os.path.isfile(p) else [os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs]
    for f in files:
        rel = os.path.relpath(f, root)
        if rel.startswith("scripts/dev/") or not (f.endswith(".sh") or rel == "install.sh"):
            continue
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "WEB_ROOT" not in text and "/var/www/network_config" not in text:
            continue
        homes.append(rel)
        joined = re.sub(r"\\\n\s*", " ", text)
        for n, line in enumerate(joined.splitlines(), 1):
            code = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
            if not code:
                continue
            web = re.search(r"WEB_ROOT|WEB_CGI|WEB_JS|WEB_STATIC|CGI_DST|/var/www", code)
            if web and re.search(r"\bchown\b[^;|&]*www-data", code):
                fails.append("%s: chown to www-data on the web tree: %s" % (rel, code.strip()[:120]))
            if web and re.search(r"\binstall\b[^;|&]*-o\s+www-data|\binstall\b[^;|&]*-g\s+www-data", code):
                fails.append("%s: install -o/-g www-data into the web tree: %s" % (rel, code.strip()[:120]))
if len(homes) < 6:
    fails.append("only %d root-writer homes found (%s) — the sweep stopped seeing files" % (len(homes), ", ".join(homes)))
dm = json.load(open(os.path.join(root, "scripts/offline-update-deploy-map.json"), encoding="utf-8"))
web_rules = [r for r in dm.get("prefix_rules", []) if r.get("dst_prefix", "").startswith("/var/www/")]
if not web_rules:
    fails.append("deploy map has no /var/www rule — this check stopped watching it")
for r in web_rules:
    if r.get("owner") != "root:root":
        fails.append("deploy map web rule owner is %r (want root:root)" % r.get("owner"))
runner = open(os.path.join(root, "etc/sa02m-update-runner.sh"), encoding="utf-8").read()
if "www-data:www-data" in runner:
    fails.append("etc/sa02m-update-runner.sh still names www-data:www-data (github manifest owner)")
tf = os.path.join(root, "etc/tmpfiles.d/sa02m-web-root.conf")
if not os.path.isfile(tf) or not re.search(r"^Z /var/www/network_config - root root -$", open(tf).read(), re.M):
    fails.append("etc/tmpfiles.d/sa02m-web-root.conf missing or without the Z line")
print("HOMES %d" % len(homes))
for f in fails:
    print("FAIL " + f)
PY
gh=$(grep '^HOMES ' "$T/g.out"); gf=$(grep '^FAIL ' "$T/g.out")
if [ -z "$gf" ] && [ -n "$gh" ]; then
    ok "G no root writer hands the web tree to www-data (${gh#HOMES } homes swept); deploy map + github builder say root:root; boot tmpfiles line present"
else
    while IFS= read -r l; do [ -n "$l" ] && bad "G ${l#FAIL }"; done <<< "$gf"
    [ -n "$gh" ] || bad "G sweep produced no output"
fi

echo "-----"
[ "$skips" -gt 0 ] && echo "($skips section(s) SKIPPED — reported as skips, not passes)"
if [ "$fails" -eq 0 ]; then echo "PASS (all checks)"; exit 0
else echo "FAIL ($fails check(s))"; exit 1; fi
