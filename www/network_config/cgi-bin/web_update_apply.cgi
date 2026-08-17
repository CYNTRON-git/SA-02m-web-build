#!/bin/bash
# Web update apply / status (legacy GitHub OTA + offline file transaction).
# GET: transaction fields + legacy.status/log for status.js (plan §2.10 / §6.1).
# POST without confirm_version: legacy sa02m-web-update-apply (BC for status.js; no CSRF).
# POST with confirm_version: CSRF + write transaction → systemctl start sa02m-update.service.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

LEGACY_STATEDIR=/var/lib/sa02m-web-build
LEGACY_LOCKFILE="$LEGACY_STATEDIR/update.lock"
LEGACY_STATUS_FILE="$LEGACY_STATEDIR/update_status"
LEGACY_LOGFILE="$LEGACY_STATEDIR/update.log"

STATEDIR=/var/lib/sa02m-update
PACKAGE="$STATEDIR/incoming/package.sa02m"
CGI_LOCK="$STATEDIR/incoming/.cgi.lock"

METHOD="${REQUEST_METHOD:-GET}"

_json_headers() {
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-cache\r\n\r\n'
}

web_session_check_cookie || {
  _json_headers
  printf '{"error":"unauthorized","ok":false}\n'
  exit 0
}

_legacy_log_tail() {
  if [ -f "$LEGACY_LOGFILE" ]; then
    tail -40 "$LEGACY_LOGFILE" 2>/dev/null | \
      sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n",$0}'
  fi
}

