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
# RT's project identity or the legacy successful-load message. 'LoadConfig()
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
LOCK="$STATEDIR/.deploy.lock"
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

SOURCE="${1:-}"
ZIP=""
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
  if [ -s "$_tmp" ] && chmod 644 "$_tmp" && mv -f "$_tmp" "$STATUS_FILE"; then
    return 0
  fi
  rm -f "$_tmp"
  return 1
}

fail() {  # $1 stage  $2 error_code  $3 error_message
  log "FAIL stage=$1 code=$2 msg=$3"
  write_status "$1" error "$2" "$3"
}

flasher_busy() {
  [ -S "$FLASHER_SOCK" ] || return 1
  # curl недоступен → определить нельзя → fail closed (считаем занятым).
  command -v curl >/dev/null 2>&1 || return 0
  # Миррор flasher_poll_lock_held (sa02m-web-service-ctl.sh, contract
  # docs/contracts/flasher-health.md): НЕаутентифицированный GET /health,
  # читаем poll_locked (аренда COM-порта), БЕЗ токена. `-f` обязателен — без
  # него 401/500 приходят с кодом выхода 0 (fail-open port-lease регрессия).
  if ! _r=$(curl -fsS --max-time 2 --unix-socket "$FLASHER_SOCK" \
       http://localhost/health 2>/dev/null); then
    return 0   # /health не ответил (таймаут/HTTP-ошибка) → fail closed
  fi
  case "$_r" in
    *'"poll_locked":true'*|*'"poll_locked": true'*) return 0 ;;
    *'"poll_locked":false'*|*'"poll_locked": false'*) return 1 ;;
  esac
  return 0   # нет поля poll_locked → fail closed
}

start_mplc4() { timeout "$START_TIMEOUT" "$SVC_CTL" start "$SVC_ID" >>"$LOG" 2>&1; }

stop_mplc4() { timeout "$STOP_TIMEOUT" "$SVC_CTL" stop "$SVC_ID" >>"$LOG" 2>&1; }

runtime_identity() {
  curl --noproxy '*' -fsS --max-time 2 -H 'Content-Type: application/json' \
    --data '{}' http://127.0.0.1:8082/Methods/GetState 2>/dev/null |
    python3 -c 'import json, sys
try:
    response = json.load(sys.stdin)
    if "projectId" in response:
        project = response["projectId"] if isinstance(response["projectId"], str) else ""
        print(json.dumps([project.lower(), response.get("projectVersion"), not project or response.get("code") == 0]))
except (ValueError, AttributeError):
    pass'
}

resume_prior() {
  start_mplc4 || return 1
  [ -n "$PRIOR_STATE" ] || return 0
  local end=$(( $(date +%s) + VERIFY_TIMEOUT ))
  while [ "$(date +%s)" -lt "$end" ]; do
    [ "$(runtime_identity)" = "$PRIOR_STATE" ] && return 0
    sleep 2
  done
  return 1
}

restore() {
  log "restoring prior project"
  # A failed verification may leave a running project holding outputs and files.
  stop_mplc4 || return 1
  rm -rf "$MPLC_CFG_DIR" || return 1
  if [ "$HAD_PRIOR" = "1" ]; then
    tar xzf "$BACKUP_FILE" -C "$SERVER_DIR" 2>>"$LOG" || return 1
  fi
  resume_prior
}

rollback_failure() {
  if restore; then
    fail "$1" "$2" "$3"
  else
    fail restore E_RESTORE "could not restore the previous configuration after $2"
  fi
}

prune_backups() {
  # Keep the newest $BACKUP_KEEP cfg-*.tgz, delete the rest.
  ls -1t "$BACKUP_DIR"/cfg-*.tgz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while IFS= read -r _old; do
    [ -n "$_old" ] && rm -f "$_old" 2>/dev/null || true
  done
}

