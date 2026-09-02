#!/bin/bash
# Fetch web UI from GitHub and deploy. Runs as root via sudo.
# Prefers shared sa02m-update-runner (plan §2.9) when installed; otherwise
# falls back to the legacy rsync path (bootstrap release N).
# Output → $LOGFILE; status → $STATEDIR/update_status (running/done/error).
set -uo pipefail

LIB=/usr/local/lib/sa02m-web-build-lib.sh
[ -f "$LIB" ] && . "$LIB"

STATEDIR=/var/lib/sa02m-web-build
UPDATE_STATEDIR="${SA02M_UPDATE_STATEDIR:-/var/lib/sa02m-update}"
LOGFILE="$STATEDIR/update.log"
LOCKFILE="$STATEDIR/update.lock"
STATUS_FILE="$STATEDIR/update_status"
REPO_URL="${SA02M_WEB_BUILD_REPO_URL:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
BRANCH="${SA02M_WEB_BUILD_BRANCH:-main}"
if type resolve_web_build_branch >/dev/null 2>&1; then
    BRANCH="$(resolve_web_build_branch)"
fi
WEB_ROOT=/var/www/network_config
RUNNER="${SA02M_UPDATE_RUNNER:-/usr/local/libexec/sa02m-update-runner}"
# Fixed HTTPS allowlist — no arbitrary URL from UI (plan §2.9).
REPO_ALLOWLIST="${SA02M_WEB_BUILD_REPO_ALLOWLIST:-https://github.com/CYNTRON-git/SA-02m-web-build.git}"
COMMIT_PIN="${SA02M_WEB_BUILD_COMMIT_PIN:-}"
LS_REMOTE_CACHE="$UPDATE_STATEDIR/state/github_ls_remote.cache"
LS_REMOTE_MAX_AGE_SEC="${SA02M_WEB_BUILD_LS_REMOTE_MAX_AGE_SEC:-3600}"

mkdir -p "$STATEDIR"
chmod 755 "$STATEDIR"

log() { local ts; ts=$(date '+%Y-%m-%d %H:%M:%S'); printf '%s %s\n' "$ts" "$*" | tee -a "$LOGFILE"; }

normalize_repo_url() {
    local u=$1
    u="${u%.git}"
    u="${u%/}"
    printf '%s' "$u"
}

repo_url_allowed() {
    local want have n_want n_have
    want=$(normalize_repo_url "$1")
    IFS=',' read -r -a _allow <<< "$REPO_ALLOWLIST"
    for have in "${_allow[@]}"; do
        have=$(echo "$have" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -n "$have" ] || continue
        n_have=$(normalize_repo_url "$have")
        n_want=$want
        if [ "$n_want" = "$n_have" ] || [ "$n_want.git" = "$n_have" ] || [ "$n_want" = "$n_have.git" ]; then
            return 0
        fi
        # Also accept exact configured string (with .git).
        if [ "$1" = "$have" ]; then
            return 0
        fi
    done
    return 1
}

# Lock
if [ -f "$LOCKFILE" ]; then
    pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "ERROR: обновление уже выполняется (pid $pid)"
        exit 1
    fi
    rm -f "$LOCKFILE"
fi
printf '%s' "$$" > "$LOCKFILE"
printf 'running' > "$STATUS_FILE"
chmod 644 "$LOCKFILE" "$STATUS_FILE" 2>/dev/null || true

cleanup() {
    rm -f "$LOCKFILE"
    rm -rf "${TMPDIR:-}" 2>/dev/null || true
}
trap cleanup EXIT

> "$LOGFILE"
log "START: $REPO_URL ветка $BRANCH"
printf 'running' > "$STATUS_FILE"

if ! repo_url_allowed "$REPO_URL"; then
    log "ERROR: REPO_URL not in allowlist: $REPO_URL"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

# Temporary directory for clone
TMPDIR=$(mktemp -d /tmp/sa02m-web-update-XXXXXX)
chmod 755 "$TMPDIR"

