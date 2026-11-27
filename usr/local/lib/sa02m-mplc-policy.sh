#!/bin/sh
# Shared MPLC timeout delivery and effective PID1 validation. The authored
# values live only in opt/sa02m-mplc/systemd/mplc4.service; a vendor rewrite must not erase
# their derived drop-in. Callers supply their existing atomic-install primitive
# and bounded systemctl function as sa02m_mplc_atomic / sa02m_mplc_sc.

sa02m_mplc_policy_install() {
    SA02M_MPLC_POLICY_CHANGED=0
    _mp_source=$1
    [ -f "$_mp_source" ] || return 1
    _mp_dropin=/etc/systemd/system/mplc4.service.d/sa02m-timeout.conf
    _mp_tmp=$(mktemp) || return 1
    if ! awk '
        BEGIN { print "[Service]" }
        /^\[/ { service=($0 == "[Service]") }
        service && /^(TimeoutStartSec|TimeoutStopSec|TimeoutStartFailureMode|TimeoutStopFailureMode|KillMode)=/ {
            split($0, parts, "="); if (seen[parts[1]]++ || parts[2] == "") bad=1;
            print; count++
        }
        END { if (bad || count != 5) exit 1 }
    ' "$_mp_source" >"$_mp_tmp"; then
        rm -f "$_mp_tmp"
        return 1
    fi
    _mp_rc=0
    if ! cmp -s "$_mp_tmp" "$_mp_dropin"; then
        sa02m_mplc_atomic "$_mp_tmp" "$_mp_dropin" 0644 || _mp_rc=$?
        SA02M_MPLC_POLICY_CHANGED=1
    fi
    rm -f "$_mp_tmp"
    return "$_mp_rc"
}

sa02m_mplc_wrapper_install() {
    # Native vendor systemd is the fresh-install path: no init.d script is
    # manufactured and no non-existent launcher replaces its real ExecStart.
    if [ -x /etc/init.d/mplc4 ]; then
        if ! cmp -s "$1" /etc/systemd/system/mplc4.service; then
            sa02m_mplc_atomic "$1" /etc/systemd/system/mplc4.service 0644 || return 1
            SA02M_MPLC_POLICY_CHANGED=1
        fi
    elif [ ! -e /etc/systemd/system/mplc4.service ]; then
        sa02m_mplc_atomic "$2" /etc/systemd/system/mplc4.service 0644 || return 1
        SA02M_MPLC_POLICY_CHANGED=1
    fi
    return 0
}

sa02m_mplc_policy_observe() {
    _mp_props=$(sa02m_mplc_sc show --all --property=Id,ActiveState,Job,Result,TimeoutStartUSec,TimeoutStopUSec,UnitFileState,Type,KillMode,TimeoutStartFailureMode,TimeoutStopFailureMode,ExecStart,ExecStop,WorkingDirectory,RemainAfterExit,Restart,FragmentPath,ControlGroup,MainPID,ControlPID mplc4.service) || return 1
    printf '%s\n' "$_mp_props" | python3 -I -c '
import re
import sys
from pathlib import Path
data = {}
for line in sys.stdin.read().splitlines():
    key, sep, value = line.partition("=")
    if not sep or key in data:
        sys.exit(1)
    data[key] = value
# systemctl omits empty Exec* arrays even with --all; native vendor units
# intentionally have no ExecStop. All scalar evidence remains mandatory.
data.setdefault("ExecStop", "")
required = {"Id", "ActiveState", "Job", "Result", "TimeoutStartUSec", "TimeoutStopUSec", "UnitFileState",
            "Type", "KillMode", "TimeoutStartFailureMode", "TimeoutStopFailureMode", "ExecStart",
            "ExecStop", "WorkingDirectory", "RemainAfterExit", "Restart", "FragmentPath", "ControlGroup", "MainPID", "ControlPID"}
if set(data) != required or data["Id"] != "mplc4.service":
    sys.exit(1)
if (data["TimeoutStartUSec"] != "45s" or data["TimeoutStopUSec"] != "15s" or
        data["KillMode"] != "mixed" or data["TimeoutStartFailureMode"] != "terminate" or
        data["TimeoutStopFailureMode"] != "terminate" or
        data["FragmentPath"] != "/etc/systemd/system/mplc4.service"):
    sys.exit(2)
def command(field, path, argv):
    values = re.findall(r"path=([^;]+) ; argv\[\]=(.*?) ; ignore_errors=(yes|no)", data[field])
    return values == [(path, argv, "no")]
legacy = (data["Type"] == "oneshot" and data["RemainAfterExit"] == "yes" and data["Restart"] == "no"
          and command("ExecStart", "/etc/init.d/mplc4", "/etc/init.d/mplc4 start")
          and command("ExecStop", "/etc/init.d/mplc4", "/etc/init.d/mplc4 stop"))
native = (data["Type"] == "forking" and data["RemainAfterExit"] == "no" and data["Restart"] == "always"
          and data["WorkingDirectory"] == "/opt/mplc4" and not data["ExecStop"]
          and command("ExecStart", "/opt/mplc4/start_mplc4.sh", "/opt/mplc4/start_mplc4.sh"))
if not (legacy or native):
    sys.exit(2)
active, job, policy = data["ActiveState"], data["Job"], data["UnitFileState"]
terminal = job in {"", "0"} and active in {"active", "inactive", "failed"}
if terminal and active in {"inactive", "failed"}:
    if data["MainPID"] != "0" or data["ControlPID"] != "0":
        sys.exit(3)
    group = data["ControlGroup"]
    if group:
        if group != "/system.slice/mplc4.service":
            sys.exit(3)
        # KillMode=mixed can leave a descendant briefly after the leader.
        # Only PID1 terminal state plus an empty cgroup proves stopped.
        path = Path("/sys/fs/cgroup" + group)
        if path.exists() and any(p.read_text().strip() for p in path.rglob("cgroup.procs")):
            sys.exit(3)
print("|".join(["terminal" if terminal else "changing", active, policy, data["Result"]]))'
}

sa02m_mplc_clock() {
    read -r _mplc_uptime _mplc_idle </proc/uptime || return 1
    _mplc_now=${_mplc_uptime%%.*}
    case "$_mplc_now" in ''|*[!0-9]*) return 1 ;; esac
}

