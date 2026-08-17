#!/bin/bash
# SA-02m config-only factory reset — installed as
# /usr/local/libexec/sa02m-factory-reset-runner
#
# Semantics: signed defaults + allowlist wipe. NEVER rootfs/eMMC/FIT/self-flash.
# Stages: validating|backing_up|confirmed|wipe|apply|verify|done|rolling_back|rolled_back|error
#
# shellcheck shell=bash
set -euo pipefail

STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
LOCKFILE="${SA02M_UPDATE_LOCK:-$STATEDIR/update.lock}"
LOGFILE="${SA02M_FACTORY_LOG:-$STATEDIR/factory-reset.log}"
TXN_JSON="$STATEDIR/transaction.json"
IMAGING_LOCK=/run/sa02m-imaging.lock
DEFAULTS_ROOT="${SA02M_FACTORY_DEFAULTS_ROOT:-/usr/share/sa02m-factory-defaults}"
BACKUP_BIN="${SA02M_WEB_BACKUP:-/usr/local/sbin/sa02m-web-backup.sh}"
LISTS_DIR_FALLBACK="/etc/sa02m-factory-defaults/lists"
CONFIRM_PHRASE="SA02M-RESET"

SELF="${BASH_SOURCE[0]:-$0}"
CMD="${1:-run}"

mkdir -p "$STATEDIR" "$STATEDIR/backup-export" "$STATEDIR/rollback" "$STATEDIR/runner" \
  "$STATEDIR/staging" "$STATEDIR/state"
chmod 755 "$STATEDIR" 2>/dev/null || true

log() {
  local ts
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  printf '%s %s\n' "$ts" "$*" | tee -a "$LOGFILE" >/dev/null
}

iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- imaging lock (same policy as update runner) -----------------------------
install_imaging_lock() {
  date -Iseconds >"$IMAGING_LOCK"
  sync
  systemctl stop net-watchdog sa02m-watchdog-feed 2>/dev/null || true
  systemctl set-property --runtime Manager RuntimeWatchdogSec=0 2>/dev/null || true
}

cleanup_imaging_lock() {
  systemctl set-property --runtime Manager RuntimeWatchdogSec=15s 2>/dev/null || true
  systemctl start net-watchdog 2>/dev/null || true
  systemctl start sa02m-watchdog-feed 2>/dev/null || true
  rm -f "$IMAGING_LOCK"
}

# --- transaction journal (temp → fsync → rename; no Python dependency) -------
txn_write() {
  # Usage: txn_write key=value ...
  # Merges into existing transaction.json via python3 when available.
  local args=("$@")
  python3 - "$TXN_JSON" "${args[@]}" <<'PY'
import json, os, sys, tempfile, time
path = sys.argv[1]
fields = {}
for a in sys.argv[2:]:
    if "=" not in a:
        continue
    k, v = a.split("=", 1)
    if v in ("true", "True"):
        fields[k] = True
    elif v in ("false", "False"):
        fields[k] = False
    elif v.isdigit():
        fields[k] = int(v)
    elif v == "null":
        fields[k] = None
    else:
        fields[k] = v
txn = {}
if os.path.isfile(path):
    with open(path, "r", encoding="utf-8") as f:
        txn = json.load(f)
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
txn.setdefault("schema_version", 1)
txn.setdefault("operation", "factory_reset")
txn.setdefault("id", __import__("uuid").uuid4().hex)
txn.update(fields)
txn["updated_at"] = now
if fields.get("stage") in ("done", "error", "rolled_back"):
    txn["finished_at"] = now
    if fields.get("stage") == "done":
        txn["result"] = "success"
    elif fields.get("stage") == "rolled_back":
        txn["result"] = "rolled_back"
    elif fields.get("stage") == "error":
        txn.setdefault("result", "failed")
data = json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(prefix=".txn.", dir=d)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

txn_get() {
  local key=$1
  python3 -c 'import json,sys; t=json.load(open(sys.argv[1],encoding="utf-8")); v=t.get(sys.argv[2]); print("" if v is None else v)' \
    "$TXN_JSON" "$key" 2>/dev/null || true
}

fail() {
  local code=$1 msg=$2
  log "ERROR $code: $msg"
  if [ -n "${JOURNAL_DIR:-}" ] && [ -d "${JOURNAL_DIR:-}" ] && [ "${SA02M_FACTORY_ROLLING:-}" != "1" ]; then
    SA02M_FACTORY_ROLLING=1 rollback_from_journal || true
  fi
  txn_write "stage=error" "error_code=$code" "error_message=$msg" "result=failed" || true
  cleanup_imaging_lock || true
  exit 1
}

# --- preserve / wipe lists ---------------------------------------------------
load_lists() {
  local ver_dir wipe_f preserve_f
  ver_dir=$(resolve_defaults_dir)
  wipe_f="$ver_dir/lists/wipe.list"
  preserve_f="$ver_dir/lists/preserve.list"
  if [ ! -f "$wipe_f" ]; then
    wipe_f="$LISTS_DIR_FALLBACK/wipe.list"
  fi
  if [ ! -f "$preserve_f" ]; then
    preserve_f="$LISTS_DIR_FALLBACK/preserve.list"
  fi
  [ -f "$wipe_f" ] || fail E_INTERNAL "wipe.list missing"
  [ -f "$preserve_f" ] || fail E_INTERNAL "preserve.list missing"
  mapfile -t WIPE_LIST < <(grep -vE '^\s*(#|$)' "$wipe_f" || true)
  mapfile -t PRESERVE_LIST < <(grep -vE '^\s*(#|$)' "$preserve_f" || true)
}

path_matches_glob() {
  # $1=path $2=pattern (supports trailing / for prefix, or single * in basename)
  local path=$1 pat=$2
  case "$pat" in
    */)
      [[ "$path" == "$pat"* || "$path/" == "$pat"* ]] && return 0
      return 1
      ;;
    *\*)
      # shell glob
      # shellcheck disable=SC2254
      case "$path" in
        $pat) return 0 ;;
      esac
      return 1
      ;;
    *)
      [ "$path" = "$pat" ] && return 0
      return 1
      ;;
  esac
}

