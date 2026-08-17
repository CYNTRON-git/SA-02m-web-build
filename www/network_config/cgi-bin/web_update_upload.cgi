#!/bin/bash
# Offline .sa02m upload + inspect (plan §2.8 / §2.10).
# POST multipart field "file" → upload_receive → sudo inspect.
# GET → inspect existing package.sa02m (404 JSON if absent).
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

STATEDIR=/var/lib/sa02m-update
PACKAGE="$STATEDIR/incoming/package.sa02m"
INSPECT=/usr/local/libexec/sa02m-update-inspect
UPLOAD_LIB=/opt/sa02m-update

_json_headers() {
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n\r\n'
}

_unauth() {
  _json_headers
  printf '{"ok":false,"error":"unauthorized"}\n'
  exit 0
}

web_session_check_cookie || _unauth

METHOD="${REQUEST_METHOD:-GET}"

_run_inspect() {
  if [ ! -x "$INSPECT" ] && [ ! -f "$INSPECT" ]; then
    printf '%s' '{"ok":false,"error_code":"E_CMD","error_message":"sa02m-update-inspect missing"}'
    return 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    printf '%s' '{"ok":false,"error_code":"E_CMD","error_message":"sudo not found"}'
    return 1
  fi
  # Real sudo — no fake success if sudoers/unit missing.
  sudo -n "$INSPECT" "$PACKAGE" 2>/dev/null
}

_normalize_inspect_json() {
  # Map inspect helper JSON into plan §2.10 inspect object + top-level fields.
  python3 -c '
import json,sys
raw=sys.stdin.read()
try:
    d=json.loads(raw) if raw.strip() else {}
except Exception:
    print(json.dumps({"ok":False,"error_code":"E_INTERNAL","error_message":"inspect JSON parse failed"}))
    sys.exit(0)
insp=d.get("inspect") if isinstance(d.get("inspect"), dict) else {}
# Full validator returns flat fields; bootstrap inspect nests under "inspect".
version=insp.get("version", d.get("version"))
commit=insp.get("commit", d.get("commit"))
sig=insp.get("signature_ok", d.get("signature_ok"))
compat=insp.get("compatible", d.get("compatible"))
warn=insp.get("warnings", d.get("warnings") or [])
if not isinstance(warn, list):
    warn=[]
out={
  "ok": bool(d.get("ok")),
  "size": d.get("package_size", d.get("size")),
  "package_sha256": d.get("package_sha256"),
  "error_code": d.get("error_code"),
  "error_message": d.get("error_message") or d.get("error"),
  "inspect":{
    "version": version,
    "commit": commit,
    "signature_ok": bool(sig) if sig is not None else False,
    "compatible": bool(compat) if compat is not None else False,
    "warnings": warn,
  },
}
print(json.dumps(out, ensure_ascii=False))
'
}

