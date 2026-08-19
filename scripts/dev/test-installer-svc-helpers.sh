#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# test-installer-svc-helpers.sh — regression harness for the installer's
# service-state helpers (scripts/lib.sh: sa02m_svc_capture / sa02m_svc_apply /
# sa02m_svc_kick / sa02m_pkg_install_tier thirdparty / sa02m_pip_install) and
# the stack-policy lib (etc/sa02m-stacks-policy.sh). Quality row
# `installer-svc-helpers`. Contract: docs/contracts/installer-refresh-policy.md.
#
# Why this exists: every defect it pins is SILENT on the device — a re-run of
# the installer that re-enables a service the operator stopped, a D-Bus timeout
# read as "new unit" (⇒ widening), a second restart after the ctl already
# restarted, a refresh that installs a third-party package. None of them fail
# a syntax gate; the board just comes back different.
#
# Method: source the SHIPPED lib.sh with a `systemctl` PATH shim that RECORDS
# every call and answers from a per-unit state table (is-enabled / is-active /
# show -p ActiveEnterTimestampMonotonic / show -p NeedDaemonReload / cat), plus
# timeout/apt-get/dpkg/pip3/python3/getent/ip shims; `log` captured to a
# buffer; SA02M_STACKS_CONF pointed into a scratch dir. Each case asserts the
# EXACT recorded widening-call list AND the log line. Nothing talks to the real
# systemd, apt or pip.
#
# Drive-to-failure: point SVC_HELPERS_LIB at a pre-refresh scripts/lib.sh (it
# has no sa02m_svc_apply) — the source guard fails loudly; or revert the
# never-widen rule in _sa02m_svc_apply_app_existing and the "disabled+inactive
# ⇒ zero calls" case goes red.
#
# Run: bash scripts/dev/test-installer-svc-helpers.sh   (stdlib bash only)
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

LIB=${SVC_HELPERS_LIB:-scripts/lib.sh}
T=$(mktemp -d) || exit 1
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"
ST="$T/state"          # per-unit state table the systemctl shim answers from
CALLS="$T/calls.log"   # every systemctl invocation, one line each
mkdir -p "$BIN" "$ST"

fails=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# ── Shims (PATH-first) ─────────────────────────────────────────────────────
# systemctl: records "$*" to CALLS and answers queries from $ST/<unit>.<key>.
# State-file conventions: <u>.enabled holds the is-enabled word (missing file =
# unit unknown: rc 1, no output; the literal `timeout` = exit 124); <u>.active
# holds the is-active word (missing = rc 3 "inactive"? NO — missing means the
# shim answers nothing rc 3; tests always seed it); <u>.ts / <u>.needreload
# feed `show -p`.
cat > "$BIN/systemctl" <<SHIM
#!/bin/bash
ST="$ST"; CALLS="$CALLS"
SHIM
cat >> "$BIN/systemctl" <<'SHIM'
args=("$@")
printf 'systemctl %s\n' "$*" >> "$CALLS"
# strip a leading --root=... (rootfs-build case) but keep it recorded above
[[ "${1:-}" == --root=* ]] && shift
cmd=${1:-}; shift || true
case "$cmd" in
    is-enabled)
        u=$1
        f="$ST/$u.enabled"
        [ -f "$f" ] || exit 1
        v=$(cat "$f")
        [ "$v" = timeout ] && exit 124
        printf '%s\n' "$v"
        [ "$v" = enabled ] && exit 0 || exit 1
        ;;
    is-active)
        u=$1
        f="$ST/$u.active"
        [ -f "$f" ] || exit 3
        v=$(cat "$f")
        [ "$v" = timeout ] && exit 124
        printf '%s\n' "$v"
        [ "$v" = active ] && exit 0 || exit 3
        ;;
    show)
        # show -p <prop> --value <unit>
        prop=""; u=""
        while [ $# -gt 0 ]; do
            case "$1" in
                -p) prop=$2; shift 2 ;;
                --value) shift ;;
                *) u=$1; shift ;;
            esac
        done
        case "$prop" in
            ActiveEnterTimestampMonotonic) cat "$ST/$u.ts" 2>/dev/null ;;
            NeedDaemonReload) cat "$ST/$u.needreload" 2>/dev/null || echo no ;;
        esac
        exit 0
        ;;
    cat)
        u=$1
        [ -f "$ST/$u.enabled" ] && exit 0 || exit 1
        ;;
    enable|disable|start|stop|restart|unmask|daemon-reload)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
SHIM