sa02m_mplc_budget_begin() {
    command -v timeout >/dev/null 2>&1 || return 1
    sa02m_mplc_clock || return 1
    if [ "${SA02M_CACHE_JOIN:-}" = 1 ] && [ -n "${SA02M_MPLC_DEADLINE:-}" ]; then
        case "${SA02M_MPLC_DEADLINE:-}" in ''|*[!0-9]*) return 1 ;; esac
        MPLC_DEADLINE=$SA02M_MPLC_DEADLINE
        [ "$MPLC_DEADLINE" -le "$((_mplc_now + 85))" ] || return 1
    else
        MPLC_DEADLINE=$((_mplc_now + 85))
    fi
    [ "$MPLC_DEADLINE" -gt "$_mplc_now" ] || return 1
    SA02M_MPLC_DEADLINE=$MPLC_DEADLINE
    export SA02M_MPLC_DEADLINE
}

sa02m_mplc_bounded() {
    _mplc_bound=$1
    shift
    sa02m_mplc_clock || return 1
    _mplc_left=$((MPLC_DEADLINE - _mplc_now))
    [ "$_mplc_left" -gt 1 ] || return 124
    # Leave the SIGKILL margin inside the one absolute helper deadline.
    [ "$_mplc_bound" -lt "$_mplc_left" ] || _mplc_bound=$((_mplc_left - 1))
    timeout -k 1 "$_mplc_bound" "$@"
}

sa02m_mplc_wait_terminal() {
    while :; do
        _mplc_observation=$(sa02m_mplc_policy_observe) || return 1
        _mplc_status=${_mplc_observation%|*}
        _mplc_result=${_mplc_observation##*|}
        if [ "${1:-}" = start ]; then
            case "$_mplc_result" in success|'') ;; *) return 2 ;; esac
        fi
        case "$_mplc_status" in terminal\|*) return 0 ;; esac
        sa02m_mplc_clock || return 1
        _mplc_reserve=2
        [ "${1:-}" != start ] || _mplc_reserve=20
        [ "$((MPLC_DEADLINE - _mplc_now))" -gt "$_mplc_reserve" ] || return 1
        sleep 1
    done
}

sa02m_mplc_cancel_start() {
    # An explicit stop cancels native Restart=always without changing its
    # steady-state restart/autostart policy or dispatching a second start.
    sa02m_mplc_sc stop mplc4.service >/dev/null 2>&1 || true
    sa02m_mplc_wait_terminal stop || return 1
    case "$_mplc_status" in
        terminal\|inactive\|*|terminal\|failed\|*) return 0 ;;
        *) return 1 ;;
    esac
}

sa02m_mplc_runtime_active() {
    # Align with status.cgi fast_service_state: comm names first, then vendor
    # binary patterns. Never trust systemctl ActiveState alone — mplc4.service
    # is Type=oneshot RemainAfterExit=yes and stays "active (exited)" with no
    # runtime process (dashboard then shows «<1м»).
    pgrep -x mplc >/dev/null 2>&1 \
        || pgrep -x mplc4 >/dev/null 2>&1 \
        || pgrep -x mplc_monitor >/dev/null 2>&1 \
        || pgrep -f '[m]plc.*\.bin\|[M]asterPLC\|[m]asterplc' >/dev/null 2>&1
}