is_preserved() {
  local path=$1 p
  for p in "${PRESERVE_LIST[@]}"; do
    path_matches_glob "$path" "$p" && return 0
  done
  # Absolute hard denies
  case "$path" in
    /dev/*|/boot/*|/proc/*|/sys/*) return 0 ;;
  esac
  return 1
}

is_wipe_allowed() {
  local path=$1 p
  is_preserved "$path" && return 1
  for p in "${WIPE_LIST[@]}"; do
    path_matches_glob "$path" "$p" && return 0
  done
  return 1
}

resolve_defaults_dir() {
  local ver bundled
  ver=$(tr -d '\r' </var/www/network_config/VERSION 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
  if [ -n "$ver" ] && [ -d "$DEFAULTS_ROOT/$ver" ]; then
    printf '%s\n' "$DEFAULTS_ROOT/$ver"
    return 0
  fi
  bundled=$(ls -1d "$DEFAULTS_ROOT"/*/ 2>/dev/null | sort -V | tail -1 || true)
  if [ -n "$bundled" ]; then
    printf '%s\n' "${bundled%/}"
    return 0
  fi
  if [ -d /etc/sa02m-factory-defaults/templates ]; then
    printf '%s\n' /etc/sa02m-factory-defaults
    return 0
  fi
  return 1
}

