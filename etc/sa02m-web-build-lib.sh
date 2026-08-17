#!/bin/bash
# Общие функции для sa02m-web-update-check / sa02m-web-update-apply
# shellcheck shell=bash

WEB_VERSION_FILE="${WEB_VERSION_FILE:-/var/www/network_config/VERSION}"
WEB_APP_JS="${WEB_APP_JS:-/var/www/network_config/static/js/app.js}"
WEB_BUILD_CONF="${WEB_BUILD_CONF:-/etc/sa02m_web_build.conf}"

read_version_from_text() {
    tr -d '\r' | grep -E '^[0-9]+(\.[0-9]+){1,3}$' 2>/dev/null | head -1
}

read_version_from_app_js() {
    [ -f "$WEB_APP_JS" ] || return 1
    sed -n "s/^const APP_VERSION = '\\([^']*\\)'.*/\\1/p" "$WEB_APP_JS" 2>/dev/null | head -1 | read_version_from_text
}

read_local_web_version() {
    local v1="" v2=""
    if [ -f "$WEB_VERSION_FILE" ]; then
        v1=$(read_version_from_text < "$WEB_VERSION_FILE" || true)
    fi
    v2=$(read_version_from_app_js 2>/dev/null || true)
    if [ -n "$v2" ]; then
        printf '%s' "$v2"
        return 0
    fi
    if [ -n "$v1" ]; then
        printf '%s' "$v1"
        return 0
    fi
    return 1
}

load_web_build_conf() {
    if [ -f "$WEB_BUILD_CONF" ]; then
        # shellcheck disable=SC1090
        . "$WEB_BUILD_CONF" 2>/dev/null || true
    fi
}

write_web_build_conf_branch() {
    local branch=$1
    [ -n "$branch" ] || return 1
    case "$branch" in
        *[!a-zA-Z0-9._-]*) return 1 ;;
    esac
    umask 022
    cat > "$WEB_BUILD_CONF" <<EOF
# Авто: ветка GitHub для проверки/установки обновлений веб-UI.
# Совпадает с www/network_config/VERSION / APP_VERSION.
SA02M_WEB_BUILD_BRANCH=${branch}
EOF
    chmod 644 "$WEB_BUILD_CONF" 2>/dev/null || true
}

# Which GitHub branch the update check/apply targets. Releases live on **main**:
# a version-named branch (e.g. 1.0.5.73) is DELETED from origin after its PR
# squash-merges, so every remote call against it 404s and the check falsely
# reports network_or_git_failed even with working internet (1.0.5.75 fix).
# Therefore: default to main, honour ONLY an explicit non-main, non-version pin
# (the real dev-branch escape hatch), and IGNORE a bare version-string pin — the
# pre-1.0.5.75 auto-written value that also lingers in stale confs on already-
# deployed devices.
resolve_web_build_branch() {
    load_web_build_conf
    local pin="${SA02M_WEB_BUILD_BRANCH:-}"
    if [ -n "$pin" ] && [ "$pin" != "main" ] \
       && ! printf '%s' "$pin" | grep -qE '^[0-9]+(\.[0-9]+){1,3}$'; then
        printf '%s' "$pin"
        return 0
    fi
    printf '%s' "main"
}

# Persist the check/apply branch. The production/device path resolves to main
# (empty / main / HEAD / a bare version → main); a real non-main dev branch is
# still written verbatim (it exists during dev). Never persists the deployed
# VERSION as a branch — see resolve_web_build_branch above.
sync_web_build_conf_from_deploy() {
    local branch=""
    if [ -n "${REPO_ROOT:-}" ] && [ -d "${REPO_ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
        branch=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || true)
    fi
    if [ -z "$branch" ] || [ "$branch" = "main" ] || [ "$branch" = "HEAD" ] \
       || printf '%s' "$branch" | grep -qE '^[0-9]+(\.[0-9]+){1,3}$'; then
        branch=main
    fi
    write_web_build_conf_branch "$branch"
}
