#!/usr/bin/env bash
# web-install-secret — the web panel's admin password must never reach the
# install log, and its first-install staging copy must be private. The log
# (/var/log/sa02m_install.log, scripts/lib.sh `log`) is served VERBATIM to any
# logged-in session by log.cgi (tail) and log_export.cgi (whole file), and the
# offline wrapper relays installer lines into the panel-served update.log; the
# password's one home is /etc/sa02m_web.env (a $6$ hash, 0640 root:www-data).
# The installer used to print `Логин: admin / <password>` into that log — the
# operator's real --pass value in plain text, or, on every --refresh, the
# default even when it was not the board's password (audit 2026-10-24 F1). The
# first-install credential was staged at a FIXED /tmp name under the default
# umask, so the hash (plaintext in the no-hasher fallback) was briefly
# world-readable (F8). Sibling of mqtt-install-secret, which closed the same
# class for the MQTT password.
#
# WHAT IT PINS
#   A. ALLOW-LIST over every RAW line of the installer tree (install.sh and every
#      top-level scripts/*.sh — the code whose output lands in that log) naming
#      the password variable (ADMIN_PASS, or the env key SA02M_WEB_PASS). Each
#      such line, whitespace-normalised, must be EXACTLY one of the sanctioned
#      lines in SANCTIONED below: the default, the --pass parse, the module
#      default, the htpasswd hash, the bootstrap hash, the two writes into the
#      private temp. Anything else FAILS and is printed verbatim — a `log`, an
#      echo, a redirect, a tee, an alias (`p=$ADMIN_PASS`), a helper this gate
#      has never heard of. An allow-list of uses, not a denylist of sinks
#      (quality-gate-rigor.md shape (b)). RAW, not comment-stripped, on purpose:
#      a whole-line `#` inside a heredoc still expands, and a comment-stripping
#      sweep would blank exactly that line — so the password's name may not
#      appear in a comment of these files at all.
#      The allow-list is non-vacuous BOTH ways: every sanctioned line must still
#      be present (a stale entry is shape (g)), and the sweep must see files.
#   B. The paths that name no line: no xtrace (`set -x`, `set -o xtrace`,
#      `bash -x`, BASH_XTRACEFD) and no environment dump (bare `env`,
#      `printenv`, bare `set` / `export -p` / `declare -p`) in those files — the
#      variable is exported to every module, so a dump would carry it.
#      Comment-stripped (lib_check.sh): a commented `set -x` is not a leak.
#   C. Private staging (F8), comment-stripped: the fixed name
#      `sa02m_web.env.bootstrap` is gone; the temp comes from `mktemp` under
#      `umask 077`; an EXIT trap removes it.
#      A sandboxed bootstrap block exercises plaintext/hash writes, install
#      and mktemp failure, and shell exit; mode 0600 and cleanup are observed.
#   D. Behaviour of the summary line install.sh prints instead
#      (scripts/lib.sh sa02m_web_cred_summary): sourced with a canary password
#      in the environment AND in the env file, driven through all five states
#      (kept / kept + --pass ignored / set from --pass / factory default /
#      missing) — each state's message must appear, the canary must appear in
#      NEITHER the log file nor stdout/stderr. install.sh must call it.
#   E. The scrub of lines OLDER installers already wrote (Operator decision
#      2026-10-24, «стирать»): the banner value becomes equal-byte-length stars in
#      the served logs. One home, etc/sa02m-web-log-scrub-lib.sh, two callers.
#      Static, comment-stripped: scripts/lib.sh sources it, install.sh runs it,
#      the update runner runs it in cmd_apply (its own wrapper must never fail
#      the update), both shell delivery paths install it to /usr/local/lib, the
#      offline deploy map ships it there, its name falls under the runner's
#      `etc/sa02m-*-lib.sh` → /usr/local/lib mapping and the pack allow-list
#      glob, and its default list names both served logs. Behaviour on canary
#      logs: the banner value is gone (plain, ANSI-coloured, heartbeat-embedded,
#      a rotation), every other byte is unchanged (including a `[WARN]` line
#      with the same label), owner (root or real sudo: a foreign uid) and mode survive, a
#      second run changes nothing (bytes, inode, mtime), concurrent appends and
#      open writers survive; a chunk-spanning prefix/value is masked; a missing path, a
#      symlink and a .gz are left alone, the printed output names files only.
#      The runner's wrapper is run under `set -euo pipefail` against a missing
#      lib and a failing stub: rc 0 both times.
#      Injected pwrite/fsync EIO errors reach installer and runner logs as
#      sanitized warnings, without exception text, false success or abort.
#
# WHAT IT DOES NOT REACH, stated so the next sweep does not re-derive it: a
# name assembled at run time (`v=ADMIN_; v+=PASS; echo "${!v}"`) names neither
# token on one line; files outside the swept set (the rootfs builder
# tools/debian-rootfs/ prints to the build host, not to a served file — its
# in-chroot install.sh run IS swept); argv exposure to local `ps` (`openssl
# passwd ... "$pass"`) is not a log and not pinned here.
#
# Mutations (RED/GREEN record in the commit body): re-introducing
# `log OK " Логин: admin / ${ADMIN_PASS}"` or the fixed /tmp staging turns A/C
# RED; commenting a sanctioned line out turns A RED (stale entry) and the
# presence pins RED — the latter are registered cases in comment-mutation-proof.
#
# Sandbox re-run without touching the repo: WEB_SECRET_ROOT=<copy of the tree>.
#
# Run: bash .ai-dev/quality/checks/web-install-secret.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. .ai-dev/quality/checks/lib_check.sh
# shellcheck source=.ai-dev/quality/checks/lib_skip.sh
. .ai-dev/quality/checks/lib_skip.sh