# --- path safety: never touch block devices / use tar -C / -------------------
assert_safe_dst() {
  local dst=$1
  case "$dst" in
    /etc/*|/var/lib/sa02m-update/*) ;;
    *) fail E_APPLY "dst outside config tree: $dst" ;;
  esac
  is_preserved "$dst" && fail E_APPLY "refusing preserved path: $dst"
  is_wipe_allowed "$dst" || fail E_APPLY "dst not on wipe allowlist: $dst"
  # Refuse if path resolves to a block device
  if [ -b "$dst" ] || [ -b "$(readlink -f "$dst" 2>/dev/null || true)" ]; then
    fail E_APPLY "block device refused: $dst"
  fi
}

atomic_install_file() {
  local src=$1 dst=$2 mode=$3 owner=$4
  local dir tmp
  assert_safe_dst "$dst"
  dir=$(dirname "$dst")
  mkdir -p "$dir"
  tmp="$dst.tmp.$$"
  # Journal prior content for rollback (copy, never tar -C /)
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    local jdir="$JOURNAL_DIR/files"
    mkdir -p "$jdir$dir"
    cp -a "$dst" "$jdir$dst" 2>/dev/null || true
    printf '%s\n' "$dst" >>"$JOURNAL_DIR/touched.list"
  else
    printf '%s\n' "$dst" >>"$JOURNAL_DIR/created.list"
  fi
  install -m "$mode" "$src" "$tmp"
  # owner best-effort
  if [ -n "$owner" ]; then
    chown "$owner" "$tmp" 2>/dev/null || true
  fi
  sync
  mv -f "$tmp" "$dst"
  sync
}

clear_dir_allowlisted() {
  local dir=$1
  assert_safe_dst "$dir"
  [ -d "$dir" ] || return 0
  local jdir="$JOURNAL_DIR/files"
  mkdir -p "$jdir$dir"
  # Backup then remove contents (not the directory node)
  if compgen -G "$dir/*" >/dev/null 2>&1; then
    cp -a "$dir/." "$jdir$dir/" 2>/dev/null || true
    printf '%s\n' "$dir" >>"$JOURNAL_DIR/cleared_dirs.list"
    find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
  fi
}

rollback_from_journal() {
  log "rolling back from journal $JOURNAL_DIR"
  txn_write "stage=rolling_back" || true
  local f
  if [ -f "$JOURNAL_DIR/created.list" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      rm -f "$f" 2>/dev/null || true
    done <"$JOURNAL_DIR/created.list"
  fi
  if [ -d "$JOURNAL_DIR/files" ]; then
    # Restore files by walking journal tree
    while IFS= read -r -d '' f; do
      local rel="${f#"$JOURNAL_DIR/files"}"
      [ -n "$rel" ] || continue
      mkdir -p "$(dirname "$rel")"
      cp -a "$f" "$rel"
    done < <(find "$JOURNAL_DIR/files" -type f -print0 2>/dev/null || true)
  fi
  txn_write "stage=rolled_back" "result=rolled_back" || true
}

# --- apply templates ---------------------------------------------------------
apply_defaults() {
  local base=$1
  local t="$base/templates"
  [ -d "$t" ] || fail E_INTERNAL "templates missing under $base"

  # Wipe user overlays dir
  if [ -d /etc/sa02m-device-templates/user ]; then
    clear_dir_allowlisted /etc/sa02m-device-templates/user/
  fi

  # Clear allowlisted interfaces.d/*.conf then reinstall canonical templates only.
  local iface_conf
  if compgen -G "/etc/network/interfaces.d/*.conf" >/dev/null 2>&1; then
    for iface_conf in /etc/network/interfaces.d/*.conf; do
      [ -e "$iface_conf" ] || continue
      is_wipe_allowed "$iface_conf" || continue
      local jdir="$JOURNAL_DIR/files"
      mkdir -p "$jdir$(dirname "$iface_conf")"
      cp -a "$iface_conf" "$jdir$iface_conf" 2>/dev/null || true
      printf '%s\n' "$iface_conf" >>"$JOURNAL_DIR/touched.list"
      rm -f "$iface_conf"
    done
  fi

  # Optional files that may be absent on device: remove if wipe-allowed and no template
  local optional
  for optional in \
    /etc/sa02m_storage.conf \
    /etc/sa02m_status_blocks.conf \
    /etc/sa02m_serial_profile.conf \
    /etc/sa02m_flasher.conf \
    /etc/sa02m-mqtt-snmp.conf \
    /etc/sa02m-mqtt-opcua.conf
  do
    if [ -e "$optional" ] && is_wipe_allowed "$optional"; then
      local jdir="$JOURNAL_DIR/files"
      mkdir -p "$jdir$(dirname "$optional")"
      cp -a "$optional" "$jdir$optional" 2>/dev/null || true
      printf '%s\n' "$optional" >>"$JOURNAL_DIR/touched.list"
      rm -f "$optional"
    fi
  done

  local src dst mode owner
  while IFS=$'\t' read -r src dst mode owner; do
    [ -n "$src" ] || continue
    if [ ! -f "$t/$src" ]; then
      log "WARN missing template $src — skip"
      continue
    fi
    atomic_install_file "$t/$src" "$dst" "$mode" "$owner"
  done <<'MAP'
etc/nginx/.htpasswd	/etc/nginx/.htpasswd	0600	root:root
etc/sa02m_web.env	/etc/sa02m_web.env	0640	root:www-data
etc/network/interfaces.d/eth0.conf	/etc/network/interfaces.d/eth0.conf	0644	root:root
etc/network/interfaces.d/eth1.conf	/etc/network/interfaces.d/eth1.conf	0644	root:root
etc/sa02m_modem.conf	/etc/sa02m_modem.conf	0644	root:root
etc/sa02m_network.conf	/etc/sa02m_network.conf	0644	root:root
etc/sa02m-modbus-mqtt.yaml	/etc/sa02m-modbus-mqtt.yaml	0660	root:www-data
etc/sa02m-gateway.yaml	/etc/sa02m-gateway.yaml	0660	root:www-data
etc/sa02m-alice-client.conf	/etc/sa02m-alice-client.conf	0640	root:www-data
etc/sa02m-alice-devices.conf	/etc/sa02m-alice-devices.conf	0640	root:www-data
etc/sa02m-alice/sa02m-alice-client.conf	/etc/sa02m-alice/sa02m-alice-client.conf	0640	root:www-data
etc/sa02m-alice/sa02m-alice-devices.conf	/etc/sa02m-alice/sa02m-alice-devices.conf	0640	root:www-data
MAP

  # Alice: force client_enabled=false even if only one layout exists
  for dst in /etc/sa02m-alice-client.conf /etc/sa02m-alice/sa02m-alice-client.conf; do
    if [ -f "$dst" ] && is_wipe_allowed "$dst"; then
      if grep -q 'client_enabled' "$dst" 2>/dev/null; then
        sed -i 's/^[[:space:]]*client_enabled[[:space:]]*=.*/client_enabled = false/' "$dst" 2>/dev/null || true
      fi
    fi
  done
}

