#!/bin/bash
# Privileged MPLC4 project-deploy helper (root, via one pinned www-data sudoers
# line). Runs the reversible deploy state machine for «Обновление проекта MPLC»:
#
#   validate -> stop mplc4 -> backup existing cfg/ -> delete -> extract new cfg/
#            -> start mplc4 -> verify-load ; restore-on-ANY-failure.
#
# Contract: docs/contracts/mplc-project-deploy.md. Fail-closed: the existing
# project is deleted ONLY after a good backup, and ANY failure in replace/start/
# verify restores the backup and restarts mplc4. Success is reported ONLY on the
# RT's 'Configuration was load successful' — a bare start that logs 'LoadConfig()
# error' is a FAILURE (§0 load-verify guard), never a false green.
#
# Usage: sa02m-mplc-project-deploy.sh <validated-staged-zip>
# The zip is re-validated here (defense in depth). The deploy target is HARD-CODED
# below and never derived from the request or the zip entry names (zip-slip).
set -u

# ── Hard-coded constants (never from the request) ───────────────────────────
SERVER_DIR=/opt/mplc4/server
MPLC_CFG_DIR=/opt/mplc4/server/cfg          # THE deploy target — constant
STATEDIR=/var/lib/sa02m-mplc
INCOMING="$STATEDIR/incoming"
BACKUP_DIR="$STATEDIR/backups"
STATUS_FILE="$STATEDIR/status.json"
LOCK="$INCOMING/.deploy.lock"
SVC_CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
MPLC_LIB=/opt/sa02m-mplc
MONITOR_LOG=/var/log/mplc4/monitor/mplc_monitor.log
FLASHER_SOCK=/run/sa02m-flasher/flasher.sock
LOG=/var/log/sa02m_install.log
SVC_ID=mplc4                                 # literal — never from the request

BACKUP_KEEP=5                                # retention cap (embedded ARM disk)
STOP_TIMEOUT=60
START_TIMEOUT=60
VERIFY_TIMEOUT=60

ZIP="${1:-}"
BACKUP_FILE=""
HAD_PRIOR=0
PROJECT_JSON='null'

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') sa02m-mplc-project-deploy: $*" >>"$LOG" 2>&1; }

# Atomic status write (temp + mv), world-readable so the www-data CGI can poll it.
write_status() {
  # $1 stage  $2 result(""|success|error)  $3 error_code  $4 error_message
  _stage="$1"; _result="${2:-}"; _ecode="${3:-}"; _emsg="${4:-}"
  mkdir -p "$STATEDIR" 2>/dev/null || true
  _tmp="$STATUS_FILE.$$"
  STAGE="$_stage" RESULT="$_result" ECODE="$_ecode" EMSG="$_emsg" PROJ="$PROJECT_JSON" \
    python3 - >"$_tmp" 2>/dev/null <<'PY'
import json, os
stage = os.environ["STAGE"]
result = os.environ.get("RESULT") or None
pending = result is None
try:
    proj = json.loads(os.environ.get("PROJ") or "null")
except Exception:
    proj = None
out = {
    "ok": result != "error",
    "pending": pending,
    "stage": stage,
    "result": result,
    "error_code": (os.environ.get("ECODE") or None) if result == "error" else None,
    "error_message": (os.environ.get("EMSG") or None) if result == "error" else None,
    "project": proj,
}
print(json.dumps(out, ensure_ascii=False))
PY
  if [ -s "$_tmp" ]; then
    mv -f "$_tmp" "$STATUS_FILE" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    chmod 644 "$STATUS_FILE" 2>/dev/null || true
  else
    rm -f "$_tmp" 2>/dev/null || true
  fi
}

fail() {  # $1 stage  $2 error_code  $3 error_message
  log "FAIL stage=$1 code=$2 msg=$3"
  write_status "$1" error "$2" "$3"
}

flasher_busy() {
  [ -S "$FLASHER_SOCK" ] || return 1
  command -v curl >/dev/null 2>&1 || return 1
  _r=$(curl -sS --max-time 2 --unix-socket "$FLASHER_SOCK" \
       -H 'Cookie: session_token=cyntron_session' http://localhost/status 2>/dev/null) || return 1
  case "$_r" in *'"busy":true'*|*'"busy": true'*) return 0 ;; esac
  return 1
}

start_mplc4() { timeout "$START_TIMEOUT" "$SVC_CTL" start "$SVC_ID" >>"$LOG" 2>&1; }

# Restore the prior project (or clear a half-written cfg on first deploy) and
# bring mplc4 back up. Best-effort — used on the failure path.
restore() {
  log "restoring prior project"
  rm -rf "$MPLC_CFG_DIR" 2>/dev/null || true
  if [ "$HAD_PRIOR" = "1" ] && [ -n "$BACKUP_FILE" ] && [ -f "$BACKUP_FILE" ]; then
    tar xzf "$BACKUP_FILE" -C "$SERVER_DIR" 2>>"$LOG" || log "restore tar failed"
  fi
  start_mplc4 || log "restart after restore failed"
}

prune_backups() {
  # Keep the newest $BACKUP_KEEP cfg-*.tgz, delete the rest.
  ls -1t "$BACKUP_DIR"/cfg-*.tgz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while IFS= read -r _old; do
    [ -n "$_old" ] && rm -f "$_old" 2>/dev/null || true
  done
}

# ── Single-flight: hold the lock for the whole run ──────────────────────────
mkdir -p "$INCOMING" "$BACKUP_DIR" "$STATEDIR" 2>/dev/null || true
exec 9>"$LOCK" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    log "another deploy is running — abort"
    exit 0
  fi