# timeout: swallow the duration, run the command (exit code passes through —
# incl. the shim's 124).
printf '#!/bin/bash\nshift\nexec "$@"\n' > "$BIN/timeout"
# dpkg: nothing is ever installed (drives the tier helpers into the apt path).
printf '#!/bin/bash\nexit 1\n' > "$BIN/dpkg"
# apt-get: record + succeed.
cat > "$BIN/apt-get" <<SHIM
#!/bin/bash
printf 'apt-get %s\n' "\$*" >> "$CALLS"
exit 0
SHIM
# pip3: `install --help` advertises --break-system-packages; anything else is
# recorded.
cat > "$BIN/pip3" <<SHIM
#!/bin/bash
if [ "\${1:-}" = install ] && [ "\${2:-}" = --help ]; then
    echo "  --break-system-packages"
    exit 0
fi
printf 'pip3 %s\n' "\$*" >> "$CALLS"
exit 0
SHIM
# python3: `-c "import X"` succeeds iff X is listed in $T/py-ok (one per line).
cat > "$BIN/python3" <<SHIM
#!/bin/bash
if [ "\${1:-}" = -c ]; then
    mod=\$(printf '%s' "\$2" | sed -n 's/^import //p')
    grep -qxF "\$mod" "$T/py-ok" 2>/dev/null && exit 0
    exit 1
