#!/bin/bash
# Config-only factory reset CGI (plan §4.6).
# GET  → transaction status (operation=factory_reset)
# POST → requires session + CSRF + JSON {"confirm_phrase":"SA02M-RESET","backup_ok":true}
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
. "$(dirname "$0")/lib_web_json.sh"

STATEDIR=/var/lib/sa02m-update
TXN_JSON="$STATEDIR/transaction.json"
LOCKFILE="$STATEDIR/update.lock"
CONFIRM_PHRASE="SA02M-RESET"
METHOD="${REQUEST_METHOD:-GET}"

printf 'Content-type: application/json; charset=UTF-8\r\n'
printf 'Cache-Control: no-store\r\n\r\n'

web_session_check_cookie || {
  printf '{"ok":false,"error":"unauthorized","error_code":"E_AUTH"}\n'
  exit 0
}

# CSRF: prefer shared helper (added with offline-update CSRF work); else session .csrf file.
web_factory_csrf_check() {
  if declare -F web_csrf_check >/dev/null 2>&1; then
    web_csrf_check
    return $?
  fi
  if declare -F web_csrf_validate >/dev/null 2>&1; then
    web_csrf_validate
    return $?
  fi
  local tok hash expected got
  tok=$(web_session__cookie_token) || return 1
  hash=$(web_session__hash "$tok") || return 1
  [ -r "$SA02M_SESSION_DIR/$hash.csrf" ] || return 1
  expected=$(tr -d '\r\n' <"$SA02M_SESSION_DIR/$hash.csrf" 2>/dev/null) || return 1
  got="${HTTP_X_SA02M_CSRF:-}"
  [ -n "$expected" ] && [ -n "$got" ] && [ "$expected" = "$got" ]
}

json_out() {
  printf '%s\n' "$1"
}

if [ "$METHOD" = "GET" ]; then
  # Status only — never starts reset
  python3 - <<'PY'
import json, os, sys
path = "/var/lib/sa02m-update/transaction.json"
lock = "/var/lib/sa02m-update/update.lock"
txn = {"schema_version": 1, "operation": None, "stage": "idle", "result": None}
if os.path.isfile(path):
    try:
        with open(path, encoding="utf-8") as f:
            txn = json.load(f)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_code": "E_INTERNAL", "error_message": str(exc)}))
        sys.exit(0)
running = False
if os.path.isfile(lock):
    try:
        pid = open(lock, encoding="utf-8").read().strip()
        if pid.isdigit() and os.path.isdir(f"/proc/{pid}"):
            running = True
    except OSError:
        pass
out = {
    "ok": True,
    "operation": txn.get("operation"),
    "stage": txn.get("stage", "idle"),
    "result": txn.get("result"),
    "progress_pct": txn.get("progress_pct", 0),
    "error_code": txn.get("error_code"),
    "error_message": txn.get("error_message"),
    "backup_path": txn.get("backup_path"),
    "defaults_bundle": txn.get("defaults_bundle"),
    "wipe_manifest": txn.get("wipe_manifest"),
    "preserve_manifest": txn.get("preserve_manifest"),
    "running": running and txn.get("operation") == "factory_reset",
    "transaction_id": txn.get("id"),
}
print(json.dumps(out, ensure_ascii=False))
PY
  exit 0
fi

if [ "$METHOD" != "POST" ]; then
  json_out '{"ok":false,"error_code":"E_CMD","error_message":"method not allowed"}'
  exit 0
fi

web_factory_csrf_check || {
  json_out '{"ok":false,"error_code":"E_CSRF","error_message":"CSRF validation failed"}'
  exit 0
}

# Read JSON body (bounded)
CL="${CONTENT_LENGTH:-0}"
case "$CL" in
  ''|*[!0-9]*) CL=0 ;;
esac
if [ "$CL" -gt 4096 ]; then
  json_out '{"ok":false,"error_code":"E_CMD","error_message":"body too large"}'
  exit 0
fi
BODY=""
if [ "$CL" -gt 0 ]; then
  BODY=$(dd bs=1 count="$CL" 2>/dev/null || true)