_legacy_running() {
  if [ -f "$LEGACY_LOCKFILE" ]; then
    local pid
    pid=$(tr -d ' \r\n' < "$LEGACY_LOCKFILE" 2>/dev/null)
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

_emit_status() {
  python3 - "$STATEDIR" "$LEGACY_STATEDIR" <<'PY'
import json, sys
from pathlib import Path

statedir = Path(sys.argv[1])
legacy_dir = Path(sys.argv[2])
sys.path.insert(0, "/opt/sa02m-update")

txn = None
try:
    from lib import transaction as txnmod
    txn = txnmod.load(statedir)
except Exception:
    p = statedir / "transaction.json"
    if p.is_file():
        try:
            txn = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            txn = None

stage = (txn or {}).get("stage") or "idle"
result = (txn or {}).get("result") or "pending"
running_stages = {
    "uploaded", "validating", "backing_up", "applying",
    "verifying", "committing", "rolling_back",
    "confirmed", "wipe", "apply", "verify",
}
if stage in running_stages:
    legacy_status = "running"
elif stage == "done":
    legacy_status = "done"
elif stage in ("error", "rolled_back"):
    legacy_status = "error"
elif stage == "cancelled":
    legacy_status = "idle"
else:
    legacy_status = "idle"

lock = legacy_dir / "update.lock"
status_file = legacy_dir / "update_status"
log_file = legacy_dir / "update.log"
legacy_running = False
if lock.is_file():
    try:
        pid = lock.read_text(encoding="utf-8", errors="replace").strip()
        if pid.isdigit() and Path(f"/proc/{pid}").exists():
            legacy_running = True
    except Exception:
        pass
if legacy_running:
    legacy_status = "running"
elif status_file.is_file() and not txn:
    try:
        legacy_status = status_file.read_text(encoding="utf-8", errors="replace").strip() or "idle"
    except Exception:
        pass

prefer_new = stage in running_stages or stage in ("done", "error", "rolled_back", "cancelled")
log_path = (statedir / "update.log") if prefer_new and (statedir / "update.log").is_file() else log_file
log_tail = ""
if log_path.is_file():
    try:
        log_tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    except Exception:
        log_tail = ""

out = {
    "ok": True,
    "status": legacy_status,
    "log": log_tail,
    "legacy": {"status": legacy_status},
}
if txn:
    out["transaction"] = txn
    out["transaction_id"] = txn.get("id")
    out["stage"] = txn.get("stage")
    out["progress_pct"] = txn.get("progress_pct")
    out["files_total"] = txn.get("files_total")
    out["files_done"] = txn.get("files_done")
    out["error_code"] = txn.get("error_code")
    out["error_message"] = txn.get("error_message")
    out["target_version"] = txn.get("target_version")
    out["source"] = txn.get("source")
    out["operation"] = txn.get("operation")
    out["result"] = txn.get("result")
print(json.dumps(out, ensure_ascii=False))
PY
}

if [ "$METHOD" = "GET" ]; then
  _json_headers
  _emit_status
  exit 0
fi

# ── POST ───────────────────────────────────────────────────────────────────
TMP_BODY=$(mktemp /tmp/sa02m-web-upd-body.XXXXXX)
trap 'rm -f "$TMP_BODY"' EXIT
CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
if [ -n "$CL" ] && [ "$CL" -gt 0 ] 2>/dev/null; then
  if [ "$CL" -gt 65536 ]; then
    _json_headers
    printf '{"ok":false,"error":"body_too_large"}\n'
    exit 0
  fi
  dd bs=1 count="$CL" 2>/dev/null >"$TMP_BODY" || true
fi

CONFIRM=$(python3 - "$TMP_BODY" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
raw = p.read_text(encoding="utf-8", errors="replace").strip() if p.is_file() else ""
if not raw:
    print("")
    raise SystemExit
try:
    d = json.loads(raw)
except Exception:
    print("")
    raise SystemExit
print(str(d.get("confirm_version") or "").strip())
PY
)

if [ -n "$CONFIRM" ]; then
  # Offline file apply — CSRF required (headers not yet sent).
  if ! web_csrf_validate; then
    _json_headers
    printf '{"ok":false,"error":"csrf","error_code":"E_CSRF"}\n'
    exit 0
  fi

  case "$CONFIRM" in
    *[!0-9.]*|'')
      _json_headers
      printf '{"ok":false,"error":"invalid_confirm_version","error_code":"E_COMPAT"}\n'
      exit 0
      ;;
  esac

  if [ ! -f "$PACKAGE" ]; then
    _json_headers
    printf '{"ok":false,"error_code":"E_TAR","error_message":"package.sa02m absent"}\n'
    exit 0
  fi

  mkdir -p "$STATEDIR/incoming" 2>/dev/null || true

  (
    flock -n 9 || {
      printf 'Content-type: application/json; charset=UTF-8\r\n'
      printf 'Cache-Control: no-cache\r\n\r\n'
      printf '{"ok":false,"error_code":"E_LOCK","error_message":"update busy"}\n'
      exit 0
    }

    python3 - "$STATEDIR" "$CONFIRM" "$PACKAGE" <<'PY'
import json, os, subprocess, sys, uuid
from pathlib import Path

statedir = Path(sys.argv[1])
confirm = sys.argv[2]
package = sys.argv[3]
sys.path.insert(0, "/opt/sa02m-update")

try:
    from lib import transaction as txnmod
except Exception as e:
    sys.stdout.write("Content-type: application/json; charset=UTF-8\r\n")
    sys.stdout.write("Cache-Control: no-cache\r\n\r\n")
    sys.stdout.write(json.dumps({"ok": False, "error_code": "E_CMD", "error_message": str(e)}) + "\n")
    sys.exit(0)

installed = None
vf = Path("/var/www/network_config/VERSION")
if vf.is_file():
    for line in vf.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and all(c.isdigit() or c == "." for c in line):
            installed = line
            break

existing = txnmod.load(statedir)
if existing:
    st = str(existing.get("stage") or "")
    if st in {"validating", "backing_up", "applying", "verifying", "committing", "rolling_back"}:
        sys.stdout.write("Content-type: application/json; charset=UTF-8\r\n")
        sys.stdout.write("Cache-Control: no-cache\r\n\r\n")
        sys.stdout.write(json.dumps({
            "ok": False,
            "error_code": "E_LOCK",
            "error_message": f"busy stage={st}",
            "transaction_id": existing.get("id"),
        }, ensure_ascii=False) + "\n")
        sys.exit(0)

txn = txnmod.new_transaction(
    operation="update",
    source="file",
    package_path=package,
    target_version=confirm,
    previous_version=installed,
    signature_ok=True,
)
txn["stage"] = "validating"
txn["id"] = str(uuid.uuid4())

def persist(t):
    try:
        txnmod.save(t, statedir)
        return True
    except OSError:
        data = json.dumps(t, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        r = subprocess.run(
            ["sudo", "-n", "/usr/bin/tee", str(statedir / "transaction.json")],
            input=data.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0

def emit(status_line, obj):
    if status_line:
        sys.stdout.write(status_line + "\r\n")
    sys.stdout.write("Content-type: application/json; charset=UTF-8\r\n")
    sys.stdout.write("Cache-Control: no-cache\r\n\r\n")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")

if not persist(txn):
    emit(None, {"ok": False, "error_code": "E_INTERNAL", "error_message": "cannot write transaction.json"})
    sys.exit(0)

subprocess.run(
    ["sudo", "-n", "/usr/bin/systemctl", "reset-failed", "sa02m-update.service"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
r = subprocess.run(
    ["sudo", "-n", "/usr/bin/systemctl", "start", "sa02m-update.service"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if r.returncode != 0:
    err = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip() or "systemctl start failed"
    txn["stage"] = "error"
    txn["result"] = "failed"
    txn["error_code"] = "E_CMD"
    txn["error_message"] = err
    persist(txn)
    emit(None, {"ok": False, "error_code": "E_CMD", "error_message": err, "transaction_id": txn["id"]})
    sys.exit(0)

emit("Status: 202 Accepted", {
    "ok": True,
    "transaction_id": txn["id"],
    "status": "running",
    "stage": "validating",
    "legacy": {"status": "running"},
})
PY
  ) 9>"$CGI_LOCK"
  exit 0
fi

# ── Legacy GitHub OTA (status.js BC — empty/no confirm_version; no CSRF) ───
_json_headers

if _legacy_running; then
  log_tail=$(_legacy_log_tail)
  log_tail="${log_tail%\\n}"
  printf '{"ok":true,"status":"running","log":"%s","legacy":{"status":"running"}}\n' "$log_tail"
  exit 0
fi

if ! command -v sudo >/dev/null 2>&1; then
  printf '{"ok":false,"status":"error","log":"sudo не найден","error_code":"E_CMD"}\n'
  exit 0
fi

nohup sudo -n /usr/local/sbin/sa02m-web-update-apply >/dev/null 2>&1 &
BGPID=$!
sleep 1

if [ -f "$LEGACY_LOCKFILE" ] || kill -0 "$BGPID" 2>/dev/null; then
  printf '{"ok":true,"status":"running","log":"Обновление запущено...","legacy":{"status":"running"}}\n'
else
  printf 'error' > "$LEGACY_STATUS_FILE" 2>/dev/null || true
  log_tail=$(_legacy_log_tail)
  log_tail="${log_tail%\\n}"
  printf '{"ok":false,"status":"error","log":"%s","legacy":{"status":"error"}}\n' "$log_tail"
fi
