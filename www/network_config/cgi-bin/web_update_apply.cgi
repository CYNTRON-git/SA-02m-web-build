#!/bin/bash
# Web update apply / status (legacy GitHub OTA + offline file transaction).
# GET: transaction fields + legacy.status/log for status.js (plan §2.10 / §6.1),
#      plus runner_alive / stale (1.0.6.52 — a dead runner is reported, not polled forever).
# POST without confirm_version: legacy sa02m-web-update-apply (BC for status.js). CSRF required (launches a root GitHub OTA).
# POST with confirm_version: CSRF + write transaction → systemctl start sa02m-update.service.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_update.sh"

# Env-overridable ONLY for the harness (scripts/dev/test-web-update-apply-guard.sh);
# the same variable name etc/sa02m-update-runner.sh honours. nginx/fcgiwrap set nothing.
LEGACY_STATEDIR="${SA02M_WEB_BUILD_STATEDIR:-/var/lib/sa02m-web-build}"
LEGACY_LOCKFILE="$LEGACY_STATEDIR/update.lock"
LEGACY_STATUS_FILE="$LEGACY_STATEDIR/update_status"
LEGACY_LOGFILE="$LEGACY_STATEDIR/update.log"

# Same seam as the runner's STATEDIR (etc/sa02m-update-runner.sh); harness only.
STATEDIR="$WEB_UPD_STATEDIR"
PACKAGE="$STATEDIR/incoming/package.sa02m"
CGI_LOCK="$STATEDIR/incoming/.cgi.lock"
# A running-stage transaction whose runner is gone for longer than this is
# reported `stale` (status «error», E_RUNNER_LOST). 120 s covers the
# recover→verify handover at boot and any txn_patch gap shorter than the
# health gate's settle window (contract: docs/contracts/web-update.md).
WEB_UPD_STALE_AFTER_S=120

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
  # Liveness is judged in bash (lib_web_update.sh: lock pid + cmdline, units)
  # and handed to the JSON builder as a flag; python only does the arithmetic.
  local alive=0
  web_upd_runner_alive && alive=1
  python3 - "$STATEDIR" "$LEGACY_STATEDIR" "$alive" "$WEB_UPD_STALE_AFTER_S" <<'PY'
import datetime, json, sys, time
from pathlib import Path

statedir = Path(sys.argv[1])
legacy_dir = Path(sys.argv[2])
runner_alive_flag = sys.argv[3] == "1"
stale_after_s = int(sys.argv[4])
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

# Dead-runner detection (1.0.6.52, contract «GET — состояние»): a running-stage
# transaction with NO live runner for longer than stale_after_s is `stale` —
# reported as an error with a next step, never as «running» forever. Three
# conditions, all required (fail-closed toward «alive»: a false «stale» costs a
# page reload, a false «alive» costs 120 s). The legacy launcher lock counts as
# alive: the clone/handoff phase runs before the runner takes its own lock.
runner_alive = None
stale = False
if txn:
    runner_alive = bool(runner_alive_flag or legacy_running)
    if stage in running_stages and not runner_alive:
        try:
            t = datetime.datetime.strptime(str(txn.get("updated_at")), "%Y-%m-%dT%H:%M:%SZ")
            age = time.time() - t.replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            age = None  # no usable timestamp and no runner: nothing will ever move it
        if age is None or age > stale_after_s:
            stale = True
            legacy_status = "error"
            age_txt = "unknown" if age is None else "%d" % int(age)

prefer_new = stage in running_stages or stage in ("done", "error", "rolled_back", "cancelled")
# The human line for the OLD cached bundle: it never reads `stale`, it routes
# `log` to the event log and prints the generic «Ошибка обновления» — this is
# the only channel through which a board on ≤1.0.6.51 (the delivering OTA
# freezes at 85 % under the old runner) can tell the operator what to do.
# The labels mirror WEB_UPD_STAGE_UI in static/js/app/status.js (the one home
# for the panel's stage wording) minus the trailing ellipsis — a second copy by
# necessity: the CGI cannot read the bundle. Keep in step.
STAGE_RU = {
    "uploaded": "Проверка пакета", "validating": "Проверка пакета",
    "backing_up": "Создание резервной копии", "applying": "Установка",
    "verifying": "Проверка сервисов", "committing": "Проверка сервисов",
    "rolling_back": "Откат",
}
stale_line = ""
if stale:
    stale_line = ("Обновление прервано на этапе «%s»: перезагрузите плату — при загрузке "
                  "проверка завершится сама." % STAGE_RU.get(stage, stage))
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
    "log": (stale_line + "\n" + log_tail).strip() if stale_line else log_tail,
    "legacy": {"status": legacy_status},
    "runner_alive": runner_alive,
    "stale": stale,
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
    if stale:
        # A code the runner already recorded (E_HEALTH on a rollback it never
        # finished) is kept — it names the real cause; E_RUNNER_LOST is the
        # code for «nothing recorded, and nobody is running».
        out["error_code"] = txn.get("error_code") or "E_RUNNER_LOST"
        out["error_message"] = txn.get("error_message") or (
            "update runner is not running (stage=%s, last update %ss ago) — "
            "reboot the board; verification completes at boot" % (stage, age_txt))
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