R=${WEB_SECRET_ROOT:-.}
fails=0
ok()  { printf 'web-install-secret: ok    %s\n' "$*"; }
bad() { printf 'web-install-secret: FAIL  %s\n' "$*"; fails=$((fails + 1)); }

INSTALL="$R/install.sh"
WEBSRV="$R/scripts/03-webserver.sh"
LIB="$R/scripts/lib.sh"
for f in "$INSTALL" "$WEBSRV" "$LIB"; do
    [ -f "$f" ] || { echo "web-install-secret: FAIL — $f absent"; exit 1; }
done

FILES=("$INSTALL")
for f in "$R"/scripts/*.sh; do [ -f "$f" ] && FILES+=("$f"); done
if [ "${#FILES[@]}" -lt 10 ]; then
    bad "the sweep sees only ${#FILES[@]} installer file(s) — expected install.sh + >=9 scripts/*.sh; a sweep that stopped matching proves nothing"
else
    ok "sweeping ${#FILES[@]} installer files (install.sh + scripts/*.sh)"
fi

# ── A. allow-list over every line naming the secret ────────────────────────
# Whitespace-normalised (runs of blanks -> one space, ends trimmed). Quoted
# heredoc: the lines are literal shell text, nothing below expands.
declare -A SANCTIONED=() SEEN=()
while IFS= read -r s; do
    s=$(printf '%s' "$s" | tr -s ' \t' ' ')
    s=${s# }; s=${s% }
    [ -n "$s" ] && SANCTIONED["$s"]=1
done <<'EOF'
export ADMIN_PASS="cyntron"
--pass) ADMIN_PASS="$2"; _ARG_PASS_SET=1; shift 2 ;;
: "${ADMIN_PASS:=cyntron}"
HASHED=$(openssl passwd -apr1 "$ADMIN_PASS")
_bootstrap_hash=$(web_auth_hash "${ADMIN_PASS}")
web_auth_write admin "${_bootstrap_hash:-$ADMIN_PASS}" > "$_web_env_tmp"
printf "SA02M_WEB_USER='admin'\nSA02M_WEB_PASS='%s'\n" "$(printf '%s' "${ADMIN_PASS}" | sed "s/'/'\\\\''/g")" > "$_web_env_tmp"
EOF

n_secret=0; n_unsanctioned=0
for f in "${FILES[@]}"; do
    text=$(tr -s ' \t' ' ' < "$f")
    n=0
    while IFS= read -r ln; do
        n=$((n + 1))
        case "$ln" in *ADMIN_PASS*|*SA02M_WEB_PASS*) ;; *) continue ;; esac
        ln=${ln# }; ln=${ln% }
        n_secret=$((n_secret + 1))
        if [ -n "${SANCTIONED["$ln"]+x}" ]; then
            SEEN["$ln"]=1
        else
            n_unsanctioned=$((n_unsanctioned + 1))
            hint="every use of the web password must be one of the sanctioned lines in this gate, or it is a candidate leak into the served install log"
            case "$ln" in log\ *) hint="a 'log' command interpolates the web password — it lands in the panel-served install log (audit 2026-10-24 F1)" ;; esac
            bad "${f#"$R"/}:$n unsanctioned use of the web password — $hint: $ln"
        fi
    done <<<"$text"
done

if [ "$n_secret" -lt "${#SANCTIONED[@]}" ]; then
    bad "only $n_secret line(s) name the web password, fewer than the ${#SANCTIONED[@]} sanctioned uses — the sweep is broken or the bootstrap was gutted"
fi
stale=0
for s in "${!SANCTIONED[@]}"; do
    if [ -z "${SEEN["$s"]+x}" ]; then
        stale=$((stale + 1))
        bad "sanctioned line no longer present (stale allow-list entry, or the line was commented out / reshaped — re-pin it here): $s"
    fi
done
if [ "$n_unsanctioned" -eq 0 ] && [ "$stale" -eq 0 ] && [ "$n_secret" -ge "${#SANCTIONED[@]}" ]; then
    ok "all $n_secret line(s) naming the web password are the ${#SANCTIONED[@]} sanctioned uses — no other line can carry it to a log or stdout"
fi

# ── B. paths that name no line: xtrace and environment dumps ───────────────
XTRACE='(^|[;&|({[:space:]])(set[[:space:]]+-[a-zA-Z]*x|set[[:space:]]+-o[[:space:]]+(xtrace|verbose)|set[[:space:]]+-[a-zA-Z]*v|bash[[:space:]]+-[a-zA-Z]*x)([[:space:]]|$)|BASH_XTRACEFD'
# Command position only (line start, or after ; & | ( ` $( ): a bare `set` in a
# trailing comment («... when set)») is prose, not a dump.
CMDPOS='(^|[;&|(`]|\$\()[[:space:]]*'
ENVDUMP="${CMDPOS}(env|set|export[[:space:]]+-p|declare[[:space:]]+-[a-zA-Z]*p|typeset[[:space:]]+-[a-zA-Z]*p)[[:space:]]*(\$|[|>;&)\`])|${CMDPOS}printenv([[:space:]]|\$)"
n_dump=0
for f in "${FILES[@]}"; do
    st=$(stripped_text "$f")
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        n_dump=$((n_dump + 1))
        bad "${f#"$R"/}: xtrace or an environment dump would print the exported web password into the log: $hit"
    done < <(grep -nE "$XTRACE|$ENVDUMP" <<<"$st" || true)
done
[ "$n_dump" -eq 0 ] && ok "no xtrace and no environment dump in the installer files"

# ── C. private first-install staging (F8) ──────────────────────────────────
if stripped_has "$WEBSRV" 'sa02m_web.env.bootstrap'; then
    bad "03-webserver.sh still stages the credential at the fixed /tmp/sa02m_web.env.bootstrap name (F8)"
else
    ok "no fixed-name credential staging file"
fi
if stripped_matches "$WEBSRV" '_web_env_tmp=\$\(umask 077 && mktemp\)'; then
    ok "the first-install credential is staged in a private mktemp under umask 077"
else
    bad "03-webserver.sh no longer stages the credential via \`_web_env_tmp=\$(umask 077 && mktemp)\` (F8)"
fi
if stripped_has "$WEBSRV" "trap 'rm -f \"\$_web_env_tmp\"' EXIT"; then
    ok "an EXIT trap removes the staging temp"
else
    bad "03-webserver.sh has no EXIT trap removing the staging temp — an interrupted run leaves the credential behind"
fi

# ── D. the summary line: the fact, never the value ─────────────────────────
if stripped_matches "$INSTALL" '^[[:space:]]*sa02m_web_cred_summary "\$_WEB_ENV_EXISTED" "\$_ARG_PASS_SET"[[:space:]]*$'; then
    ok "install.sh reports the credential through sa02m_web_cred_summary"
else
    bad "install.sh does not call sa02m_web_cred_summary \"\$_WEB_ENV_EXISTED\" \"\$_ARG_PASS_SET\" — the summary no longer reports the credential state (or reports it some other way)"
fi

tmp=$(mktemp -d) || { echo "web-install-secret: FAIL — mktemp -d"; exit 1; }
trap 'rm -rf "$tmp"' EXIT
# Run only the bootstrap block, with all system paths rebound into a sandbox.
# This proves the private file and cleanup behavior, not just their spelling.
bootstrap=$(awk '/^if \[ ! -f \/etc\/sa02m_web.env \]; then$/{f=1} /^if \[ -x \/usr\/local\/sbin\/sa02m-repair-web-env \]; then$/{f=0} f{print}' "$WEBSRV")
if [ -z "$bootstrap" ]; then
    bad "C: credential bootstrap block is absent"
else
    for scenario in plain hashed install-fail interrupted mktemp-fail; do
        c="$tmp/bootstrap-$scenario"; mkdir -p "$c/temp"
        block=${bootstrap//\/etc\/sa02m_web.env/$c/web.env}
        block=${block//\/usr\/local\/lib\/sa02m-web-auth-lib.sh/$c/auth.sh}
        if [ "$scenario" = hashed ]; then
            printf '%s\n' 'web_auth_hash() { printf "%s" hashed-canary; }' \
                'web_auth_write() { printf "user=%s hash=%s\n" "$1" "$2"; }' > "$c/auth.sh"
        fi
        result=$(bash -c '
            block=$1; c=$2; scenario=$3
            export TMPDIR="$c/temp" ADMIN_PASS=Bootstrap-canary LOG_FILE="$c/log"
            log() { printf "%s %s\n" "$1" "$2" >> "$LOG_FILE"; }
            install() {
                stat -c %a "${@: -2:1}" > "$c/mode"
                case "$scenario" in
                    install-fail) return 1 ;;
                    interrupted) exit 42 ;;
                esac
                cp "${@: -2:1}" "${@: -1}"
            }
            if [ "$scenario" = mktemp-fail ]; then mktemp() { return 1; }; fi
            eval "$block"
        ' _ "$block" "$c" "$scenario" 2>&1)
        rc=$?
        expected_rc=0; [ "$scenario" = interrupted ] && expected_rc=42
        [ "$rc" -eq "$expected_rc" ] || bad "C: $scenario unexpected exit $rc"
        if [ "$scenario" != mktemp-fail ]; then
            [ "$(cat "$c/mode" 2>/dev/null)" = 600 ] || bad "C: $scenario staging mode is not 0600"
        fi
        case "$scenario" in
            plain) grep -qF Bootstrap-canary "$c/web.env" || bad "C: plaintext fallback did not reach credential file" ;;
            hashed) grep -qF hashed-canary "$c/web.env" || bad "C: hashed credential did not reach credential file" ;;
            *) [ ! -e "$c/web.env" ] || bad "C: $scenario unexpectedly created credential file" ;;
        esac
        if [ "$scenario" = install-fail ] || [ "$scenario" = mktemp-fail ]; then
            grep -qF ERR "$c/log" || bad "C: $scenario did not report failure"
        fi
        logged=$(cat "$c/log" 2>/dev/null)
        text_has "$result$logged" Bootstrap-canary && bad "C: $scenario printed the credential"
        leftovers=("$c/temp"/*)
        [ ! -e "${leftovers[0]}" ] || bad "C: $scenario left a staging file behind"
        ok "C: exercised $scenario bootstrap, private mode and cleanup"
    done
fi
CANARY='Canary-7f3a-pass'
# state | existed | pass_given | env present | literal that must appear
STATES='kept|1|0|1|Логин и пароль панели не менялись
kept-pass-ignored|1|1|1|--pass не применён
set-from-pass|0|1|1|пароль задан из --pass
factory-default|0|0|1|заводской пароль
missing|0|0|0|не найден'
while IFS='|' read -r name existed given present want; do
    [ -n "$name" ] || continue
    d="$tmp/$name"; mkdir -p "$d"
    [ "$present" = 1 ] && printf "SA02M_WEB_USER='admin'\nSA02M_WEB_PASS='%s'\n" "$CANARY" > "$d/web.env"
    out=$(set +u; export ADMIN_PASS="$CANARY" LOG_FILE="$d/log"
          # shellcheck disable=SC1090
          . "$LIB" >/dev/null 2>&1 || exit 97
          command -v sa02m_web_cred_summary >/dev/null || exit 98
          sa02m_web_cred_summary "$existed" "$given" "$d/web.env" 2>&1)
    rc=$?
    if [ "$rc" -eq 98 ]; then bad "[$name] scripts/lib.sh defines no sa02m_web_cred_summary"; continue; fi
    if [ "$rc" -ne 0 ]; then bad "[$name] sa02m_web_cred_summary exited $rc — install.sh runs under set -e, a non-zero return aborts the install"; continue; fi
    logged=$(cat "$d/log" 2>/dev/null)
    if [ -z "$logged" ]; then bad "[$name] the summary wrote nothing to the log"; continue; fi
    if text_has "$logged$out" "$CANARY"; then
        bad "[$name] the password value reached the log or stdout"
    elif ! text_has "$logged" "$want"; then
        bad "[$name] the summary does not say «$want»: $logged"
    else
        ok "[$name] reports «$want», value absent from log and stdout"
    fi
    if [ "$name" = kept ] && text_has "$logged" '--pass не применён'; then
        bad "[kept] warns that --pass was ignored although none was given"
    fi
done <<<"$STATES"

# ── E. scrub of the legacy lines already on disk ───────────────────────────
SCRUB="$R/etc/sa02m-web-log-scrub-lib.sh"
RUNNER="$R/etc/sa02m-update-runner.sh"
WWWONLY="$R/scripts/update-www-only.sh"
DMAP="$R/scripts/offline-update-deploy-map.json"
ALLOW="$R/scripts/offline-update-allowlist.txt"
for f in "$SCRUB" "$RUNNER" "$WWWONLY" "$DMAP" "$ALLOW"; do
    [ -f "$f" ] || bad "E: $f absent — the scrub (or its delivery) cannot be checked"
done
pin() {  # file ERE what
    if stripped_matches "$1" "$2"; then ok "E: $3"; else bad "E: ${1#"$R"/} — $3 is missing"; fi
}
pin "$LIB" '^[[:space:]]*source "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)/\.\./etc/sa02m-web-log-scrub-lib\.sh"' \
    "scripts/lib.sh sources the scrub lib (install.sh path)"
pin "$INSTALL" '^done < <\(sa02m_scrub_cred_banner_logs 2>&1\)$' "install.sh runs the scrub and captures diagnostics"
pin "$RUNNER" '^[[:space:]]+scrub_cred_banner_logs \|\| true$' "the update runner runs the scrub in cmd_apply"
pin "$RUNNER" '^WEB_LOG_SCRUB_LIB="\$\{SA02M_WEB_LOG_SCRUB_LIB:-/usr/local/lib/sa02m-web-log-scrub-lib\.sh\}"$' \
    "the runner reads the lib from /usr/local/lib"
pin "$WEBSRV" 'sa02m_atomic_install -m 644 "\$SCRIPT_DIR/\.\./etc/sa02m-web-log-scrub-lib\.sh" /usr/local/lib/sa02m-web-log-scrub-lib\.sh' \
    "03-webserver.sh installs the lib to /usr/local/lib"
pin "$WWWONLY" 'sa02m_atomic_install -m 644 "\$REPO_ETC/sa02m-web-log-scrub-lib\.sh" /usr/local/lib/sa02m-web-log-scrub-lib\.sh' \
    "update-www-only.sh installs the lib to /usr/local/lib"
pin "$DMAP" '"etc/sa02m-web-log-scrub-lib\.sh": \{' "the offline deploy map ships the lib"
pin "$DMAP" '"dst": "/usr/local/lib/sa02m-web-log-scrub-lib\.sh"' "the offline deploy map targets /usr/local/lib"
pin "$ALLOW" '^etc/sa02m-web-\*\.sh$' "the offline pack allow-list glob takes etc/sa02m-web-*.sh"
pin "$RUNNER" 'if name\.endswith\("-lib\.sh"\)' "the GitHub-OTA mapper sends etc/sa02m-*-lib.sh to /usr/local/lib"
pin "$SCRUB" '/var/log/sa02m_install\.log /var/log/sa02m_install\.log\.\[0-9\]\*' "the scrub list names the install log and its rotations"
pin "$SCRUB" '/var/lib/sa02m-web-build/update\.log /var/lib/sa02m-web-build/update\.log\.\[0-9\]\*' "the scrub list names the legacy update.log and its rotations"

E="$tmp/scrub"; mkdir -p "$E"
CAN='Canary-9b1e-old-pass'
ESC=$''
TS='[2026-10-01 10:00:00]'
# Each fixture is written twice from one table: as found on a board (V=value)
# and as the scrub must leave it (V=equal-length stars) — the expectation is spelled out, not
# derived from the scrub's own expression.
mk_install() {  # $1 = what the banner tails carry
    printf '%s
' "────────" "$TS [INFO] === [03] Настройка веб-сервера ==="
    printf '%s
' "$TS [OK]  Логин: admin / $1"
    printf '%s
' "$TS [OK]  Логин: admin — заводской пароль (README, «Доступы по умолчанию»), хранится в /etc/sa02m_web.env"
    printf '%s
' "$TS [WARN]  Логин: admin / not-a-banner-keep"
    printf '%s
' "$TS [OK]  Логин: admin / ***"
    printf '%s
' "${ESC}[0;32m$TS [OK]  Логин: admin / $2"
    printf '%s' "$TS [OK] tail without newline"
}
MASK=${CAN//?/*}
mk_install "$CAN" "$CAN${ESC}[0m" > "$E/install.log";   mk_install "$MASK" "$MASK${ESC}[0m" > "$E/install.log.want"
mk_rot() { printf '%s
' "$TS [OK]  Логин: admin / $1" "kept line"; }
mk_rot "$CAN-rotated" > "$E/install.log.1";             mk_rot "$MASK********" > "$E/install.log.1.want"
mk_upd() { printf '%s
' "2026-10-01 10:01:00 [INFO] … install.sh работает (3 мин): $TS [OK]  Логин: admin / $1" "other"; }
mk_upd "$CAN" > "$E/update.log";                        mk_upd "$MASK" > "$E/update.log.want"
printf '%s
' "$TS [OK]  Логин: admin / $CAN" > "$E/target.log"
real_link=0
if ln -s "$E/target.log" "$E/link.log" 2>/dev/null && [ -L "$E/link.log" ]; then real_link=1; else rm -f "$E/link.log"; fi
printf '%s
' "$TS [OK]  Логин: admin / $CAN" > "$E/install.log.2.gz"
printf '%s
' "no banner here" > "$E/clean.log"
cp "$E/target.log" "$E/target.want"; cp "$E/install.log.2.gz" "$E/gz.want"
chmod 0640 "$E/install.log"; chmod 0604 "$E/update.log"
as_root=0
if [ "$(id -u)" = 0 ]; then as_root=1; chown 4321:4322 "$E/install.log"; fi
before() { stat -c '%u:%g %a' "$1" 2>/dev/null; }
b_install=$(before "$E/install.log"); b_update=$(before "$E/update.log")
clean_ino=$(stat -c '%i %Y' "$E/clean.log")
# shellcheck disable=SC1090
out=$(set +u; . "$SCRUB" >/dev/null 2>&1 || exit 97
      sa02m_scrub_cred_banner "$E/install.log" "$E/install.log.1" "$E/update.log" "$E/link.log" \
          "$E/install.log.2.gz" "$E/clean.log" "$E/missing.log" 2>&1)
rc=$?
if [ "$rc" -eq 97 ]; then bad "E: the scrub lib does not source"
elif [ "$rc" -ne 0 ]; then bad "E: sa02m_scrub_cred_banner exited $rc — it must never fail its caller"
else ok "E: the scrub returns 0 (missing path included)"; fi
for f in install.log install.log.1 update.log; do
    if cmp -s "$E/$f" "$E/$f.want"; then
        ok "E: $f — banner value masked in place, every other byte unchanged"
    else
        bad "E: $f differs from the expected scrub (value left, or another line touched): $(diff "$E/$f.want" "$E/$f" 2>&1 | head -4 | tr '\n' ' ')"
    fi
done
grep -qF "$CAN" "$E/install.log" "$E/install.log.1" "$E/update.log" && bad "E: the canary survived in a scrubbed log"
text_has "$out" "$CAN" && bad "E: the scrub printed a line's content"
want_out=$(printf '%s\n' "$E/install.log" "$E/install.log.1" "$E/update.log")
[ "$out" = "$want_out" ] && ok "E: the output names exactly the rewritten files" \
    || bad "E: the output is not exactly the rewritten files: $out"
[ "$(before "$E/install.log")" = "$b_install" ] && ok "E: install.log owner and mode kept ($b_install)" \
    || bad "E: install.log owner/mode changed: $b_install -> $(before "$E/install.log")"
if [ "$as_root" != 1 ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        # CI runs the gate unprivileged with passwordless sudo. Keep ordinary
        # fixtures caller-owned, and check foreign ownership inside one real
        # privileged probe; chowning the main fixture would make it unreadable
        # to the caller. Its parent directory stays caller-owned for cleanup.
        mkdir -p "$E/foreign"
        if sudo -n bash -c '
            set -euo pipefail
            . "$1"
            f="$2/log"
            printf "[OK]  Логин: admin / abc\n" > "$f"
            chmod 0640 "$f"
            chown 4321:4322 "$f"
            before=$(stat -c "%u:%g %a %i" "$f")
            out=$(sa02m_scrub_cred_banner "$f")
            [ "$out" = "$f" ]
            [ "$(cat "$f")" = "[OK]  Логин: admin / ***" ]
            [ "$(stat -c "%u:%g %a %i" "$f")" = "$before" ]
            after=$(stat -c "%u:%g %a %i %Y" "$f")
            [ -z "$(sa02m_scrub_cred_banner "$f")" ]
            [ "$(stat -c "%u:%g %a %i %Y" "$f")" = "$after" ]
        ' _ "$SCRUB" "$E/foreign"; then
            ok "E: real sudo probe preserves foreign owner/mode/inode and is idempotent"
        else
            bad "E: real sudo foreign-owner probe failed"
        fi
    else
        quality_skip "web-install-secret E: foreign-owner assertion needs root or passwordless sudo (same-uid owner checked)"
    fi
fi
[ "$(before "$E/update.log")" = "$b_update" ] && ok "E: update.log owner and mode kept" \
    || bad "E: update.log owner/mode changed: $b_update -> $(before "$E/update.log")"
if [ "$real_link" = 1 ]; then
    cmp -s "$E/target.log" "$E/target.want" && ok "E: a symlink is not followed" || bad "E: the scrub rewrote a symlink's target"
else
    quality_skip "web-install-secret E: symlink case not observable here (ln -s does not make a symlink on this host)"
fi
cmp -s "$E/install.log.2.gz" "$E/gz.want" && ok "E: a compressed rotation is left alone" || bad "E: the scrub rewrote a .gz"
[ "$(stat -c '%i %Y' "$E/clean.log")" = "$clean_ino" ] && ok "E: a log with no banner is not rewritten" || bad "E: a clean log was rewritten"
# Idempotent: the second run rewrites nothing.
id1=$(stat -c '%i %Y' "$E/install.log" "$E/update.log"); cp "$E/install.log" "$E/install.log.r1"
# shellcheck disable=SC1090
out2=$(set +u; . "$SCRUB" >/dev/null 2>&1; sa02m_scrub_cred_banner "$E/install.log" "$E/update.log" 2>&1)
if [ -z "$out2" ] && [ "$(stat -c '%i %Y' "$E/install.log" "$E/update.log")" = "$id1" ] && cmp -s "$E/install.log" "$E/install.log.r1"; then
    ok "E: a second run changes nothing (bytes, inode, mtime)"
else
    bad "E: the scrub is not idempotent (second run printed «$out2» or rewrote a file)"
fi
# After both runs: the second one reaches the no-change branch, which must clean up too.
for left in "$E"/*.scrub.*; do [ -e "$left" ] && bad "E: a scrub temp file was left behind: $left"; done
# Inject a real append after the snapshot read and immediately before pwrite.
# Keep that writer fd open until the scrub returns, then append again: replacing
# the inode would lose one or both records. The wrapper changes no source file.
mkdir -p "$E/inject"
cat > "$E/inject/python3" <<'PY'
#!/usr/bin/python3
import os
import sys
code = sys.stdin.read()
sys.argv = sys.argv[1:]
writer = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)
write = os.pwrite
injected = False
def interleaved(fd, data, offset):
    global injected
    if not injected:
        os.write(writer, b'concurrent-update-status\n')
        injected = True
    return write(fd, data, offset)
os.pwrite = interleaved
exec(compile(code, '<scrub>', 'exec'), {'__name__': '__main__'})
if not injected:
    raise SystemExit('scrub never reached the append injection')
os.close(writer)
PY
chmod 755 "$E/inject/python3"
# The original copy/rename implementation reaches this injection instead of
# pwrite; it must fail the same preservation assertion, not evade the fixture.
cat > "$E/inject/mv" <<'SH'
#!/bin/bash
printf 'concurrent-update-status\n' >> "$TEST_SCRUB_LOG"
exec /usr/bin/mv "$@"
SH
chmod 755 "$E/inject/mv"
printf '[OK]  Логин: admin / abc\nkeep\n' > "$E/concurrent.log"
printf '[OK]  Логин: admin / ***\nkeep\nconcurrent-update-status\nopen-writer-after-scrub\n' > "$E/concurrent.want"
inode_before=$(stat -c %i "$E/concurrent.log")
TEST_SCRUB_LOG="$E/concurrent.log" PATH="$E/inject:$PATH" bash -c 'exec 9>>"$2"; . "$1"; sa02m_scrub_cred_banner "$2"; printf "open-writer-after-scrub\n" >&9' _ "$SCRUB" "$E/concurrent.log" > "$E/concurrent.out" 2>&1
if cmp -s "$E/concurrent.log" "$E/concurrent.want" && [ "$(stat -c %i "$E/concurrent.log")" = "$inode_before" ]; then
    ok "E: append between read/write and existing open writer both survive"
else
    bad "E: scrub lost a concurrent append, replaced the inode, or did not execute the injection"
fi
# Exercise prefix and value crossing the bounded read chunks, with CRLF and a
# trailing ANSI reset retained. The expected output is independent fixture data.
python3 - "$E" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
prefix = '[OK]  Логин: admin / '.encode()
before = b'x' * (65536 - 7)
after = b'\x1b[0m\r\nunchanged\n'
(p / 'large.log').write_bytes(before + prefix + b's' * 140000 + after)
(p / 'large.want').write_bytes(before + prefix + b'*' * 140000 + after)
PY
bash -c '. "$1"; sa02m_scrub_cred_banner "$2"' _ "$SCRUB" "$E/large.log" > "$E/large.out" 2>&1
cmp -s "$E/large.log" "$E/large.want" && ok "E: chunk-spanning prefix/value, CRLF and ANSI reset preserved" \
    || bad "E: bounded scrub mishandled a chunk boundary or control suffix"
# The runner's wrapper: never fails the update, no lib = no-op.
fn=$(awk '/^scrub_cred_banner_logs\(\) \{$/{f=1} f{print} f&&/^\}$/{exit}' "$RUNNER")
if [ -z "$fn" ]; then
    bad "E: the runner has no scrub_cred_banner_logs() function"
else
    printf '%s\n' 'sa02m_scrub_cred_banner_logs() { echo /var/log/x; return 1; }' > "$E/stub-lib.sh"
    for lib in "$E/no-such-lib.sh" "$E/stub-lib.sh"; do
        r=$(bash -c 'set -euo pipefail; log() { printf "LOG %s\n" "$*"; }; WEB_LOG_SCRUB_LIB=$1; eval "$2"; scrub_cred_banner_logs; echo rc=$?' _ "$lib" "$fn" 2>&1)
        case "$r" in
            *rc=0*) ok "E: runner wrapper rc 0 under set -e with ${lib##*/}" ;;
            *) bad "E: runner wrapper failed under set -e with ${lib##*/}: $r" ;;
        esac
    done
