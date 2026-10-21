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
  json_out "{\"ok\":false,\"error_code\":\"E_CSRF\",\"reason\":\"$(web_csrf_fail_reason)\",\"error_message\":\"CSRF validation failed\"}"
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

# The fixed root controller serializes reset with OTA and owns the journal. Its
# stdout IS the answer (structured JSON, ok:false on a refusal) - never a second
# launcher. A refused sudo (an old sudoers file) prints nothing: the headers are
# already out, so the body gets the same bootstrap shape lib_web_update_control.sh
# answers, instead of an empty body the panel cannot parse.
OUT=$(sudo -n /usr/local/libexec/sa02m-update-control factory-reset SA02M-RESET true) || true
if [ -z "$OUT" ]; then
  OUT='{"ok":false,"status":"error","error_code":"E_CMD","error_message":"update controller unavailable; run install.sh --refresh"}'
fi
json_out "$OUT"
