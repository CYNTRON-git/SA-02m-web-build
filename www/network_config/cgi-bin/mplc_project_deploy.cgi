#!/bin/bash
# «Обновление проекта MPLC» — upload a MasterSCADA4D project-export .zip and
# deploy it to the running MPLC4 RT (stop -> backup -> replace -> start -> verify).
# Contract: docs/contracts/mplc-project-deploy.md.
#
#   GET             -> currently deployed project meta ({deployed, project{…}}).
#   GET ?result=1   -> async deploy status JSON (pending until the helper writes
#                      a terminal result — mirrors services_ctrl.cgi).
#   POST (multipart) -> auth + CSRF -> receive+cap -> validate (zip-slip/allow-list/
#                      size caps in Python zipfile) -> refuse if flasher busy or a
#                      deploy is locked -> launch the privileged helper -> {pending}.
#
# Security floors (web-code-rigor.md ## Bash CGI floors): auth+CSRF BEFORE any
# mutation; the zip is handled in Python, never shelled out; the deploy target
# is HARD-CODED in the helper, never taken from the request/zip; the body is
# capped; all root work is one pinned `sudo -n` of the helper. Transport stays
# HTTP 200 with ok:false (project idiom).
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

STATEDIR=/var/lib/sa02m-mplc
INCOMING="$STATEDIR/incoming"
STAGED_ZIP="$INCOMING/project.zip"
UPLOAD_BODY="$INCOMING/upload.bin"
STATUS_FILE="$STATEDIR/status.json"
CGI_LOCK="$INCOMING/.deploy.lock"
HELPER=/usr/local/sbin/sa02m-mplc-project-deploy.sh
SVC_CTL=/usr/local/sbin/sa02m-web-service-ctl.sh
MPLC_LIB=/opt/sa02m-mplc
CFG_DIR=/opt/mplc4/server/cfg
MAX_BODY=5242880   # 5 MiB — the real export is ~14 KB

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

# ── GET ?result=1 — async deploy status ─────────────────────────────────────
if [ "$METHOD" = "GET" ] && printf '%s' "${QUERY_STRING:-}" | grep -q 'result=1'; then
  _json_headers
  if [ -s "$STATUS_FILE" ]; then
    cat "$STATUS_FILE"
  else
    printf '{"ok":true,"pending":false,"stage":"idle"}\n'
  fi
  exit 0
fi

# ── GET — currently deployed project meta (read cfg/ProjInfo.json directly) ──
if [ "$METHOD" = "GET" ]; then
  _json_headers
  CFG_DIR="$CFG_DIR" python3 - <<'PY' 2>/dev/null || printf '{"ok":true,"deployed":false}\n'
import json, os
from pathlib import Path
cfg = Path(os.environ["CFG_DIR"])
pj = cfg / "ProjInfo.json"
if not pj.is_file():
    print(json.dumps({"ok": True, "deployed": False}))
    raise SystemExit
try:
    d = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
except Exception:
    print(json.dumps({"ok": True, "deployed": True, "project": None}))
    raise SystemExit
vi = d.get("VersionEditsInfo") or {}
print(json.dumps({
    "ok": True,
    "deployed": True,
    "project": {
        "name": d.get("ProjectName"),
        "id": d.get("ProjectId"),
        "ide_version": (vi.get("IDEVersion") if isinstance(vi, dict) else None),
    },
}, ensure_ascii=False))
PY
  exit 0
fi

if [ "$METHOD" != "POST" ]; then
  _json_headers
  printf '{"ok":false,"error":"method_not_allowed"}\n'
  exit 0
fi

# ── POST — CSRF BEFORE any work (mutating endpoint) ─────────────────────────
web_csrf_require

_json_headers

if [ ! -f "$HELPER" ]; then
  printf '{"ok":false,"error_code":"E_CMD","error_message":"deploy helper missing"}\n'
  exit 0
fi

# Body-size cap on CONTENT_LENGTH before reading a byte (anti-DoS).
CL=$(printf '%s' "${CONTENT_LENGTH:-}" | tr -cd '0-9')
if [ -z "$CL" ] || [ "$CL" -lt 1 ] 2>/dev/null; then
  printf '{"ok":false,"error_code":"E_UPLOAD","error_message":"empty body"}\n'
  exit 0