fi

# Unexpected scrub failures stay nonfatal but must reach both callers' logs.
# Inject hostile exception text to prove only path + errno are emitted.
mkdir -p "$E/fault"
cat > "$E/fault/python3" <<'PY'
#!/usr/bin/python3
import os
import sys
code = sys.stdin.read()
sys.argv = sys.argv[1:]
def fail(*args):
    raise OSError(5, 'Fault-secret-must-not-be-logged')
setattr(os, os.environ['SCRUB_FAULT'], fail)
exec(compile(code, '<scrub>', 'exec'), {'__name__': '__main__'})
PY
chmod 755 "$E/fault/python3"
install_loop=$(awk '/^while IFS= read -r _scrubbed; do$/{f=1} f{print} f&&/^done /{exit}' "$INSTALL")
if [ -z "$install_loop" ] || [ -z "$fn" ]; then
    bad "E: scrub caller missing for fault-injection tests"
else
    for fault in pwrite fsync; do
        for caller in installer runner; do
            target="$E/fault-$caller-$fault.log"
            printf '[OK]  Логин: admin / Fault-secret-must-not-be-logged\n' > "$target"
            printf '. %q\nsa02m_scrub_cred_banner_logs() { sa02m_scrub_cred_banner %q; }\n' "$SCRUB" "$target" > "$E/fault-lib.sh"
            out=$(SCRUB_FAULT="$fault" PATH="$E/fault:$PATH" bash -c '
                set -euo pipefail
                caller=$1; lib=$2; install_loop=$3; runner_fn=$4; LOG_FILE=$5
                log() { printf "%s\n" "$*" >> "$LOG_FILE"; }
                if [ "$caller" = installer ]; then
                    . "$lib"
                    eval "$install_loop"
                else
                    WEB_LOG_SCRUB_LIB=$lib
                    eval "$runner_fn"
                    scrub_cred_banner_logs
                fi
                echo survived
            ' _ "$caller" "$E/fault-lib.sh" "$install_loop" "$fn" "$target.diagnostic" 2>&1)
            rc=$?
            diagnostic=$(cat "$target.diagnostic" 2>/dev/null)
            if [ "$rc" = 0 ] && [ "$out" = survived ] \
                && text_has "$diagnostic" 'WARN scrub error: path=' \
                && text_has "$diagnostic" "$target" \
                && text_has "$diagnostic" 'errno=5' \
                && ! text_has "$diagnostic$out" 'Fault-secret-must-not-be-logged' \
                && ! text_has "$diagnostic" 'password removed' \
                && ! text_has "$diagnostic" 'удалён пароль'; then
                ok "E: $caller $fault failure logs sanitized warning, no success, remains nonfatal"
            else
                bad "E: $caller $fault failure is silent, leaks data, claims success or aborts"
            fi
        done
    done
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "web-install-secret: ALL OK"
    exit 0
fi
echo "web-install-secret: $fails FAILURE(S)"
exit 1