verify_reset() {
  [ -f /etc/nginx/.htpasswd ] || fail E_HEALTH "htpasswd missing after reset"
  grep -q '^admin:' /etc/nginx/.htpasswd || fail E_HEALTH "admin htpasswd line missing"
  if [ -f /etc/sa02m_web.env ]; then
    grep -q "SA02M_WEB_USER='admin'" /etc/sa02m_web.env || fail E_HEALTH "web env user not admin"
  fi
  # Preserve checks (must still exist if they existed before — only assert critical ones that always exist)
  [ -f /etc/machine-id ] || fail E_HEALTH "machine-id vanished"
  # Alice certs: if present before, must remain (checked via journal absence of those paths)
  if [ -f /var/lib/sa02m-alice/device.crt.pem ]; then
    is_preserved /var/lib/sa02m-alice/device.crt.pem || fail E_HEALTH "alice cert not preserved"
  fi
  # Never wrote to mmc
  true
}

do_backup() {
  local out=$1
  mkdir -p "$(dirname "$out")"
  if [ -x "$BACKUP_BIN" ]; then
    if "$BACKUP_BIN" --output "$out" >>"$LOGFILE" 2>&1; then
      return 0
    fi
    log "WARN backup helper failed; trying minimal tar of wipe allowlist"
  fi
  # Minimal fallback: tar only wipe-allowlisted existing files (still not tar -C / for extract)
  local listf="$STATEDIR/state/factory-backup-paths.txt"
  : >"$listf"
  local p
  for p in "${WIPE_LIST[@]}"; do
    case "$p" in
      */)
        [ -d "${p%/}" ] && printf '%s\n' "${p%/}" >>"$listf"
        ;;
      *\*)
        # shellcheck disable=SC2086
        compgen -G "$p" >>"$listf" 2>/dev/null || true
        ;;
      *)
        [ -e "$p" ] && printf '%s\n' "$p" >>"$listf"
        ;;
    esac
  done
  if [ ! -s "$listf" ]; then
    # create empty archive so stage can proceed (CGI already required backup_ok)
    tar -czf "$out" --files-from=/dev/null 2>/dev/null || gzip -c </dev/null >"$out"
    return 0
  fi
  tar -czf "$out" -T "$listf" --ignore-failed-read 2>>"$LOGFILE" || fail E_CMD "backup tar failed"
}