fi
if [ "$CL" -gt "$MAX_BODY" ] 2>/dev/null; then
  printf '{"ok":false,"error_code":"E_SIZE","error_message":"body too large"}\n'
  exit 0
fi

mkdir -p "$INCOMING" 2>/dev/null || true
rm -f "$STAGED_ZIP" "$UPLOAD_BODY" 2>/dev/null || true

# Receive the multipart body (bounded), extract the file field, and VALIDATE the
# zip in Python — all read-only, no device mutation yet. Fails closed.
VALIDATE_JSON=$(
  CONTENT_TYPE="${CONTENT_TYPE:-}" MAX_BODY="$MAX_BODY" STAGED_ZIP="$STAGED_ZIP" \
  MPLC_LIB="$MPLC_LIB" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["MPLC_LIB"])
try:
    from lib import ProjectZipError
    from lib.project_zip import parse_multipart_file, validate_zip
except Exception as e:  # module not installed
    print(json.dumps({"ok": False, "error_code": "E_CMD",
                      "error_message": "project_zip module missing: %s" % e}))
    sys.exit(0)

max_body = int(os.environ.get("MAX_BODY") or "5242880")
ctype = os.environ.get("CONTENT_TYPE", "")
staged = os.environ["STAGED_ZIP"]
try:
    body = sys.stdin.buffer.read(max_body + 1)
    if len(body) > max_body:
        raise ProjectZipError("E_SIZE", "body exceeds cap")
    raw = parse_multipart_file(body, ctype, field="file")
    with open(staged, "wb") as f:
        f.write(raw)
    meta = validate_zip(staged)   # verify_hashes off by default (see module docs)
    print(json.dumps({"ok": True, "project": meta}, ensure_ascii=False))
except ProjectZipError as e:
    try:
        os.unlink(staged)
    except OSError:
        pass
    print(json.dumps({"ok": False, "error_code": e.code, "error_message": e.message},
                     ensure_ascii=False))
except Exception as e:
    try:
        os.unlink(staged)
    except OSError:
        pass
    print(json.dumps({"ok": False, "error_code": "E_INTERNAL", "error_message": str(e)}))
PY
)

VALID_OK=$(printf '%s' "$VALIDATE_JSON" | python3 -c 'import json,sys;
try: print("1" if json.load(sys.stdin).get("ok") else "0")
except Exception: print("0")' 2>/dev/null || echo 0)
if [ "$VALID_OK" != "1" ]; then
  rm -f "$UPLOAD_BODY" 2>/dev/null || true
  printf '%s\n' "${VALIDATE_JSON:-'{"ok":false,"error_code":"E_UPLOAD","error_message":"upload failed"}'}"
  exit 0
fi

# Refuse if the RS-485 flasher holds the COM lease (mirror the mplc4-start gate).
if [ -x "$SVC_CTL" ]; then
  BUSY=$(sudo -n "$SVC_CTL" list 2>/dev/null | python3 -c 'import json,sys;
try: print("1" if json.load(sys.stdin).get("flasher_busy") else "0")
except Exception: print("0")' 2>/dev/null || echo 0)
  if [ "$BUSY" = "1" ]; then
    rm -f "$STAGED_ZIP" 2>/dev/null || true
    printf '{"ok":false,"error":"flasher_busy","error_code":"E_BUSY","error_message":"идёт прошивка или сканирование RS-485"}\n'
    exit 0
  fi
fi

# Single-flight fast-fail: TEST (do not hold) the deploy lock. The privileged
# helper is the TRUE single-flight guard — it flocks the same file for its whole
# run and a second launch exits. This test only turns a concurrent POST into a
# clean "busy" instead of a doomed second helper.
if command -v flock >/dev/null 2>&1; then
  : >>"$CGI_LOCK" 2>/dev/null || true
  if ! ( flock -n 9 ) 9<"$CGI_LOCK" 2>/dev/null; then
    printf '{"ok":false,"error":"busy","error_code":"E_LOCK","error_message":"развёртывание уже выполняется"}\n'
    exit 0
  fi
fi

# Answer `pending` immediately (deploy can exceed the nginx read timeout), then
# hand to the root helper. The helper owns STATUS_FILE from here on.
printf '{"ok":true,"pending":true,"stage":"validate"}\n'
nohup sudo -n "$HELPER" "$STAGED_ZIP" >>/var/log/sa02m_install.log 2>&1 &
exit 0
