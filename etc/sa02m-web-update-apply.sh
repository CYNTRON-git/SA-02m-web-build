#!/bin/bash
# Fetch the fixed GitHub source under the root download unit.
# A capable shared runner is mandatory; legacy boards require full refresh.
# Output → $LOGFILE; status → $STATEDIR/update_status (running/done/error).
set -uo pipefail

LIB=/usr/local/lib/sa02m-web-build-lib.sh
[ -f "$LIB" ] && . "$LIB"

# Both state dirs env-overridable ONLY for the harness
# (scripts/dev/test-web-update-launcher-guard.sh) — the same two names the
# CGI and the runner honour. sudo's env_reset strips them on the board.
STATEDIR="${SA02M_WEB_BUILD_STATEDIR:-/var/lib/sa02m-web-build}"
UPDATE_STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
LOGFILE="$STATEDIR/update.log"
LOCKFILE="$STATEDIR/update.lock"
STATUS_FILE="$STATEDIR/update_status"
REPO_URL="${SA02M_WEB_BUILD_REPO_URL:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
BRANCH="${SA02M_WEB_BUILD_BRANCH:-main}"
if type resolve_web_build_branch >/dev/null 2>&1; then
    BRANCH="$(resolve_web_build_branch)"
fi
WEB_ROOT=/var/www/network_config
RUNNER="${SA02M_UPDATE_RUNNER:-/usr/local/libexec/sa02m-update-runner}"
# Fixed HTTPS allowlist — no arbitrary URL from UI (plan §2.9).
REPO_ALLOWLIST="${SA02M_WEB_BUILD_REPO_ALLOWLIST:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
COMMIT_PIN="${SA02M_WEB_BUILD_COMMIT_PIN:-}"
LS_REMOTE_CACHE="$UPDATE_STATEDIR/state/github_ls_remote.cache"
LS_REMOTE_MAX_AGE_SEC="${SA02M_WEB_BUILD_LS_REMOTE_MAX_AGE_SEC:-3600}"

mkdir -p "$STATEDIR"
chmod 755 "$STATEDIR"

log() { local ts; ts=$(date '+%Y-%m-%d %H:%M:%S'); printf '%s %s\n' "$ts" "$*" | tee -a "$LOGFILE"; }

normalize_repo_url() {
    local u=$1
    u="${u%.git}"
    u="${u%/}"
    printf '%s' "$u"
}

repo_url_allowed() {
    local want have n_want n_have
    want=$(normalize_repo_url "$1")
    IFS=',' read -r -a _allow <<< "$REPO_ALLOWLIST"
    for have in "${_allow[@]}"; do
        have=$(echo "$have" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -n "$have" ] || continue
        n_have=$(normalize_repo_url "$have")
        n_want=$want
        if [ "$n_want" = "$n_have" ] || [ "$n_want.git" = "$n_have" ] || [ "$n_want" = "$n_have.git" ]; then
            return 0
        fi
        # Also accept exact configured string (with .git).
        if [ "$1" = "$have" ]; then
            return 0
        fi
    done
    return 1
}

# Serialize direct invocations as well as systemd starts. Never unlink this inode.
exec 7>>"$STATEDIR/launch.lock"
flock -n 7 || exit 1

# Lock
if [ -f "$LOCKFILE" ]; then
    pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "ERROR: обновление уже выполняется (pid $pid)"
        exit 1
    fi
    rm -f "$LOCKFILE"
fi
printf '%s' "$$" > "$LOCKFILE"
printf 'running' > "$STATUS_FILE"
chmod 644 "$LOCKFILE" "$STATUS_FILE" 2>/dev/null || true

# Until the clone dir exists the EXIT trap releases the lock and nothing else.
# The dir-removing trap is armed only AFTER this launcher's own mktemp, on a
# private name: an inherited TMPDIR (a manual root run — sudo's env_reset
# strips it on the panel path) is the caller's directory, never ours to remove.
# Harness: scripts/dev/test-web-update-launcher-guard.sh L12.
trap 'rm -f "$LOCKFILE"' EXIT
CLONE_TMP=""
UPDATE_LOCK_HELD=0
# The controller reserved the journal (validating, download_pending) before it
# started this unit; a failed run closes that reservation with the code the
# failing step names, never leaves it to age into E_RECOVERY_REQUIRED.
FAIL_CODE=E_DOWNLOAD
FAIL_MESSAGE="download failed; see update log"
cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ] && [ "$UPDATE_LOCK_HELD" = 1 ] && [ -f "$UPDATE_STATEDIR/transaction.json" ]; then
        FAIL_CODE="$FAIL_CODE" FAIL_MESSAGE="$FAIL_MESSAGE" python3 - "$UPDATE_STATEDIR/transaction.json" <<'PYFAIL'