fi

# ── 1. Validate the staged zip (defense in depth) ───────────────────────────
write_status validate "" "" ""
if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
  fail validate E_UPLOAD "staged zip missing"
  exit 0
fi
case "$ZIP" in
  "$INCOMING"/*) ;;                       # must live in our incoming dir
  *) fail validate E_UPLOAD "staged zip outside incoming"; exit 0 ;;
esac

PROJECT_JSON=$(
  MPLC_LIB="$MPLC_LIB" ZIP="$ZIP" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["MPLC_LIB"])
try:
    from lib import ProjectZipError
    from lib.project_zip import validate_zip
    meta = validate_zip(os.environ["ZIP"])   # verify_hashes off (module docs)
    print(json.dumps(meta, ensure_ascii=False))
except Exception:
    print("null")
PY
)
if [ "$PROJECT_JSON" = "null" ] || [ -z "$PROJECT_JSON" ]; then
  PROJECT_JSON='null'
  fail validate E_MEMBERS "not a valid MasterSCADA export"
  exit 0
fi

# ── 2. Refuse if the RS-485 flasher holds the COM lease ─────────────────────
if flasher_busy; then
  fail validate E_BUSY "flasher holds the RS-485 port lease"
  exit 0
fi

# ── 3. Stop mplc4 ───────────────────────────────────────────────────────────
write_status stopping "" "" ""
log "stopping $SVC_ID"
if ! timeout "$STOP_TIMEOUT" "$SVC_CTL" stop "$SVC_ID" >>"$LOG" 2>&1; then
  # Stop failed — the RT is untouched (nothing deleted). Leave it as-is.
  fail stopping E_STOP "could not stop mplc4"
  exit 0
fi

# ── 4. Backup the existing project (if any) ─────────────────────────────────
write_status backup "" "" ""
if [ -d "$MPLC_CFG_DIR" ] && [ -n "$(ls -A "$MPLC_CFG_DIR" 2>/dev/null)" ]; then
  HAD_PRIOR=1
  _ts=$(date '+%Y%m%d-%H%M%S')
  BACKUP_FILE="$BACKUP_DIR/cfg-$_ts.tgz"
  if ! tar czf "$BACKUP_FILE" -C "$SERVER_DIR" cfg 2>>"$LOG" || [ ! -s "$BACKUP_FILE" ]; then
    rm -f "$BACKUP_FILE" 2>/dev/null || true
    # Backup failed — do NOT delete the live project; bring mplc4 back up.
    start_mplc4 || log "restart after backup-fail failed"
    fail backup E_BACKUP "could not back up the current project"
    exit 0
  fi
  chmod 600 "$BACKUP_FILE" 2>/dev/null || true
  log "backed up to $BACKUP_FILE"
  prune_backups
else
  log "no prior project at $MPLC_CFG_DIR (first deploy)"
fi

# ── 5. Delete the existing project (only after a good backup, or when absent) ─
rm -rf "$MPLC_CFG_DIR" 2>/dev/null || true

# ── 6. Extract the new cfg/ to the HARD-CODED target ────────────────────────
write_status replace "" "" ""
if ! MPLC_LIB="$MPLC_LIB" ZIP="$ZIP" DEST="$MPLC_CFG_DIR" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["MPLC_LIB"])
from lib.project_zip import extract_project
extract_project(os.environ["ZIP"], os.environ["DEST"])   # fixed basenames only
PY
then
  restore
  fail replace E_EXTRACT "could not write the new project"
  exit 0
fi
chmod 755 "$MPLC_CFG_DIR" 2>/dev/null || true
chmod 644 "$MPLC_CFG_DIR"/* 2>/dev/null || true
chown -R root:root "$MPLC_CFG_DIR" 2>/dev/null || true

# ── 7. Start mplc4, then VERIFY the RT actually loaded the project ──────────
write_status starting "" "" ""
# Record the monitor-log end offset BEFORE start so verify only reads NEW lines.
LOG_OFFSET=0
[ -f "$MONITOR_LOG" ] && LOG_OFFSET=$(wc -c <"$MONITOR_LOG" 2>/dev/null | tr -cd '0-9')
[ -n "$LOG_OFFSET" ] || LOG_OFFSET=0

if ! start_mplc4; then
  restore
  fail starting E_START "could not start mplc4"
  exit 0
fi

write_status verify "" "" ""
_deadline=$(( $(date +%s) + VERIFY_TIMEOUT ))
_verdict=""
while [ "$(date +%s)" -lt "$_deadline" ]; do
  if [ -f "$MONITOR_LOG" ]; then
    _new=$(tail -c +"$((LOG_OFFSET + 1))" "$MONITOR_LOG" 2>/dev/null)
    case "$_new" in
      *"load successful"*|*"was load successful"*) _verdict="ok"; break ;;
      *"LoadConfig() error"*|*"Loading default configuration"*) _verdict="fail"; break ;;
    esac
  fi
  sleep 2
done

if [ "$_verdict" = "ok" ]; then
  log "load confirmed — deploy success"
  write_status done success "" ""
  rm -f "$ZIP" 2>/dev/null || true
  exit 0
fi

# Not confirmed (LoadConfig error, default-config fallback, or timeout) — the RT
# did NOT load the project. Restore the prior project and report the failure.
restore
if [ "$_verdict" = "fail" ]; then
  fail verify E_VERIFY "RT rejected the project (LoadConfig error)"
else
  fail verify E_VERIFY "load not confirmed within timeout"
fi
exit 0
