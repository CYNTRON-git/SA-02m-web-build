#!/bin/bash
# Cancel offline update while stage is uploaded|validating|backing_up (plan §2.10).
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

STATEDIR=/var/lib/sa02m-update
TXN="$STATEDIR/transaction.json"
CGI_LOCK="$STATEDIR/incoming/.cgi.lock"

_json_headers() {
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n\r\n'
}

web_session_check_cookie || {
  _json_headers
  printf '{"ok":false,"error":"unauthorized"}\n'
  exit 0
}

if [ "${REQUEST_METHOD:-GET}" != "POST" ]; then
  _json_headers
  printf '{"ok":false,"error":"method_not_allowed"}\n'
  exit 0
fi

web_csrf_require

_json_headers

mkdir -p "$STATEDIR/incoming" 2>/dev/null || true

(
  flock -n 9 || {
    printf '{"ok":false,"error_code":"E_LOCK","error_message":"update busy"}\n'
    exit 0
  }

  python3 - "$STATEDIR" <<'PY'
import json, os, subprocess, sys, tempfile
from pathlib import Path

statedir = Path(sys.argv[1])
sys.path.insert(0, "/opt/sa02m-update")
try:
    from lib import transaction as txnmod
except Exception as e:
    print(json.dumps({"ok": False, "error_code": "E_CMD", "error_message": f"transaction module: {e}"}))
    sys.exit(0)

txn = txnmod.load(statedir)
if not txn:
    print(json.dumps({"ok": False, "error_code": "E_CANCEL", "error_message": "no transaction"}))
    sys.exit(0)

stage = str(txn.get("stage") or "")
allowed = {"uploaded", "validating", "backing_up"}
if stage not in allowed:
    print(json.dumps({
        "ok": False,
        "error_code": "E_CANCEL",
        "error_message": f"cancel not allowed at stage={stage}",
        "stage": stage,
    }, ensure_ascii=False))
    sys.exit(0)

txn["stage"] = "cancelled"
txn["result"] = "cancelled"
txn["error_code"] = "E_CANCEL"
txn["error_message"] = "cancelled by user"
txn["finished_at"] = txnmod._iso_now()

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

if not persist(txn):
    print(json.dumps({"ok": False, "error_code": "E_INTERNAL", "error_message": "cannot write transaction.json"}))
    sys.exit(0)

# Stop oneshot apply if running (real sudo; fails closed if sudoers missing).
subprocess.run(
    ["sudo", "-n", "/usr/bin/systemctl", "stop", "sa02m-update.service"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)

# Wipe incoming package on cancel (www-data owns incoming).
for name in ("package.sa02m", "package.partial"):
    try:
        (statedir / "incoming" / name).unlink()
    except OSError:
        pass

print(json.dumps({
    "ok": True,
    "stage": "cancelled",
    "transaction_id": txn.get("id"),
}, ensure_ascii=False))
PY
) 9>"$CGI_LOCK"

exit 0