import json,sys,os,time
from pathlib import Path
p=Path(sys.argv[1])
try:
    d=json.loads(p.read_text())
    if d.get("download_pending") and d.get("source") == "github":
        d.update(stage="error", result="failed", error_code=os.environ.get("FAIL_CODE") or "E_DOWNLOAD",
                 error_message=os.environ.get("FAIL_MESSAGE") or "download failed; see update log", finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()))
        tmp=p.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(d,f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,p)
        dfd=os.open(p.parent,os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
except Exception:
    pass
PYFAIL
    fi
    rm -f "$LOCKFILE"
    [ -n "$CLONE_TMP" ] && rm -rf "$CLONE_TMP" 2>/dev/null
    return 0
}

trap cleanup EXIT

> "$LOGFILE"
log "START: $REPO_URL ветка $BRANCH"
printf 'running' > "$STATUS_FILE"

if ! repo_url_allowed "$REPO_URL"; then
    log "ERROR: REPO_URL not in allowlist: $REPO_URL"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

# ── Never clobber a RUNNING transaction (1.0.6.52 F5b; widened 1.0.6.62, audit 2026-09-28 M1) ─
# A second «Применить» while a runner works used to clone again and hand a NEW
# transaction.json over the live one (handoff_to_shared_runner). Two guards:
# HERE, before the clone, on every non-terminal stage with a live runner
# (until 1.0.6.62 only applying|verifying|committing|rolling_back — a runner at
# validating/backing_up, 10–30 s on the A7, was not protected); and AGAIN in
# handoff_to_shared_runner, under the runner's own lock, right before the
# write — the clone takes 30–60 s and a runner that became alive meanwhile is
# invisible to this one. A transaction whose runner is gone is left alone —
# re-applying is a legitimate recovery, and so is the reboot (recover →
# verify). Same liveness test as the CGI side
# (www/network_config/cgi-bin/lib_web_update.sh) — a second copy by necessity:
# a root helper must not source a www-data-writable file. Keep the two in
# step, except for sa02m-update-download.service: the CGI side counts it (the
# online runner lives inside it), this launcher IS it and would refuse
# itself; a manual run beside the unit is refused by launch.lock above.
# Harness: scripts/dev/test-web-update-launcher-guard.sh.
update_runner_alive() {
    local lock="$UPDATE_STATEDIR/update.lock" pid cmd units
    if [ -r "$lock" ]; then
        pid=$(tr -d ' \r\n' < "$lock" 2>/dev/null)
        if [[ "$pid" =~ ^[0-9]+$ ]] && [ -r "/proc/$pid/cmdline" ]; then
            cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
            case "$cmd" in
                *sa02m-update-runner*|*/sa02m-update/runner/*) return 0 ;;
            esac
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        # Both oneshot: `activating` is their running state (is-active would say rc 3).
        # Not sa02m-update-download.service — see the header note above.
        local u st
        for u in sa02m-update.service sa02m-update-verify.service; do
            st=$(timeout 5 systemctl show -p ActiveState --value "$u" 2>/dev/null) || st=""
            case "${st%%[[:space:]]*}" in active|activating|reloading) return 0 ;; esac
        done
        units=$(timeout 5 systemctl list-units --plain --no-legend 'sa02m-update-apply-*.service' 2>/dev/null) || units=""
        case "$units" in *" running"*) return 0 ;; esac
    fi
    return 1
}
# Every stage the runner still owns (the non-terminal set of
# opt/sa02m-update/lib/transaction.py) — the one home for both guards.
TXN_BUSY_STAGES="uploaded validating backing_up applying verifying committing rolling_back"
read_txn() {  # → txn_stage txn_id (empty when there is no readable transaction)
    txn_stage=""; txn_id=""
    if [ -r "$UPDATE_STATEDIR/transaction.json" ]; then
        read -r txn_stage txn_id < <(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1],encoding="utf-8")); print(str(d.get("stage") or "-"), str(d.get("id") or "-"))
except Exception:
    pass' "$UPDATE_STATEDIR/transaction.json" 2>/dev/null | tr -d '\r') || true
    fi
}
# refuse_if_running WHERE — a transaction at a busy stage whose runner is alive
# → the «уже выполняется» line, update_status=error, exit 1; the EXIT trap
# removes the legacy lock (and, before the handoff narrows it, the clone).
refuse_if_running() {
    read_txn
    case " $TXN_BUSY_STAGES " in
        *" $txn_stage "*)
            if update_runner_alive; then
                log "ERROR: обновление уже выполняется (txn ${txn_id:-?} stage=$txn_stage; $1) — повторный запуск отклонён"
                printf 'error' > "$STATUS_FILE"
                exit 1
            fi
            if ! python3 - "$UPDATE_STATEDIR/transaction.json" <<'RESERVATION'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if d.get("source")=="github" and d.get("download_pending") is True else 1)
RESERVATION
            then
                log "ERROR [E_RECOVERY_REQUIRED]: unfinished transaction $txn_id at $txn_stage; recover before retry"
                printf 'error' > "$STATUS_FILE"
                exit 1
            fi
            ;;
    esac
}
refuse_if_running "до клонирования"

# Hold the runner lock throughout download: upload cannot replace a pending transaction.
mkdir -p "$UPDATE_STATEDIR"
exec 9>>"$UPDATE_STATEDIR/update.lock"
flock -w 15 9 || { log "ERROR [E_LOCK]: update busy"; exit 1; }
UPDATE_LOCK_HELD=1

# Refused only now, under the lock: the EXIT trap (cleanup) closes the
# controller's reservation with the bootstrap code instead of leaving it
# at validating.
if [ ! -x "$RUNNER" ] || ! ( "$RUNNER" capabilities | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("control_v1") and d.get("port_maintenance_v1") else 1)' ); then
    log "ERROR [E_BOOTSTRAP_REQUIRED]: run install.sh --refresh from a verified checkout before updating"
    FAIL_CODE=E_BOOTSTRAP_REQUIRED
    FAIL_MESSAGE="update runner missing or incapable; run install.sh --refresh"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

# Temporary directory for clone
if ! CLONE_TMP=$(mktemp -d /tmp/sa02m-web-update-XXXXXX) || [ -z "$CLONE_TMP" ]; then
    CLONE_TMP=""
    log "ERROR: mktemp для клона не удался"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi
trap cleanup EXIT
chmod 755 "$CLONE_TMP"

log "Клонирование репозитория..."
if ! git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 \
    clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$CLONE_TMP/repo" 2>&1 | tee -a "$LOGFILE"; then
    log "ERROR: git clone не удался"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

# Trust gate after clone (plan §2.9)
remote_url=$(git -C "$CLONE_TMP/repo" remote get-url origin 2>/dev/null || echo "")
if ! repo_url_allowed "$remote_url"; then
    log "ERROR: cloned remote URL not in allowlist: $remote_url"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

NEW_COMMIT=$(git -C "$CLONE_TMP/repo" rev-parse HEAD 2>/dev/null || echo "")
if [ -z "$NEW_COMMIT" ]; then
    log "ERROR: cannot resolve cloned HEAD"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

if [ -n "$COMMIT_PIN" ]; then
    if [ "$NEW_COMMIT" != "$COMMIT_PIN" ]; then
        log "ERROR: commit pin mismatch (have $NEW_COMMIT want $COMMIT_PIN)"
        printf 'error' > "$STATUS_FILE"
        exit 1
    fi
    log "Commit pin OK: $NEW_COMMIT"
else
    mkdir -p "$UPDATE_STATEDIR/state" 2>/dev/null || true
    now_ts=$(date +%s)
    cache_ok=0
    if [ -f "$LS_REMOTE_CACHE" ]; then
        cache_ts=$(awk 'NR==1{print $1}' "$LS_REMOTE_CACHE" 2>/dev/null || echo 0)
        cache_sha=$(awk 'NR==1{print $2}' "$LS_REMOTE_CACHE" 2>/dev/null || echo "")
        if [ -n "$cache_ts" ] && [ -n "$cache_sha" ] && \
            [ $((now_ts - cache_ts)) -le "$LS_REMOTE_MAX_AGE_SEC" ]; then
            if [ "$NEW_COMMIT" = "$cache_sha" ]; then
                cache_ok=1
                log "ls-remote cache hit: $cache_sha"
            fi
        fi
    fi
    if [ "$cache_ok" != 1 ]; then
        remote_sha=$(git ls-remote --heads "$REPO_URL" "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1; exit}')
        if [ -n "$remote_sha" ]; then
            printf '%s %s\n' "$now_ts" "$remote_sha" >"$LS_REMOTE_CACHE"
            chmod 644 "$LS_REMOTE_CACHE" 2>/dev/null || true
            if [ "$NEW_COMMIT" != "$remote_sha" ]; then
                # depth=1 clone of a moving branch can race; accept clone HEAD but warn
                log "WARN: clone HEAD $NEW_COMMIT != ls-remote $remote_sha (race or shallow); continuing with clone"
            else
                log "ls-remote OK: $remote_sha"
            fi
        else
            log "WARN: git ls-remote failed — trusting allowlisted clone HEAD $NEW_COMMIT"
        fi
    fi
fi

# Hand off only to the capable installed runner. Full refresh owns bootstrap.
handoff_to_shared_runner() {
    [ -x "$RUNNER" ] || return 1
    # Recheck capabilities before reserving the staged overlay.
    if ! "$RUNNER" capabilities | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("control_v1") and d.get("port_maintenance_v1") else 1)' 2>/dev/null; then
        log "ERROR [E_BOOTSTRAP_REQUIRED]: runner capabilities changed; full refresh required"
        return 1
    fi

    local prev_ver=""
    if [ -f "$WEB_ROOT/VERSION" ]; then
        prev_ver=$(tr -d '\r' <"$WEB_ROOT/VERSION" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
    fi
    local target_ver=""
    if [ -f "$CLONE_TMP/repo/www/network_config/VERSION" ]; then
        target_ver=$(tr -d '\r' <"$CLONE_TMP/repo/www/network_config/VERSION" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
    fi

    mkdir -p "$UPDATE_STATEDIR" "$UPDATE_STATEDIR/state" "$UPDATE_STATEDIR/incoming"

    # ── The runner's own lock, taken here and inherited through `exec` (1.0.6.62) ─
    # Same descriptor and mode as the runner's try_lock (fd 9, opened for APPEND
    # so a contender never truncates the holder's pid line): a live runner holds
    # it, so this launcher cannot write transaction.json over its transaction —
    # by construction, not by the stage list above. fd 9 is not close-on-exec,
    # so the lock rides into the runner. Its try_lock reuses this open file
    # description; it never closes and reopens a held descriptor. The recheck
    # below protects the journal immediately before atomic replacement.
    if ! flock -n 9; then
        read_txn
        log "ERROR: обновление уже выполняется (лок $UPDATE_STATEDIR/update.lock занят; txn ${txn_id:-?} stage=${txn_stage:-?}) — повторный запуск отклонён"
        printf 'error' > "$STATUS_FILE"
        exit 1
    fi
    refuse_if_running "после клонирования, под локом раннера"

    # Keep clone for runner overlay (cleanup trap must not remove it after handoff).
    export SA02M_UPDATE_GITHUB_OVERLAY="$CLONE_TMP/repo"
    local txn_id
    txn_id=$(
        UPDATE_STATEDIR="$UPDATE_STATEDIR" OVERLAY="$CLONE_TMP/repo" \
        TARGET_COMMIT="$NEW_COMMIT" TARGET_VER="${target_ver:-}" PREV_VER="${prev_ver:-}" \
        REPO_URL="$REPO_URL" BRANCH="$BRANCH" \
        python3 - <<'PY'
import json, os, tempfile, time, uuid
from pathlib import Path

statedir = Path(os.environ["UPDATE_STATEDIR"])
statedir.mkdir(parents=True, exist_ok=True)
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
txn = {
    "schema_version": 1,
    "id": str(uuid.uuid4()),
    "operation": "update",
    "source": "github",
    "package_path": "",
    "overlay_path": os.environ["OVERLAY"],
    "repo_url": os.environ.get("REPO_URL", ""),
    "branch": os.environ.get("BRANCH", ""),
    "target_version": os.environ.get("TARGET_VER") or None,
    "target_commit": os.environ.get("TARGET_COMMIT") or None,
    "previous_version": os.environ.get("PREV_VER") or None,
    "stage": "validating",
    "progress_pct": 0,
    "files_total": 0,
    "files_done": 0,
    "result": "pending",
    "error_code": None,
    "error_message": None,
    "rollback_archive": None,
    "imaging_lock": False,
    "signature_ok": False,
    "started_at": now,
    "updated_at": now,
    "finished_at": None,
}
path = statedir / "transaction.json"
if path.is_file():
    old = json.loads(path.read_text(encoding="utf-8"))
    if old.get("download_pending") and old.get("source") == "github":
        txn["id"] = old["id"]
        txn["started_at"] = old["started_at"]
fd, tmp = tempfile.mkstemp(prefix=".txn-", dir=statedir)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        out.flush()
        os.fchmod(out.fileno(), 0o644)
        os.fsync(out.fileno())
    os.replace(tmp, path)
    dfd = os.open(statedir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
print(txn["id"])
PY
    ) || { log "ERROR: cannot reserve downloaded overlay"; return 1; }
    trap 'rm -f "$LOCKFILE"' EXIT

    if [ -z "$txn_id" ]; then
        log "ERROR: failed to write github transaction.json"
        return 1
    fi
    log "Handoff to shared runner txn=$txn_id overlay=$CLONE_TMP/repo"
    # Do not delete clone on EXIT — runner owns overlay_path.
    CLONE_TMP=""
    rm -f "$LOCKFILE"
    printf 'running' > "$STATUS_FILE"
    exec "$RUNNER" apply
}

if handoff_to_shared_runner; then
    : # exec never returns
fi
log "ERROR [E_BOOTSTRAP_REQUIRED]: run install.sh --refresh from a verified checkout before updating"
printf 'error' > "$STATUS_FILE"
exit 1