# ── Legacy GitHub OTA (status.js BC — empty/no confirm_version) ───
# CSRF required: this path launches a full root update from GitHub via
# `sudo -n sa02m-web-update-apply`. The policy (docs/decisions/selective-csrf-policy.md)
# and the threat model both say every session-authed mutating endpoint carries the
# token; until 1.0.6.24 this one was marked "no CSRF" and did not (audit M6). The web
# UI already sends X-SA02M-CSRF here (app/status.js withCsrfHeaders()). Checked BEFORE
# any header is emitted so the shared E_CSRF error shape can be returned; a stale
# cached bundle without the token gets E_CSRF and the app.js wrapper re-logs in.
if ! web_csrf_validate; then
  _json_headers
  printf '{"ok":false,"error":"csrf","error_code":"E_CSRF"}\n'
  exit 0
fi

# Internet Apply only (this branch; file-package apply uses confirm_version
# above). FAIL-CLOSED guard — docs/contracts/web-update.md is the one home of
# the table: the ONLY way to the root launch below is a readable, FRESH
# check.json that says the remote is newer. Until 1.0.6.39 every error path
# here (no file, no python3, unparseable JSON) *permitted* the launch, and a
# stale file was read as current truth (audit C10). Exit codes of the probe:
# 0 = newer available (launch), 2 = nothing newer (E_NO_UPDATE), anything
# else = cannot tell (E_CHECK_STALE). The state dir is env-overridable for the
# harness only (scripts/dev/test-web-update-apply-guard.sh).
WEB_UPD_CHECK_MAX_AGE_S=86400
CHECK_JSON="$LEGACY_STATEDIR/check.json"
guard_rc=3
if [ -f "$CHECK_JSON" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$CHECK_JSON" "$WEB_UPD_CHECK_MAX_AGE_S" <<'PY'
import datetime, json, re, sys, time
try:
    j = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(3)
if not isinstance(j, dict):
    raise SystemExit(3)
# Freshness: checked_at is the checker's UTC stamp (etc/sa02m-web-update-check.sh).
try:
    t = datetime.datetime.strptime(str(j.get("checked_at")), "%Y-%m-%dT%H:%M:%SZ")
    t = t.replace(tzinfo=datetime.timezone.utc).timestamp()
except Exception:
    raise SystemExit(3)
age = time.time() - t
if age > int(sys.argv[2]) or age < -3600:
    raise SystemExit(3)

def parse(v):
    if v is None:
        return None
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", str(v).strip())
    if not m:
        return None
    return [int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)]

# Same precedence as the frontend's webUpdResolveAvailable (app/status.js):
# a version compare wins, else the checker's own flag, else unknowable.
dep, rem = parse(j.get("deployed_version")), parse(j.get("remote_version"))
if dep is not None and rem is not None:
    raise SystemExit(2 if dep >= rem else 0)
if j.get("update_available") is False:
    raise SystemExit(2)
if j.get("update_available") is True:
    raise SystemExit(0)
raise SystemExit(3)
PY
  guard_rc=$?
fi
case "$guard_rc" in
  0) : ;;
  2)
    _json_headers
    printf '{"ok":false,"status":"error","error":"no_update","error_code":"E_NO_UPDATE","log":"Обновлений нет"}\n'
    exit 0
    ;;
  *)
    _json_headers
    printf '{"ok":false,"status":"error","error":"check_stale","error_code":"E_CHECK_STALE","log":"Сведения об обновлении устарели — нажмите «Проверить»"}\n'
    exit 0
    ;;
esac

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