deploy() (
  trap 'rm -f "$ZIP"' EXIT
PRIOR_STATE=$(runtime_identity)
# ── 3. Stop mplc4 ───────────────────────────────────────────────────────────
write_status stopping "" "" ""
log "stopping $SVC_ID"
if ! stop_mplc4; then
  # Stop failed — the RT is untouched (nothing deleted). Leave it as-is.
  fail stopping E_STOP "could not stop mplc4"
  exit 0
fi

# ── 4. Backup the existing project (if any) ─────────────────────────────────
write_status backup "" "" ""
if [ -d "$MPLC_CFG_DIR" ]; then
  HAD_PRIOR=1
  _ts=$(date '+%Y%m%d-%H%M%S')
  if ! BACKUP_FILE=$(mktemp "$BACKUP_DIR/cfg-$_ts-XXXXXX.tgz") ||
     ! tar czf "$BACKUP_FILE" -C "$SERVER_DIR" cfg 2>>"$LOG" || [ ! -s "$BACKUP_FILE" ]; then
    rm -f "$BACKUP_FILE" 2>/dev/null || true
    # Backup failed — do NOT delete the live project; bring mplc4 back up.
    if resume_prior; then
      fail backup E_BACKUP "could not back up the current project"
    else
      fail restore E_RESTORE "could not resume the previous configuration after E_BACKUP"
    fi
    exit 0
  fi
  chmod 600 "$BACKUP_FILE" 2>/dev/null || true
  log "backed up to $BACKUP_FILE"
  prune_backups
else
  log "no prior project at $MPLC_CFG_DIR (first deploy)"
fi

# ── 5. Delete the existing project (only after a good backup, or when absent) ─
if ! rm -rf "$MPLC_CFG_DIR"; then
  rollback_failure replace E_EXTRACT "could not replace the previous configuration"
  exit 0
fi

# ── 6. Extract the new cfg/ to the HARD-CODED target ────────────────────────
write_status replace "" "" ""
if ! MPLC_LIB="$MPLC_LIB" ZIP="$ZIP" DEST="$MPLC_CFG_DIR" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["MPLC_LIB"])
from lib.project_zip import extract_project
extract_project(os.environ["ZIP"], os.environ["DEST"])   # fixed basenames only
PY
then
  rollback_failure replace E_EXTRACT "could not write the new project"
  exit 0