_mark_uploaded() {
  # Best-effort transaction stage=uploaded (www-data may lack STATEDIR write —
  # then sudo tee via python tempfile).
  python3 - "$STATEDIR" <<'PY' 2>/dev/null || true
import json, os, subprocess, sys, tempfile
from pathlib import Path
statedir = Path(sys.argv[1])
sys.path.insert(0, "/opt/sa02m-update")
try:
    from lib import transaction as txnmod
except Exception:
    sys.exit(0)
txn = txnmod.load(statedir) or txnmod.new_transaction(source="file")
txn["stage"] = "uploaded"
txn["source"] = "file"
txn["package_path"] = str(statedir / "incoming" / "package.sa02m")
txn["result"] = "pending"
txn["error_code"] = None
txn["error_message"] = None
txn["finished_at"] = None
try:
    txnmod.save(txn, statedir)
    sys.exit(0)
except OSError:
    pass
data = json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
fd, tmp = tempfile.mkstemp(prefix="sa02m-txn.")
try:
    os.write(fd, data.encode("utf-8"))
    os.close(fd)
    fd = -1
    r = subprocess.run(
        ["sudo", "-n", "/usr/bin/tee", str(statedir / "transaction.json")],
        input=data.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    sys.exit(0 if r.returncode == 0 else 1)
finally:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.unlink(tmp)
    except OSError:
        pass
PY
}

if [ "$METHOD" = "GET" ]; then
  if [ ! -f "$PACKAGE" ]; then
    printf 'Status: 404 Not Found\r\n'
    _json_headers
    printf '{"ok":false,"error":"not_found","error_code":"E_TAR","error_message":"package.sa02m absent"}\n'
    exit 0
  fi
  _json_headers
  raw=$(_run_inspect) || true
  if [ -z "$raw" ]; then
    printf '{"ok":false,"error_code":"E_CMD","error_message":"inspect failed (sudo/helper)"}\n'
    exit 0
  fi
  printf '%s\n' "$raw" | _normalize_inspect_json
  exit 0
fi

if [ "$METHOD" != "POST" ]; then
  _json_headers
  printf '{"ok":false,"error":"method_not_allowed"}\n'
  exit 0
fi

web_csrf_require

_json_headers

if [ ! -d "$UPLOAD_LIB/lib" ] || [ ! -f "$UPLOAD_LIB/lib/upload_receive.py" ]; then
  printf '{"ok":false,"error_code":"E_CMD","error_message":"upload_receive module missing"}\n'
  exit 0
fi

# Stream multipart from CGI stdin into package.sa02m (www-data → incoming/).
UPLOAD_JSON=$(
  CONTENT_TYPE="${CONTENT_TYPE:-}" CONTENT_LENGTH="${CONTENT_LENGTH:-}" \
  python3 - <<'PY'
import json, os, sys
sys.path.insert(0, "/opt/sa02m-update")
from lib.upload_receive import UploadError, receive_multipart_file
try:
    result = receive_multipart_file(os.environ)
    print(json.dumps(result, ensure_ascii=False))
except UploadError as e:
    print(json.dumps({"ok": False, "error_code": e.code, "error_message": e.message}, ensure_ascii=False))
    sys.exit(1)
except Exception as e:
    print(json.dumps({"ok": False, "error_code": "E_INTERNAL", "error_message": str(e)}, ensure_ascii=False))
    sys.exit(1)
PY
)
UP_RC=$?

if [ "$UP_RC" -ne 0 ] || [ -z "$UPLOAD_JSON" ]; then
  if [ -n "$UPLOAD_JSON" ]; then
    printf '%s\n' "$UPLOAD_JSON"
  else
    printf '{"ok":false,"error_code":"E_UPLOAD","error_message":"upload failed"}\n'
  fi
  exit 0
fi

ok=$(printf '%s' "$UPLOAD_JSON" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("ok") else "0")' 2>/dev/null || echo 0)
if [ "$ok" != "1" ]; then
  printf '%s\n' "$UPLOAD_JSON"
  exit 0
fi

_mark_uploaded

raw=$(_run_inspect) || true
if [ -z "$raw" ]; then
  # Upload succeeded but inspect/sudo failed — honest partial result.
  printf '%s\n' "$UPLOAD_JSON" | python3 -c '
import json,sys
u=json.load(sys.stdin)
print(json.dumps({
  "ok": False,
  "size": u.get("size"),
  "package_sha256": u.get("package_sha256"),
  "error_code": "E_CMD",
  "error_message": "upload ok; inspect failed (sudo/helper)",
  "inspect": {"version": None, "commit": None, "signature_ok": False, "compatible": False, "warnings": ["inspect_failed"]},
}, ensure_ascii=False))
'
  exit 0
fi

printf '%s\n' "$raw" | python3 -c '
import json,sys
d=json.loads(sys.stdin.read() or "{}")
insp=d.get("inspect") if isinstance(d.get("inspect"), dict) else {}
version=insp.get("version", d.get("version"))
commit=insp.get("commit", d.get("commit"))
sig=insp.get("signature_ok", d.get("signature_ok"))
compat=insp.get("compatible", d.get("compatible"))
warn=insp.get("warnings", d.get("warnings") or [])
if not isinstance(warn, list):
    warn=[]
size=d.get("package_size", d.get("size"))
sha=d.get("package_sha256")
out={
  "ok": bool(d.get("ok")),
  "size": size,
  "package_sha256": sha,
  "inspect": {
    "version": version,
    "commit": commit,
    "signature_ok": bool(sig) if sig is not None else False,
    "compatible": bool(compat) if compat is not None else False,
    "warnings": warn,
  },
}
if d.get("error_code"):
    out["error_code"]=d.get("error_code")
    out["error_message"]=d.get("error_message")
print(json.dumps(out, ensure_ascii=False))
'
exit 0