fi

PARSE=$(BODY="$BODY" CONFIRM_PHRASE="$CONFIRM_PHRASE" python3 - <<'PY'
import json, os, sys
raw = os.environ.get("BODY", "")
need = os.environ.get("CONFIRM_PHRASE", "SA02M-RESET")
try:
    obj = json.loads(raw) if raw.strip() else {}
except Exception:
    print("E_CMD\tinvalid JSON")
    sys.exit(0)
if not isinstance(obj, dict):
    print("E_CMD\tbody must be object")
    sys.exit(0)
phrase = obj.get("confirm_phrase")
backup_ok = obj.get("backup_ok")
if phrase != need:
    print("E_CANCEL\tconfirm_phrase must be exactly SA02M-RESET")
    sys.exit(0)
if backup_ok is not True:
    print("E_CANCEL\tbackup_ok must be true (mandatory backup)")
    sys.exit(0)
print("OK")
PY
)

case "$PARSE" in
  OK) ;;
  *)
    code=${PARSE%%$'\t'*}
    msg=${PARSE#*$'\t'}
    python3 -c 'import json,sys; print(json.dumps({"ok":False,"error_code":sys.argv[1],"error_message":sys.argv[2]},ensure_ascii=False))' \
      "$code" "$msg"
    exit 0
    ;;
esac

# Refuse if lock held
if [ -f "$LOCKFILE" ]; then
  pid=$(tr -d ' \r\n' <"$LOCKFILE" 2>/dev/null || true)
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    json_out '{"ok":false,"error_code":"E_LOCK","error_message":"update/reset already running"}'
    exit 0
  fi
fi

mkdir -p "$STATEDIR" 2>/dev/null || true

# Write transaction then start oneshot service (no args)
TXN_ID=$(python3 - <<'PY'
import json, os, tempfile, time, uuid
statedir = "/var/lib/sa02m-update"
path = statedir + "/transaction.json"
os.makedirs(statedir, exist_ok=True)
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
txn = {
    "schema_version": 1,
    "id": str(uuid.uuid4()),
    "operation": "factory_reset",
    "source": "web",
    "package_path": None,
    "target_version": None,
    "target_commit": None,
    "previous_version": None,
    "stage": "confirmed",
    "progress_pct": 0,
    "files_total": 0,
    "files_done": 0,
    "result": "pending",
    "error_code": None,
    "error_message": None,
    "rollback_archive": None,
    "imaging_lock": False,
    "signature_ok": True,
    "confirm_phrase_ok": True,
    "backup_ok": True,
    "started_at": now,
    "updated_at": now,
    "finished_at": None,
}
data = json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
fd, tmp = tempfile.mkstemp(prefix=".txn.", dir=statedir)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(data)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, path)
print(txn["id"])
PY
)

if [ -z "$TXN_ID" ]; then
  json_out '{"ok":false,"error_code":"E_INTERNAL","error_message":"failed to write transaction"}'
  exit 0
fi

if ! command -v sudo >/dev/null 2>&1; then
  json_out '{"ok":false,"error_code":"E_CMD","error_message":"sudo not found"}'
  exit 0
fi

if sudo -n /usr/bin/systemctl start sa02m-factory-reset.service >/dev/null 2>&1; then
  python3 -c 'import json,sys; print(json.dumps({"ok":True,"transaction_id":sys.argv[1],"stage":"confirmed"},ensure_ascii=False))' \
    "$TXN_ID"
  exit 0
fi

# Fallback when unit is not installed yet: start pinned runner under sudo.
if [ -x /usr/local/libexec/sa02m-factory-reset-runner ]; then
  nohup sudo -n /usr/local/libexec/sa02m-factory-reset-runner run >/dev/null 2>&1 &
  python3 -c 'import json,sys; print(json.dumps({"ok":True,"transaction_id":sys.argv[1],"stage":"confirmed","warning":"started runner directly"},ensure_ascii=False))' \
    "$TXN_ID"
  exit 0
fi

json_out '{"ok":false,"error_code":"E_CMD","error_message":"failed to start sa02m-factory-reset.service"}'