fi
chmod 755 "$MPLC_CFG_DIR" 2>/dev/null || true
chmod 644 "$MPLC_CFG_DIR"/* 2>/dev/null || true
chown -R root:root "$MPLC_CFG_DIR" 2>/dev/null || true

# ── 7. Start mplc4, then VERIFY the RT actually loaded the project ──────────
PROJECT_VERSION=$(python3 -c 'import json,sys
with open(sys.argv[1], encoding="utf-8-sig") as f:
    edits = json.load(f).get("VersionEditsInfo") or {}
    print(edits.get("ProjectVersion", "") if isinstance(edits, dict) else "")' "$MPLC_CFG_DIR/ProjInfo.json") || {
  rollback_failure replace E_EXTRACT "could not read deployed project metadata"
  exit 0
}
write_status starting "" "" ""
# Record the monitor-log end offset BEFORE start so verify only reads NEW lines.
LOG_OFFSET=0
[ -f "$MONITOR_LOG" ] && LOG_OFFSET=$(wc -c <"$MONITOR_LOG" 2>/dev/null | tr -cd '0-9')
[ -n "$LOG_OFFSET" ] || LOG_OFFSET=0

if ! start_mplc4; then
  rollback_failure starting E_START "could not start mplc4"
  exit 0
fi

write_status verify "" "" ""
_deadline=$(( $(date +%s) + VERIFY_TIMEOUT ))
_verdict=""
_state_seen=0
[ -z "$PRIOR_STATE" ] || _state_seen=1
_legacy_loaded=0
while [ "$(date +%s)" -lt "$_deadline" ]; do
  # GetState describes the running process. A file already extracted on disk
  # cannot confirm that the runtime accepted it.
  _state=$(runtime_identity | python3 -c 'import json, sys
try:
    actual, version, healthy = json.load(sys.stdin)
    same_version = not sys.argv[2] or version is None or str(version) == sys.argv[2]
    print("ok" if healthy and actual == sys.argv[1].lower() and same_version else "different")
except ValueError:
    pass' "$PROJECT_ID" "$PROJECT_VERSION")
  if [ "$_state" = ok ]; then _verdict=ok; break; fi
  if [ "$_state" = different ]; then _state_seen=1; fi
  if [ -f "$MONITOR_LOG" ]; then
    _new=$(tail -c +"$((LOG_OFFSET + 1))" "$MONITOR_LOG" 2>/dev/null)
    case "$_new" in
      *"LoadConfig() error"*|*"Loading default configuration"*) _verdict=fail; break ;;
      *"load successful"*) _legacy_loaded=1 ;;
    esac
  fi
  sleep 2
done
if [ -z "$_verdict" ] && [ "$_state_seen" = 0 ] && [ "$_legacy_loaded" = 1 ]; then
  _verdict=ok
fi

if [ "$_verdict" = "ok" ]; then
  log "load confirmed — deploy success"
  write_status done success "" ""
  exit 0
fi

# Not confirmed (LoadConfig error, default-config fallback, or timeout) — the RT
# did NOT load the project. Restore the prior project and report the failure.
if [ "$_verdict" = "fail" ]; then
  rollback_failure verify E_VERIFY "RT rejected the project (LoadConfig error)"
else
  rollback_failure verify E_VERIFY "load not confirmed within timeout"
fi
exit 0
)

# --submit accepts a previously validated archive on stdin from the CGI.
# Preserve the original synchronous invocation for tools staging a file.
mkdir -p "$STATEDIR" "$BACKUP_DIR" || exit 1
exec 9>"$LOCK" || exit 1
if ! flock -n 9; then
  printf '%s\n' '{"ok":false,"error":"busy","error_code":"E_LOCK"}'
  exit 0
fi
ZIP=$(mktemp "$STATEDIR/.project.XXXXXXXX") || exit 1
trap 'rm -f "$ZIP"' EXIT
if ! PROJECT_JSON=$(python3 /dev/fd/3 "$SOURCE" "$ZIP" "$INCOMING" "$MPLC_LIB" 3<<'PYTHON'
import json
import os
import stat
import sys

source, destination, incoming, library = sys.argv[1:]
sys.path.insert(0, library)
from lib.project_zip import validate_zip

if source == "--submit":
    stream = sys.stdin.buffer
else:
    if os.path.dirname(source) != incoming:
        sys.exit("expected a staged archive")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        sys.exit("expected a regular archive")
    stream = os.fdopen(fd, "rb")
with stream:
    data = stream.read(5242881)
if len(data) > 5242880:
    sys.exit("archive too large")
with open(destination, "wb") as f:
    f.write(data)
print(json.dumps(validate_zip(destination), ensure_ascii=False))
PYTHON
); then
  PROJECT_JSON=null
  fail validate E_MEMBERS "not a valid MasterSCADA export"
  cat "$STATUS_FILE"
  exit 0
fi
PROJECT_ID=$(printf '%s' "$PROJECT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])') || exit 1
if flasher_busy; then
  fail validate E_BUSY "flasher holds the RS-485 port lease"
  cat "$STATUS_FILE"
  exit 0
fi
write_status validate "" "" "" || exit 1
if [ "$SOURCE" = --submit ]; then
  (trap '' HUP; deploy) </dev/null >>"$LOG" 2>&1 &
  trap - EXIT
  printf '%s\n' '{"ok":true,"pending":true,"stage":"validate"}'
else
  deploy
  if grep -q '"result": "success"' "$STATUS_FILE"; then rm -f -- "$SOURCE"; fi
fi