fi
exit 0
SHIM
# getent (the online probe): succeeds iff $T/online exists. ip: no default route.
printf '#!/bin/bash\n[ -e "%s" ] && exit 0 || exit 2\n' "$T/online" > "$BIN/getent"
printf '#!/bin/bash\nexit 0\n' > "$BIN/ip"
chmod +x "$BIN"/*
PATH="$BIN:$PATH"

# ── Source the shipped lib ─────────────────────────────────────────────────
export LOG_FILE="$T/install.log"
export SA02M_STACKS_CONF="$T/etc/sa02m_stacks.conf"
export SA02M_STACK_PROBE_ROOT="$T/root"
export SA02M_SYSV_RC_DIRS="$T/rc2.d $T/rc3.d"
mkdir -p "$T/etc" "$T/root" "$T/rc2.d" "$T/rc3.d"
: > "$T/py-ok"

# shellcheck disable=SC1090
source "$LIB" || { echo "FAIL  cannot source $LIB"; exit 1; }
for f in sa02m_svc_capture sa02m_svc_apply sa02m_svc_kick sa02m_svc_unmask sa02m_unit_exists \
         sa02m_sysv_autostart sa02m_pkg_install_tier sa02m_pip_install \
         sa02m_stack_policy_get sa02m_stack_policy_set sa02m_stack_policy_derive \
         sa02m_stack_verdict sa02m_stack_installed sa02m_stack_is_thirdparty; do
    declare -F "$f" >/dev/null \
        || { echo "FAIL  $LIB does not define ${f}() — wrong lib revision or the source guard broke"; exit 1; }
done

# Capture log lines in-memory (stub AFTER sourcing so the buffer wins).
LOGCAP=""
log() { LOGCAP="${LOGCAP}[$1] ${2:-}"$'\n'; }

# ── Case plumbing ──────────────────────────────────────────────────────────
reset_case() {
    rm -f "$ST"/* "$CALLS"
    : > "$CALLS"
    LOGCAP=""
    # The SA02M_* below are the SHIPPED lib's state (read inside lib.sh):
    # shellcheck disable=SC2034
    SA02M_SVC_EN=()
    # shellcheck disable=SC2034
    SA02M_SVC_ACT=()
    # shellcheck disable=SC2034
    SA02M_SVC_TS=()
    # shellcheck disable=SC2034
    SA02M_ONLINE_CACHE=""
    unset SA02M_INSTALL_MODE SA02M_WITH_OPTIONAL SA02M_ROOTFS_BUILD 2>/dev/null || true
}
seed() {  # seed <unit> <enabled|-> <active|-> [ts] [needreload]
    local u=$1
    [ "${2:-}" != - ] && [ -n "${2:-}" ] && printf '%s' "$2" > "$ST/$u.enabled"
    [ "${3:-}" != - ] && [ -n "${3:-}" ] && printf '%s' "$3" > "$ST/$u.active"
    [ -n "${4:-}" ] && printf '%s' "$4" > "$ST/$u.ts"
    [ -n "${5:-}" ] && printf '%s' "$5" > "$ST/$u.needreload"
}
# Widening verbs actually recorded for a unit (enable/start/restart/unmask,
# stop/disable are tightening but listed too where a case wants exactness).
verbs() {  # verbs <unit> — prints space-joined widening verbs, in order
    awk -v u="$1" '$0 ~ ("systemctl.*" u) {
        for (i = 2; i <= NF; i++)
            if ($i == "enable" || $i == "start" || $i == "restart" || $i == "unmask" || $i == "stop" || $i == "disable")
                { printf "%s%s", sep, $i; sep = " " }
    } END { print "" }' "$CALLS"
}
has_log()  { case "$LOGCAP" in *"$1"*) return 0 ;; *) return 1 ;; esac; }

echo "── 1. app decision table ──"

# 1a. new unit, app/on ⇒ enable + start
reset_case
seed svc-a.service - inactive     # unit absent: no .enabled file, manager alive
sa02m_svc_capture svc-a.service
rm -f "$ST/svc-a.service.active"; seed svc-a.service - inactive
sa02m_svc_apply svc-a.service app on
[ "$(verbs svc-a.service)" = "enable start" ] \
    && ok "app/on new unit: enable + start (exact)" \
    || bad "app/on new unit: expected 'enable start', got '$(verbs svc-a.service)'"
has_log "первая установка — включён и запущен" && [ "$SA02M_SVC_LAST_RESULT" = started ] \
    && ok "app/on new unit: log line + LAST_RESULT=started" \
    || bad "app/on new unit: log/LAST_RESULT wrong (got $SA02M_SVC_LAST_RESULT; log: $LOGCAP)"

# 1b. existing enabled+active, TS unchanged ⇒ restart ONLY
reset_case
seed svc-b.service enabled active 111222
sa02m_svc_capture svc-b.service
sa02m_svc_apply svc-b.service app on
[ "$(verbs svc-b.service)" = "restart" ] \
    && ok "existing enabled+active (TS unchanged): restart only" \
    || bad "existing enabled+active: expected 'restart', got '$(verbs svc-b.service)'"
[ "$SA02M_SVC_LAST_RESULT" = restarted ] && has_log "перезапущен на свежем коде" \
    && ok "existing enabled+active: LAST_RESULT=restarted + log" \
    || bad "existing enabled+active: LAST_RESULT=$SA02M_SVC_LAST_RESULT"

# 1c. existing disabled+inactive ⇒ ZERO widening calls + «состояние сохранено»
reset_case
seed svc-c.service disabled inactive
sa02m_svc_capture svc-c.service
sa02m_svc_apply svc-c.service app on
[ -z "$(verbs svc-c.service)" ] \
    && ok "existing disabled+inactive: zero widening calls (the never-widen rule)" \
    || bad "existing disabled+inactive WIDENED: '$(verbs svc-c.service)'"
has_log "прежнее состояние сохранено (en=disabled act=inactive)" \
    && ok "existing disabled+inactive: «состояние сохранено» logged" \
    || bad "existing disabled+inactive: preserve line missing (log: $LOGCAP)"

# 1d. masked ⇒ zero widening calls, left-masked
reset_case
seed svc-d.service masked inactive
sa02m_svc_capture svc-d.service
sa02m_svc_apply svc-d.service app on
[ -z "$(verbs svc-d.service)" ] && [ "$SA02M_SVC_LAST_RESULT" = left-masked ] \
    && ok "masked app unit: zero calls, LAST_RESULT=left-masked" \
    || bad "masked app unit: verbs='$(verbs svc-d.service)' LAST_RESULT=$SA02M_SVC_LAST_RESULT"

# 1e. active but TS CHANGED since capture ⇒ no restart
reset_case
seed svc-e.service enabled active 100
sa02m_svc_capture svc-e.service
printf '%s' 999 > "$ST/svc-e.service.ts"     # the ctl/vendor already restarted it
sa02m_svc_apply svc-e.service app on
[ -z "$(verbs svc-e.service)" ] && has_log "уже перезапущен после копирования кода" \
    && ok "TS witness: already-restarted unit is NOT restarted again" \
    || bad "TS witness failed: verbs='$(verbs svc-e.service)' (log: $LOGCAP)"

# 1f. norestart ⇒ never restart even when active
reset_case
seed svc-f.service enabled active 100
sa02m_svc_capture svc-f.service
sa02m_svc_apply svc-f.service app on norestart
[ -z "$(verbs svc-f.service)" ] && has_log "перезапуск не требуется (norestart)" \
    && ok "norestart: active unit left running, no restart" \
    || bad "norestart violated: '$(verbs svc-f.service)'"

# 1g. apply WITHOUT capture (caller bug) ⇒ WARN + no widening (falls through as existing)
reset_case
seed svc-g.service disabled inactive
sa02m_svc_apply svc-g.service app on
[ -z "$(verbs svc-g.service)" ] && has_log "состояние до установки не снято" \
    && [ "$SA02M_SVC_LAST_RESULT" = uncaptured ] \
    && ok "uncaptured: WARN + treated as existing, zero widening" \
    || bad "uncaptured: verbs='$(verbs svc-g.service)' LAST_RESULT=$SA02M_SVC_LAST_RESULT"

# 1h. new unit, app/off ⇒ disable + stop
reset_case
seed svc-h.service - inactive
sa02m_svc_capture svc-h.service
sa02m_svc_apply svc-h.service app off
[ "$(verbs svc-h.service)" = "disable stop" ] && has_log "выключен по умолчанию" \
    && ok "app/off new unit: disable + stop" \
    || bad "app/off new unit: got '$(verbs svc-h.service)'"

# 1i. new unit, app/enabled ⇒ enable only (no start)
reset_case
seed svc-i.service - inactive
sa02m_svc_capture svc-i.service
sa02m_svc_apply svc-i.service app enabled
[ "$(verbs svc-i.service)" = "enable" ] && [ "$SA02M_SVC_LAST_RESULT" = enabled ] \
    && ok "app/enabled new unit: enable, no start" \
    || bad "app/enabled new unit: got '$(verbs svc-i.service)'"

# 1j. failed unit ⇒ no start, preserve line names journalctl
reset_case
seed svc-j.service enabled failed
sa02m_svc_capture svc-j.service
sa02m_svc_apply svc-j.service app on
[ -z "$(verbs svc-j.service)" ] && has_log "не запускаю; journalctl -u svc-j.service" \
    && ok "failed unit: not started, journalctl hint logged" \
    || bad "failed unit: verbs='$(verbs svc-j.service)' (log: $LOGCAP)"

# 1k. D-Bus timeout at capture ⇒ zero widening + WARN (never read as new)
reset_case
seed svc-k.service timeout timeout
sa02m_svc_capture svc-k.service
sa02m_svc_apply svc-k.service app on
[ -z "$(verbs svc-k.service)" ] && has_log "systemd не ответил до установки" \
    && [ "$SA02M_SVC_LAST_RESULT" = timeout ] \
    && ok "timeout: zero widening calls + WARN (a wedged D-Bus is never a first install)" \
    || bad "timeout widened: '$(verbs svc-k.service)' LAST_RESULT=$SA02M_SVC_LAST_RESULT"

# 1l. restore-exact: a vendor installer STARTED an operator-stopped unit
# between capture and apply (MPLC's install.sh does enable+start) ⇒ stop back
reset_case
seed svc-l.service disabled inactive
sa02m_svc_capture svc-l.service
seed svc-l.service enabled active     # the vendor run widened it mid-install
sa02m_svc_apply svc-l.service app on
[ "$(verbs svc-l.service)" = "disable stop" ] \
    && has_log "автозапуск снят обратно" && has_log "остановлен обратно" \
    && ok "restore-exact: vendor-widened unit is disabled+stopped back" \
    || bad "restore-exact failed: verbs='$(verbs svc-l.service)' (log: $LOGCAP)"

echo "── 2. third-party stack gate on the first-install default ──"

# 2a. refresh + --stack=NODERED + new unit ⇒ NO enable/start + the refresh INFO
reset_case
seed nodered.service - inactive
sa02m_svc_capture nodered.service
export SA02M_INSTALL_MODE=refresh
sa02m_svc_apply nodered.service app on --stack=NODERED
[ -z "$(verbs nodered.service)" ] && has_log "refresh: nodered.service (сторонний стек NODERED) — автозапуск не включаю" \
    && [ "$SA02M_SVC_LAST_RESULT" = skipped-thirdparty ] \
    && ok "refresh + --stack=NODERED + new unit: no enable/start, refresh INFO" \
    || bad "refresh thirdparty gate failed: '$(verbs nodered.service)' (log: $LOGCAP)"

# 2b. full mode, same call ⇒ enable + start (fresh board unchanged)
reset_case
seed nodered.service - inactive
sa02m_svc_capture nodered.service
sa02m_svc_apply nodered.service app on --stack=NODERED
[ "$(verbs nodered.service)" = "enable start" ] \
    && ok "full mode + --stack: first-install default applies (enable+start)" \
    || bad "full mode + --stack: got '$(verbs nodered.service)'"

# 2c. refresh + --with-optional ⇒ the explicit opt-in overrides the gate
reset_case
seed nodered.service - inactive
sa02m_svc_capture nodered.service
export SA02M_INSTALL_MODE=refresh SA02M_WITH_OPTIONAL=1
sa02m_svc_apply nodered.service app on --stack=NODERED
[ "$(verbs nodered.service)" = "enable start" ] \
    && ok "refresh + --with-optional + --stack: enable+start (conscious opt-in)" \
    || bad "with-optional override failed: '$(verbs nodered.service)'"

echo "── 3. generated (SysV) units ──"

# 3a. generated at capture, native now, S-link present ⇒ enable
reset_case
seed mplc4.service generated active 100
sa02m_svc_capture mplc4.service
printf '%s' enabled > /dev/null  # (noop, clarity)
printf '%s' "static" > "$ST/mplc4.service.enabled.post" 2>/dev/null || true
printf '%s' "linked" > /dev/null
# after the unit-file install the shim must answer a NON-generated word:
printf '%s' disabled > "$ST/mplc4.service.enabled"
: > "$T/rc2.d/S02mplc4"
sa02m_svc_apply mplc4.service app on
case "$(verbs mplc4.service)" in
    "enable restart"|"enable") ok "generated→native with S-link: enable carried over" ;;
    *) bad "generated→native with S-link: got '$(verbs mplc4.service)'" ;;
esac

# 3b. generated, NO S-link ⇒ no enable
reset_case
rm -f "$T/rc2.d/S02mplc4"
seed mplc4.service generated inactive
sa02m_svc_capture mplc4.service
printf '%s' disabled > "$ST/mplc4.service.enabled"
sa02m_svc_apply mplc4.service app on
[ -z "$(verbs mplc4.service)" ] \
    && ok "generated→native without S-link: autostart NOT invented" \
    || bad "generated without S-link widened: '$(verbs mplc4.service)'"

echo "── 4. infra units ──"

# 4a. masked + (thus) disabled ⇒ unmask + enable; start only with the flag
reset_case
seed infra-a.service masked inactive
sa02m_svc_apply infra-a.service infra
[ "$(verbs infra-a.service)" = "unmask enable" ] \
    && ok "infra masked: unmask + enable, NO start without the flag" \
    || bad "infra masked: got '$(verbs infra-a.service)'"

# 4b. same with `start` ⇒ unmask + enable + start
reset_case
seed infra-b.service masked inactive
sa02m_svc_apply infra-b.service infra start
[ "$(verbs infra-b.service)" = "unmask enable start" ] \
    && ok "infra masked + start: unmask + enable + start" \
    || bad "infra masked + start: got '$(verbs infra-b.service)'"

# 4c. `restart` acts only when active
reset_case
seed infra-c.service enabled active
sa02m_svc_apply infra-c.service infra restart
[ "$(verbs infra-c.service)" = "restart" ] \
    && ok "infra restart: active unit restarted" \
    || bad "infra restart active: got '$(verbs infra-c.service)'"
reset_case
seed infra-d.service enabled inactive
sa02m_svc_apply infra-d.service infra restart
[ -z "$(verbs infra-d.service)" ] \
    && ok "infra restart: inactive unit NOT started by a restart request" \
    || bad "infra restart inactive: got '$(verbs infra-d.service)'"

# 4d. already enabled + active ⇒ zero calls (delta principle)
reset_case
seed infra-e.service enabled active
sa02m_svc_apply infra-e.service infra start
[ -z "$(verbs infra-e.service)" ] \
    && ok "infra already enabled+active: zero calls" \
    || bad "infra no-op case made calls: '$(verbs infra-e.service)'"

# 4e. absent infra unit ⇒ nothing, LAST_RESULT=absent
reset_case
sa02m_svc_apply infra-f.service infra start
[ -z "$(verbs infra-f.service)" ] && [ "$SA02M_SVC_LAST_RESULT" = absent ] \
    && ok "absent infra unit: nothing asserted, LAST_RESULT=absent" \
    || bad "absent infra unit: verbs='$(verbs infra-f.service)' LAST_RESULT=$SA02M_SVC_LAST_RESULT"

echo "── 5. daemon-reload insurance ──"

reset_case
seed svc-r.service - inactive
sa02m_svc_capture svc-r.service
printf '%s' yes > "$ST/svc-r.service.needreload"
sa02m_svc_apply svc-r.service app enabled
if grep -q 'systemctl daemon-reload' "$CALLS" \
   && [ "$(grep -c 'systemctl daemon-reload' "$CALLS")" = 1 ]; then
    # daemon-reload must precede the enable
    dr=$(grep -n 'systemctl daemon-reload' "$CALLS" | cut -d: -f1)
    en=$(grep -n 'systemctl enable svc-r' "$CALLS" | cut -d: -f1)
    [ -n "$en" ] && [ "$dr" -lt "$en" ] \
        && ok "NeedDaemonReload=yes: exactly one daemon-reload, before enable" \
        || bad "daemon-reload ordering wrong (reload@$dr enable@$en)"
else
    bad "NeedDaemonReload=yes: daemon-reload count wrong ($(grep -c 'systemctl daemon-reload' "$CALLS"))"
fi

echo "── 6. rootfs build ──"

reset_case
export SA02M_ROOTFS_BUILD=1 SA02M_ROOTFS_ROOT="$T/rootfs"
sa02m_svc_capture svc-s.service
sa02m_svc_apply svc-s.service app on
if grep -q -- "--root=$T/rootfs enable svc-s.service" "$CALLS" \
   && ! grep -qE 'systemctl.*(start|restart) svc-s' "$CALLS"; then
    ok "SA02M_ROOTFS_BUILD: enable via --root only, never start"
else
    bad "rootfs build calls wrong: $(cat "$CALLS")"
fi
unset SA02M_ROOTFS_BUILD SA02M_ROOTFS_ROOT

echo "── 7. package tiers + pip ──"

# 7a. thirdparty tier in refresh ⇒ NO apt-get + the INFO
reset_case
export SA02M_INSTALL_MODE=refresh
touch "$T/online"
sa02m_pkg_install_tier thirdparty docker.io docker-compose
if ! grep -q 'apt-get' "$CALLS" && has_log "refresh: сторонние пакеты не ставятся: docker.io docker-compose"; then
    ok "thirdparty tier in refresh: no apt-get, INFO names the packages"
else
    bad "thirdparty tier in refresh leaked apt ($(grep apt-get "$CALLS" || true); log: $LOGCAP)"
fi

# 7b. thirdparty tier in full ⇒ apt-get install runs
reset_case
sa02m_pkg_install_tier thirdparty docker.io
grep -q 'apt-get.*install.*docker.io' "$CALLS" \
    && ok "thirdparty tier in full mode: apt-get install runs" \
    || bad "thirdparty tier in full mode: apt-get missing ($(cat "$CALLS"))"

# 7c. pip: already importable ⇒ nothing at all
reset_case
echo serial > "$T/py-ok"
sa02m_pip_install serial pyserial
if ! grep -q 'pip3' "$CALLS" && [ -z "$LOGCAP" ]; then
    ok "pip: already-importable module — silent no-op"
else
    bad "pip already-importable case acted: $(cat "$CALLS") / $LOGCAP"
fi

# 7d. pip offline ⇒ WARN, no pip call
reset_case
: > "$T/py-ok"
rm -f "$T/online"
sa02m_pip_install yaml pyyaml
if ! grep -q 'pip3' "$CALLS" && has_log "ОФФЛАЙН: python-модуль yaml не установлен"; then
    ok "pip offline: WARN + no pip"
else
    bad "pip offline: $(cat "$CALLS") / $LOGCAP"
fi

# 7e. pip online ⇒ pip3 install runs (bounded via the timeout shim)
reset_case
touch "$T/online"
sa02m_pip_install yaml pyyaml
grep -q 'pip3 install.*pyyaml' "$CALLS" \
    && ok "pip online: pip3 install runs" \
    || bad "pip online: pip3 not called ($(cat "$CALLS"))"

echo "── 8. stack-policy lib ──"

# 8a. get on a missing file ⇒ absent; invalid ID ⇒ rc 2
reset_case
rm -f "$SA02M_STACKS_CONF"
[ "$(sa02m_stack_policy_get NODERED)" = absent ] \
    && ok "policy get, missing file: absent" \
    || bad "policy get on missing file: $(sa02m_stack_policy_get NODERED)"
sa02m_stack_policy_get FOO >/dev/null 2>&1
[ $? -eq 2 ] && ok "policy get, invalid ID: rc 2" || bad "policy get invalid ID rc wrong"

# 8b. set ⇒ exact file bytes (header + all keys sorted), 0644, no tmp left
reset_case
rm -f "$SA02M_STACKS_CONF"
sa02m_stack_policy_set DOCKER present || bad "policy set rc != 0"
expected="$T/expected.conf"
cat > "$expected" <<'EOF'
# SA-02m third-party stacks policy. Written by install.sh and sa02m-web-service-ctl.sh; hand-editable.
# STACK_<ID>=present|absent|disabled   disabled = removed/refused by the operator: never auto-installed.
STACK_CODESYS=absent
STACK_DOCKER=present
STACK_KLOGIC=absent
STACK_MPLC=absent
STACK_NODERED=absent
EOF
cmp -s "$SA02M_STACKS_CONF" "$expected" \
    && ok "policy set: exact bytes (header + sorted keys)" \
    || bad "policy set bytes differ: $(diff "$expected" "$SA02M_STACKS_CONF" 2>&1 | head -5)"
if ls "$T/etc"/*.tmp.* >/dev/null 2>&1; then bad "policy set left a tmp file"; else ok "policy set: no tmp left"; fi
# 0644 — asserted only where the platform reports POSIX modes faithfully
probe="$T/modeprobe"; touch "$probe"; chmod 0644 "$probe"
if [ "$(stat -c %a "$probe" 2>/dev/null)" = 644 ]; then
    [ "$(stat -c %a "$SA02M_STACKS_CONF")" = 644 ] \
        && ok "policy file mode 0644" || bad "policy file mode: $(stat -c %a "$SA02M_STACKS_CONF")"
else
    ok "policy file mode: skipped (platform does not report POSIX modes)"
fi

# 8c. no-op set keeps mtime honest (bytes unchanged, no rewrite artefacts)
before=$(cat "$SA02M_STACKS_CONF")
sa02m_stack_policy_set DOCKER present
[ "$before" = "$(cat "$SA02M_STACKS_CONF")" ] \
    && ok "policy set: equal value is a no-op" || bad "no-op set rewrote the file"

# 8d. valid foreign keys preserved, unknown lines dropped
reset_case
cat > "$SA02M_STACKS_CONF" <<'EOF'
STACK_MPLC=disabled
STACK_FOO=present
garbage line
EOF
sa02m_stack_policy_set NODERED present
grep -q '^STACK_MPLC=disabled$' "$SA02M_STACKS_CONF" \
    && ok "rewrite preserves a valid key's value (MPLC=disabled)" \
    || bad "rewrite lost STACK_MPLC=disabled"
if ! grep -q 'FOO\|garbage' "$SA02M_STACKS_CONF"; then
    ok "rewrite drops unknown keys and garbage lines"
else
    bad "unknown lines survived the rewrite"
fi

# 8e. invalid ID/value ⇒ rc 2, file untouched
before=$(cat "$SA02M_STACKS_CONF")
sa02m_stack_policy_set FOO present; rc1=$?
sa02m_stack_policy_set MPLC bogus;  rc2=$?
[ "$rc1" -eq 2 ] && [ "$rc2" -eq 2 ] && [ "$before" = "$(cat "$SA02M_STACKS_CONF")" ] \
    && ok "invalid ID/value: rc 2, file untouched" \
    || bad "invalid args: rc1=$rc1 rc2=$rc2, file changed=$([ "$before" != "$(cat "$SA02M_STACKS_CONF")" ] && echo yes || echo no)"

# 8f. ROOTFS build never writes the file
reset_case
rm -f "$SA02M_STACKS_CONF"
export SA02M_ROOTFS_BUILD=1
sa02m_stack_policy_set DOCKER present
sa02m_stack_policy_derive --write >/dev/null
[ ! -f "$SA02M_STACKS_CONF" ] \
    && ok "SA02M_ROOTFS_BUILD: set and derive --write both refuse to write" \
    || bad "rootfs build wrote the policy file"
unset SA02M_ROOTFS_BUILD

# 8g. derive --write creates from live state; a later hand-edit survives
reset_case
rm -f "$SA02M_STACKS_CONF"
mkdir -p "$T/root/opt/mplc4"        # MPLC installed via the probe root
out=$(sa02m_stack_policy_derive --write)
grep -q '^STACK_MPLC=present$' "$SA02M_STACKS_CONF" && grep -q '^STACK_CODESYS=absent$' "$SA02M_STACKS_CONF" \
    && ok "derive --write: file created from live detection (MPLC=present)" \
    || bad "derive --write content wrong: $(cat "$SA02M_STACKS_CONF" 2>/dev/null)"
case "$out" in *"создан по текущему состоянию"*) ok "derive --write: RU log line" ;; *) bad "derive --write log line missing: $out" ;; esac
sed -i 's/^STACK_MPLC=present$/STACK_MPLC=disabled/' "$SA02M_STACKS_CONF"
before=$(cat "$SA02M_STACKS_CONF")
out=$(sa02m_stack_policy_derive --write)
[ "$before" = "$(cat "$SA02M_STACKS_CONF")" ] && [ -z "$out" ] \
    && ok "derive --write: second call never overwrites the operator's disabled" \
    || bad "derive --write overwrote an existing file"

echo "── 9. verdict table (12 cells + --with-optional) ──"

# Fixture control: MPLC installed ⇔ $T/root/opt/mplc4 exists.
verdict_case() {  # verdict_case <policy> <installed yes|no> <mode> <expect>
    local pol=$1 inst=$2 mode=$3 expect=$4 got
    rm -f "$SA02M_STACKS_CONF"; rm -rf "$T/root/opt/mplc4"
    [ "$pol" != none ] && sa02m_stack_policy_set MPLC "$pol"
    [ "$inst" = yes ] && mkdir -p "$T/root/opt/mplc4"
    if [ "$mode" = refresh ]; then SA02M_INSTALL_MODE=refresh; else unset SA02M_INSTALL_MODE 2>/dev/null || true; fi
    got=$(sa02m_stack_verdict MPLC)
    [ "$got" = "$expect" ] \
        && ok "verdict: policy=$pol installed=$inst mode=$mode → $expect" \
        || bad "verdict: policy=$pol installed=$inst mode=$mode → got $got, want $expect"
}
reset_case
unset SA02M_WITH_OPTIONAL 2>/dev/null || true
verdict_case disabled yes full    skip-disabled
verdict_case disabled no  full    skip-disabled
verdict_case disabled yes refresh skip-disabled
verdict_case disabled no  refresh skip-disabled
verdict_case present  yes full    install
verdict_case present  no  full    install
verdict_case present  yes refresh overlay
verdict_case present  no  refresh skip-absent
verdict_case absent   yes full    install
verdict_case absent   no  full    install
verdict_case absent   yes refresh overlay
verdict_case absent   no  refresh skip-absent
export SA02M_WITH_OPTIONAL=1
verdict_case disabled no  refresh install
verdict_case disabled no  full    install
verdict_case absent   no  refresh install
unset SA02M_WITH_OPTIONAL

# invalid ID ⇒ rc 2
sa02m_stack_verdict FOO >/dev/null 2>&1
[ $? -eq 2 ] && ok "verdict: invalid ID rc 2" || bad "verdict invalid ID rc wrong"

echo "── 10. unmask-only helper ──"
reset_case
seed nft.service masked inactive
sa02m_svc_unmask nft.service
[ "$(verbs nft.service)" = "unmask" ] && [ "$SA02M_SVC_LAST_RESULT" = unmasked ] \
    && ok "sa02m_svc_unmask: masked unit unmasked, nothing else" \
    || bad "sa02m_svc_unmask masked: got '$(verbs nft.service)'"
reset_case
seed nft.service enabled active
sa02m_svc_unmask nft.service
[ -z "$(verbs nft.service)" ] \
    && ok "sa02m_svc_unmask: unmasked unit is a no-op (delta rule)" \
    || bad "sa02m_svc_unmask no-op case made calls: '$(verbs nft.service)'"

echo "── 11. kick ──"
reset_case
seed kick.service - inactive
sa02m_svc_kick sa02m-web-update-check.service
if grep -q 'systemctl start sa02m-web-update-check.service' "$CALLS" \
   && ! grep -q 'enable sa02m-web-update-check' "$CALLS"; then
    ok "kick: start only, never enable"
else
    bad "kick calls wrong: $(cat "$CALLS")"
fi

echo "── 12. restart-if-active (frpc dependency reload, never-widen) ──"
# 12a. active unit ⇒ restart (reload the freshly installed dependency)
reset_case
seed ria.service enabled active 100
sa02m_svc_restart_if_active ria.service
[ "$(verbs ria.service)" = "restart" ] && [ "$SA02M_SVC_LAST_RESULT" = restarted ]     && ok "restart-if-active: active unit ⇒ restart only, LAST_RESULT=restarted"     || bad "restart-if-active active: verbs='$(verbs ria.service)' LAST_RESULT=$SA02M_SVC_LAST_RESULT"
# 12b. operator-stopped unit ⇒ NO restart, NO start (the never-widen guarantee)
reset_case
seed ria2.service disabled inactive
sa02m_svc_restart_if_active ria2.service
[ -z "$(verbs ria2.service)" ] && [ "$SA02M_SVC_LAST_RESULT" = left-inactive ]     && ok "restart-if-active: operator-stopped unit ⇒ zero calls (never starts it)"     || bad "restart-if-active WIDENED a stopped unit: verbs='$(verbs ria2.service)' LAST_RESULT=$SA02M_SVC_LAST_RESULT"
# 12c. ROOTFS build ⇒ no calls at all
reset_case
seed ria3.service enabled active 100
SA02M_ROOTFS_BUILD=1 sa02m_svc_restart_if_active ria3.service
[ -z "$(verbs ria3.service)" ]     && ok "restart-if-active: ROOTFS build ⇒ no runtime restart"     || bad "restart-if-active in ROOTFS made calls: '$(verbs ria3.service)'"

echo ""
if [ "$fails" -eq 0 ]; then
    echo "installer-svc-helpers: ALL OK"
    exit 0
fi
echo "installer-svc-helpers: $fails FAILURE(S)"
exit 1