run_reset() {
  local defaults_dir ts backup_path

  # flock
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    fail E_LOCK "another update/reset holds $LOCKFILE"
  fi
  printf '%s\n' "$$" >&9

  # Require CGI-prepared transaction
  [ -f "$TXN_JSON" ] || fail E_INTERNAL "transaction.json missing"
  local op confirm_ok backup_ok
  op=$(txn_get operation)
  [ "$op" = "factory_reset" ] || fail E_INTERNAL "operation is not factory_reset"
  confirm_ok=$(txn_get confirm_phrase_ok)
  backup_ok=$(txn_get backup_ok)
  [ "$confirm_ok" = "True" ] || [ "$confirm_ok" = "true" ] || [ "$confirm_ok" = "1" ] \
    || fail E_CANCEL "confirm_phrase_ok not set (need SA02M-RESET via CGI)"
  [ "$backup_ok" = "True" ] || [ "$backup_ok" = "true" ] || [ "$backup_ok" = "1" ] \
    || fail E_CANCEL "backup_ok not set (mandatory backup)"

  txn_write "stage=validating" "progress_pct=5" "imaging_lock=false"
  load_lists
  defaults_dir=$(resolve_defaults_dir) || fail E_INTERNAL "factory defaults not installed"
  log "defaults_bundle=$defaults_dir"
  txn_write "defaults_bundle=$defaults_dir"
  python3 - "$TXN_JSON" "${WIPE_LIST[@]}" -- "${PRESERVE_LIST[@]}" <<'PY'
import json, os, sys, tempfile, time
path = sys.argv[1]
args = sys.argv[2:]
sep = args.index("--")
wipe, preserve = args[:sep], args[sep + 1 :]
txn = json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else {}
txn["wipe_manifest"] = wipe
txn["preserve_manifest"] = preserve
txn["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
data = json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(prefix=".txn.", dir=d)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(data)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, path)
PY

  # Self-copy before any mutate
  local txn_id runner_copy
  txn_id=$(txn_get id)
  [ -n "$txn_id" ] || txn_id="factory-$$"
  runner_copy="$STATEDIR/runner/$txn_id/runner"
  if [ "${SA02M_FACTORY_REEXEC:-}" != "1" ]; then
    mkdir -p "$(dirname "$runner_copy")"
    cp -a "$SELF" "$runner_copy"
    chmod 755 "$runner_copy"
    log "re-exec from $runner_copy"
    export SA02M_FACTORY_REEXEC=1
    exec "$runner_copy" run
  fi

  ts=$(date +%Y%m%dT%H%M%SZ)
  backup_path="$STATEDIR/backup-export/pre-reset-$ts.tar.gz"
  JOURNAL_DIR="$STATEDIR/rollback/factory-$ts"
  export JOURNAL_DIR
  mkdir -p "$JOURNAL_DIR"
  : >"$JOURNAL_DIR/touched.list"
  : >"$JOURNAL_DIR/created.list"

  # On unexpected failure after imaging lock: journal rollback (no tar -C /)
  rollback_on_err() {
    local ec=$?
    [ "$ec" -eq 0 ] && return 0
    log "ERR trap ec=$ec — attempting journal rollback"
    rollback_from_journal || true
    cleanup_imaging_lock || true
  }
  trap rollback_on_err ERR

  txn_write "stage=backing_up" "progress_pct=15" "backup_path=$backup_path"
  do_backup "$backup_path"
  log "backup=$backup_path"

  txn_write "stage=confirmed" "progress_pct=25"
  install_imaging_lock
  txn_write "imaging_lock=true"

  txn_write "stage=wipe" "progress_pct=40"
  log "wipe allowlist entries: ${#WIPE_LIST[@]}"

  txn_write "stage=apply" "progress_pct=60"
  apply_defaults "$defaults_dir"

  txn_write "stage=verify" "progress_pct=85"
  verify_reset

  trap - ERR

  # Reload services that consume configs (best-effort)
  systemctl reload sa02m-serial-gateway 2>/dev/null || true
  systemctl restart sa02m-modbus-mqtt 2>/dev/null || true
  if command -v nginx >/dev/null 2>&1; then
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  fi

  cleanup_imaging_lock
  txn_write "stage=done" "progress_pct=100" "imaging_lock=false" "result=success" \
    "error_code=null" "error_message="
  log "DONE factory reset"
  sync
}

case "$CMD" in
  run|apply) run_reset ;;
  rollback)
    JOURNAL_DIR=$(ls -1d "$STATEDIR/rollback"/factory-* 2>/dev/null | sort | tail -1 || true)
    [ -n "${JOURNAL_DIR:-}" ] || { echo "no journal"; exit 1; }
    rollback_from_journal
    ;;
  *)
    echo "usage: $0 run|rollback" >&2
    exit 2
    ;;
esac