log "Клонирование репозитория..."
if ! git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 \
    clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$TMPDIR/repo" 2>&1 | tee -a "$LOGFILE"; then
    log "ERROR: git clone не удался"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

# Trust gate after clone (plan §2.9)
remote_url=$(git -C "$TMPDIR/repo" remote get-url origin 2>/dev/null || echo "")
if ! repo_url_allowed "$remote_url"; then
    log "ERROR: cloned remote URL not in allowlist: $remote_url"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

NEW_COMMIT=$(git -C "$TMPDIR/repo" rev-parse HEAD 2>/dev/null || echo "")
if [ -z "$NEW_COMMIT" ]; then
    log "ERROR: cannot resolve cloned HEAD"
    printf 'error' > "$STATUS_FILE"
    exit 1
fi

if [ -n "$COMMIT_PIN" ]; then
    if [ "$NEW_COMMIT" != "$COMMIT_PIN" ]; then
        log "ERROR: commit pin mismatch (have $NEW_COMMIT want $COMMIT_PIN)"
        printf 'error' > "$STATUS_FILE"
        exit 1
    fi
    log "Commit pin OK: $NEW_COMMIT"
else
    mkdir -p "$UPDATE_STATEDIR/state" 2>/dev/null || true
    now_ts=$(date +%s)
    cache_ok=0
    if [ -f "$LS_REMOTE_CACHE" ]; then
        cache_ts=$(awk 'NR==1{print $1}' "$LS_REMOTE_CACHE" 2>/dev/null || echo 0)
        cache_sha=$(awk 'NR==1{print $2}' "$LS_REMOTE_CACHE" 2>/dev/null || echo "")
        if [ -n "$cache_ts" ] && [ -n "$cache_sha" ] && \
            [ $((now_ts - cache_ts)) -le "$LS_REMOTE_MAX_AGE_SEC" ]; then
            if [ "$NEW_COMMIT" = "$cache_sha" ]; then
                cache_ok=1
                log "ls-remote cache hit: $cache_sha"
            fi
        fi
    fi
    if [ "$cache_ok" != 1 ]; then
        remote_sha=$(git ls-remote --heads "$REPO_URL" "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1; exit}')
        if [ -n "$remote_sha" ]; then
            printf '%s %s\n' "$now_ts" "$remote_sha" >"$LS_REMOTE_CACHE"
            chmod 644 "$LS_REMOTE_CACHE" 2>/dev/null || true
            if [ "$NEW_COMMIT" != "$remote_sha" ]; then
                # depth=1 clone of a moving branch can race; accept clone HEAD but warn
                log "WARN: clone HEAD $NEW_COMMIT != ls-remote $remote_sha (race or shallow); continuing with clone"
            else
                log "ls-remote OK: $remote_sha"
            fi
        else
            log "WARN: git ls-remote failed — trusting allowlisted clone HEAD $NEW_COMMIT"
        fi
    fi
fi

# Shared runner handoff when installed and capable of source=github (plan §2.9).
# Bootstrap N: runner may be absent or not yet understand github overlay → legacy.
handoff_to_shared_runner() {
    [ -x "$RUNNER" ] || return 1
    # Runner must mention github trust path; otherwise keep legacy www rsync.
    if ! grep -qE 'source=.github.|overlay_path|SA02M_UPDATE_GITHUB' "$RUNNER" 2>/dev/null; then
        log "WARN: $RUNNER present but no github handoff support yet — legacy deploy"
        return 1
    fi

    local prev_ver=""
    if [ -f "$WEB_ROOT/VERSION" ]; then
        prev_ver=$(tr -d '\r' <"$WEB_ROOT/VERSION" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
    fi
    local target_ver=""
    if [ -f "$TMPDIR/repo/www/network_config/VERSION" ]; then
        target_ver=$(tr -d '\r' <"$TMPDIR/repo/www/network_config/VERSION" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
    fi

    mkdir -p "$UPDATE_STATEDIR" "$UPDATE_STATEDIR/state" "$UPDATE_STATEDIR/incoming"
    # Keep clone for runner overlay (cleanup trap must not remove it after handoff).
    export SA02M_UPDATE_GITHUB_OVERLAY="$TMPDIR/repo"
    trap 'rm -f "$LOCKFILE"' EXIT

    local txn_id
    txn_id=$(
        UPDATE_STATEDIR="$UPDATE_STATEDIR" OVERLAY="$TMPDIR/repo" \
        TARGET_COMMIT="$NEW_COMMIT" TARGET_VER="${target_ver:-}" PREV_VER="${prev_ver:-}" \
        REPO_URL="$REPO_URL" BRANCH="$BRANCH" \
        python3 - <<'PY'
import json, os, time, uuid
from pathlib import Path

statedir = Path(os.environ["UPDATE_STATEDIR"])
statedir.mkdir(parents=True, exist_ok=True)
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
txn = {
    "schema_version": 1,
    "id": str(uuid.uuid4()),
    "operation": "update",
    "source": "github",
    "package_path": "",
    "overlay_path": os.environ["OVERLAY"],
    "repo_url": os.environ.get("REPO_URL", ""),
    "branch": os.environ.get("BRANCH", ""),
    "target_version": os.environ.get("TARGET_VER") or None,
    "target_commit": os.environ.get("TARGET_COMMIT") or None,
    "previous_version": os.environ.get("PREV_VER") or None,
    "stage": "validating",
    "progress_pct": 0,
    "files_total": 0,
    "files_done": 0,
    "result": "pending",
    "error_code": None,
    "error_message": None,
    "rollback_archive": None,
    "imaging_lock": False,
    "signature_ok": False,
    "started_at": now,
    "updated_at": now,
    "finished_at": None,
}
path = statedir / "transaction.json"
tmp = statedir / (".txn." + txn["id"])
tmp.write_text(json.dumps(txn, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print(txn["id"])
PY
    )

    if [ -z "$txn_id" ]; then
        log "ERROR: failed to write github transaction.json"
        return 1
    fi
    log "Handoff to shared runner txn=$txn_id overlay=$TMPDIR/repo"
    # Do not delete clone on EXIT — runner owns overlay_path.
    TMPDIR=""
    rm -f "$LOCKFILE"
    printf 'running' > "$STATUS_FILE"
    exec "$RUNNER" apply
}

if handoff_to_shared_runner; then
    : # exec never returns
fi
log "Using legacy rsync deploy (shared runner missing or github handoff unavailable)"

log "Деплой веб-файлов в $WEB_ROOT..."
# rsync --delete so files removed in a newer release do NOT linger on the
# device (a stale/vulnerable CGI would keep serving). Fall back to a purge+cp
# where rsync is unavailable. Mirrors the installer's find -delete + copy.
if command -v rsync >/dev/null 2>&1; then
    if ! rsync -a --delete "$TMPDIR/repo/www/network_config/" "$WEB_ROOT/" 2>&1 | tee -a "$LOGFILE"; then
        log "ERROR: rsync не удался"
        printf 'error' > "$STATUS_FILE"
        exit 1
    fi
else
    find "$WEB_ROOT" -mindepth 1 -delete 2>/dev/null || true
    if ! cp -a "$TMPDIR/repo/www/network_config/." "$WEB_ROOT/" 2>&1 | tee -a "$LOGFILE"; then
        log "ERROR: копирование не удалось"
        printf 'error' > "$STATUS_FILE"
        exit 1
    fi
fi

# Права доступа
find "$WEB_ROOT/cgi-bin" -name '*.cgi' -exec chmod 755 {} \; 2>/dev/null || true
find "$WEB_ROOT/static" \( -name '*.css' -o -name '*.js' \) -exec chmod 644 {} \; 2>/dev/null || true
chmod 644 "$WEB_ROOT/index.html" "$WEB_ROOT/login.html" 2>/dev/null || true
chown -R www-data:www-data "$WEB_ROOT" 2>/dev/null || true

# Обновляем вспомогательные скрипты из репозитория
for src in etc/sa02m-web-build-lib.sh etc/sa02m-web-update-check.sh etc/sa02m-web-update-apply.sh etc/sa02m-web-auth-lib.sh etc/sa02m-repair-web-env.sh etc/sa02m-web-root-cmd.sh etc/sa02m-update-runner.sh etc/sa02m-update-inspect.sh; do
    if [ -f "$TMPDIR/repo/$src" ]; then
        tgt="/usr/local/sbin/$(basename "${src%.sh}")"
        if [ "$src" = "etc/sa02m-web-auth-lib.sh" ]; then
            tgt="/usr/local/lib/sa02m-web-auth-lib.sh"
            install -m 644 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        elif [ "$src" = "etc/sa02m-web-build-lib.sh" ]; then
            tgt="/usr/local/lib/sa02m-web-build-lib.sh"
            install -m 644 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        elif [ "$src" = "etc/sa02m-web-root-cmd.sh" ]; then
            tgt="/usr/local/sbin/sa02m-web-root-cmd.sh"
            install -m 755 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        elif [ "$src" = "etc/sa02m-update-runner.sh" ]; then
            mkdir -p /usr/local/libexec
            tgt="/usr/local/libexec/sa02m-update-runner"
            install -m 755 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        elif [ "$src" = "etc/sa02m-update-inspect.sh" ]; then
            mkdir -p /usr/local/libexec
            tgt="/usr/local/libexec/sa02m-update-inspect"
            install -m 755 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        else
            install -m 755 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        fi
        log "Обновлён $tgt"
    fi
done
# Privileged config-apply helpers from usr/local/sbin. The sudoers block below
# installs the pins that name these paths; shipping the pin without the helper
# leaves a board that only ever updates this way running the OLD, trust-the-
# caller helper while the panel reports the new version (security review
# 1.0.6.24, F4). They live under usr/local/sbin/ precisely so every delivery
# path — this one included — can copy them 1:1 with no rename table.
for src in usr/local/sbin/sa02m-gateway-config-apply.sh \
           usr/local/sbin/sa02m-mqtt-config-apply.sh \
           usr/local/sbin/sa02m-cloud-web-trigger.sh; do
    if [ -f "$TMPDIR/repo/$src" ]; then
        tgt="/usr/local/sbin/$(basename "$src")"
        install -m 755 "$TMPDIR/repo/$src" "$tgt" && sed -i 's/\r$//' "$tgt"
        log "Обновлён $tgt"
    fi
done

# tmpfiles: runtime dirs the deployed CGI needs but www-data cannot create
# itself under root-owned /run (the login-throttle store — without it the
# lockout in lib_web_auth.sh fails OPEN; security review 1.0.6.24, F1).
for conf in sa02m-web-login.conf; do
    if [ -f "$TMPDIR/repo/etc/tmpfiles.d/$conf" ]; then
        install -m 644 "$TMPDIR/repo/etc/tmpfiles.d/$conf" "/etc/tmpfiles.d/$conf"
        sed -i 's/\r$//' "/etc/tmpfiles.d/$conf"
        if command -v systemd-tmpfiles >/dev/null 2>&1; then
            systemd-tmpfiles --create "/etc/tmpfiles.d/$conf" >>"$LOGFILE" 2>&1 || true
        fi
        log "Обновлён /etc/tmpfiles.d/$conf"
    fi
done

# Bootstrap updater libs from the same clone when present (release N → N+1).
if [ -d "$TMPDIR/repo/opt/sa02m-update" ]; then
    mkdir -p /opt/sa02m-update
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
            "$TMPDIR/repo/opt/sa02m-update/" /opt/sa02m-update/ 2>&1 | tee -a "$LOGFILE" || true
    else
        cp -a "$TMPDIR/repo/opt/sa02m-update/." /opt/sa02m-update/ 2>&1 | tee -a "$LOGFILE" || true
    fi
    log "Обновлён /opt/sa02m-update"
fi
# sudoers: install the committed single-home file WHOLESALE (audit B1, home 6).
#
# This used to APPEND the sa02m-web-root-cmd.sh grant when it was missing. An
# append can only ever WIDEN: it never removes a stale dangerous grant, and it
# falsified the "installed WHOLESALE … no append-if-missing" claim in the file's
# own header. It could not simply be deleted either — this legacy rsync path
# deploys no /etc/sudoers.d file of its own (see the helper loop above), so a
# board that only ever updated this way would stop converging altogether.
# Installing the committed file whole does both jobs: the root-cmd grant lands,
# and a stale grant is REMOVED. Validate before activating — a malformed
# drop-in breaks sudo globally.
if [ -f "$TMPDIR/repo/etc/sudoers.d/sa02m-www" ]; then
    _sud_tmp=$(mktemp 2>/dev/null) || _sud_tmp=""
    if [ -n "$_sud_tmp" ]; then
        sed 's/\r$//' "$TMPDIR/repo/etc/sudoers.d/sa02m-www" > "$_sud_tmp"
        if ! command -v visudo >/dev/null 2>&1 || visudo -cf "$_sud_tmp" >>"$LOGFILE" 2>&1; then
            install -m 0440 -o root -g root "$_sud_tmp" /etc/sudoers.d/sa02m-www \
                && log "Обновлён /etc/sudoers.d/sa02m-www (установлен целиком)"
        else
            log "WARN: visudo отклонил etc/sudoers.d/sa02m-www — живой файл не тронут"
        fi
        rm -f "$_sud_tmp"
    fi
fi
if [ -x /usr/local/sbin/sa02m-repair-web-env ]; then
    /usr/local/sbin/sa02m-repair-web-env >>"$LOGFILE" 2>&1 || true
fi

# Записываем задеплоенный коммит
if [ -n "$NEW_COMMIT" ]; then
    printf '%s\n' "$NEW_COMMIT" > "$STATEDIR/deployed_commit"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATEDIR/deployed_at"
    chmod 644 "$STATEDIR/deployed_commit" "$STATEDIR/deployed_at" 2>/dev/null || true
    mkdir -p "$UPDATE_STATEDIR/state" 2>/dev/null || true
    printf '%s\n' "$NEW_COMMIT" > "$UPDATE_STATEDIR/state/deployed_commit" 2>/dev/null || true
    date -u +%Y-%m-%dT%H:%M:%SZ > "$UPDATE_STATEDIR/state/deployed_at" 2>/dev/null || true
    log "Задеплоен коммит $NEW_COMMIT"
fi

if type write_web_build_conf_branch >/dev/null 2>&1; then
    # Persist the branch we actually targeted — NEVER the deployed VERSION.
    # Releases live on main and version-named branches are deleted post-merge,
    # so writing a version here re-poisons the conf that resolve_web_build_branch
    # reads on the next check (1.0.5.75: false network_or_git_failed). A bare
    # version (or empty/main/HEAD) collapses to main; a real dev branch stays.
    conf_branch="$BRANCH"
    if [ -z "$conf_branch" ] || [ "$conf_branch" = "HEAD" ] \
       || printf '%s' "$conf_branch" | grep -qE '^[0-9]+(\.[0-9]+){1,3}$'; then
        conf_branch=main
    fi
    write_web_build_conf_branch "$conf_branch" && log "Записан /etc/sa02m_web_build.conf branch=$conf_branch"
fi

# Сбрасываем кэш check.json
rm -f "$STATEDIR/check.json" 2>/dev/null || true
# Сбрасываем кэш status.cgi (JS/CSS обновились — новая версия в браузере сразу)
rm -f /tmp/sa02m_status_cache/*.json /tmp/sa02m_status_cache/*.lock 2>/dev/null || true

log "DONE: обновление завершено успешно"
printf 'done' > "$STATUS_FILE"
