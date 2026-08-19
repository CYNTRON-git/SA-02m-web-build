#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Publish the self-upgrade BRIDGE onto stale version branches (dev machine).
#
#   bash tools/update-bridge/publish-bridge.sh <stale-branch> | --all-stale [--push]
#
# For each target branch `N.N.N[.N]` on origin (never main, never a branch at
# or above main's version): build the bridge commit = origin/main tree with
# EXACTLY ONE file replaced — etc/sa02m-repair-web-env.sh ← the launcher
# (tools/update-bridge/repair-web-env-launcher.sh, taken from origin/main) —
# tag the branch's OLD tip `archive/<branch>` (rollback home; kept if it already
# exists), push the tag, then move the branch to the new commit with
# --force-with-lease on the old tip. DRY-RUN by default: prints old tip → new
# sha and the exact pushes; `--push` executes and post-verifies (ls-remote tip,
# raw VERSION at the new sha == main's version).
# WHY + what the board does with it: docs/deployment.md «Мост самообновления».
# Mechanics: git plumbing (read-tree / update-index / write-tree / commit-tree)
# instead of a checkout — no worktree on disk, nothing to clean up, and the
# result is byte-identical to "origin/main + one file". Runs on Linux, WSL and
# Windows Git-Bash (temp index lives inside .git via `git rev-parse --git-path`).
# ═══════════════════════════════════════════════════════════════════════════
set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE=origin
BRIDGE_FILE=etc/sa02m-repair-web-env.sh
LAUNCHER=tools/update-bridge/repair-web-env-launcher.sh
WRAPPER=scripts/offline-full-update.sh
RAW_BASE=https://raw.githubusercontent.com/CYNTRON-git/SA-02m-web-build
VER_RE='^[0-9]+(\.[0-9]+){1,3}$'

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}
die() { echo "ERR: $*" >&2; exit 1; }
g() { git -C "$REPO_ROOT" "$@"; }

TARGET="" ALL=0 PUSH=0
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --all-stale) ALL=1 ;;
        --push) PUSH=1 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) [ -z "$TARGET" ] || { echo "One branch or --all-stale" >&2; exit 2; }; TARGET=$1 ;;
    esac
    shift
done
if [ "$ALL" = 1 ] && [ -n "$TARGET" ]; then echo "Either <branch> or --all-stale" >&2; exit 2; fi
if [ "$ALL" = 0 ] && [ -z "$TARGET" ]; then usage >&2; exit 2; fi
if [ -n "$TARGET" ] && ! printf '%s' "$TARGET" | grep -qE "$VER_RE"; then
    die "branch name must be a version (N.N.N[.N]); main is never a target"
fi

read_version() { tr -d '\r' | grep -E "$VER_RE" | head -1; }
version_lt() {   # $1 < $2
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

echo "== git fetch $REMOTE"
g fetch --prune "$REMOTE" >/dev/null 2>&1 || die "git fetch $REMOTE failed"
MAIN_TIP="$(g rev-parse "$REMOTE/main")" || die "no $REMOTE/main"
MAIN_VER="$(g show "$REMOTE/main:www/network_config/VERSION" 2>/dev/null | read_version)"
[ -n "$MAIN_VER" ] || die "cannot read VERSION from $REMOTE/main"
echo "   main: $MAIN_TIP  version $MAIN_VER"

# The launcher and the wrapper MUST already be on origin/main: the bridge
# commit's launcher clones main and runs main's wrapper. Until main carries
# them (PR merged) this tool can only dry-run from the working tree.
LAUNCHER_SRC="$REMOTE/main:$LAUNCHER"
if ! g cat-file -e "$LAUNCHER_SRC" 2>/dev/null; then
    LAUNCHER_SRC="$REPO_ROOT/$LAUNCHER"
    [ -f "$LAUNCHER_SRC" ] || die "launcher not found on $REMOTE/main nor in the working tree ($LAUNCHER)"
    echo "   WARN: $REMOTE/main has no $LAUNCHER yet (merge main first) — using the working-tree copy; --push is refused"
    [ "$PUSH" = 0 ] || die "refusing --push: $LAUNCHER is not on $REMOTE/main"
fi
if ! g cat-file -e "$REMOTE/main:$WRAPPER" 2>/dev/null; then
    echo "   WARN: $REMOTE/main has no $WRAPPER yet — the launcher would fail on the board; --push is refused"
    [ "$PUSH" = 0 ] || die "refusing --push: $WRAPPER is not on $REMOTE/main"
fi

# ── Target branches ────────────────────────────────────────────────────────
REMOTE_HEADS="$(g ls-remote --heads "$REMOTE" 2>/dev/null)" || die "git ls-remote failed"
if [ "$ALL" = 1 ]; then
    TARGETS="$(printf '%s\n' "$REMOTE_HEADS" | awk '{print $2}' | sed 's#^refs/heads/##' | grep -E "$VER_RE" | sort -V)"
else
    TARGETS="$TARGET"
fi
[ -n "$TARGETS" ] || die "no version branches on $REMOTE"
# One remote round-trip for every archive/* tag (a per-branch ls-remote made
# --all-stale over ~90 branches take minutes).
REMOTE_ARCHIVE_TAGS="$(g ls-remote --tags "$REMOTE" 'refs/tags/archive/*' 2>/dev/null || true)"

# ── Bridge commit builder: origin/main tree + one replaced blob ─────────────
build_bridge_commit() {   # $1 = branch → prints the new commit sha
    local branch=$1 idx blob tree sha
    idx="$(g rev-parse --git-path "bridge-index-$$")"
    rm -f "$idx"
    if [ -f "$LAUNCHER_SRC" ]; then blob="$(g hash-object -w "$LAUNCHER_SRC")"
    else blob="$(g rev-parse "$LAUNCHER_SRC")"; fi
    [ -n "$blob" ] || return 1
    GIT_INDEX_FILE="$idx" g read-tree "$MAIN_TIP" || return 1
    GIT_INDEX_FILE="$idx" g update-index --cacheinfo "100644,$blob,$BRIDGE_FILE" || return 1
    tree="$(GIT_INDEX_FILE="$idx" g write-tree)" || return 1
    rm -f "$idx"
    sha="$(g commit-tree "$tree" -p "$MAIN_TIP" -m "bridge($branch): self-upgrade launcher to $MAIN_VER

origin/main $MAIN_TIP with exactly one file replaced: $BRIDGE_FILE <- $LAUNCHER.
A board on branch $branch sees $MAIN_VER as the update, its old apply installs and
runs this file, which starts the full install from main in the background.
Rollback home: tag archive/$branch (the pre-bridge tip).
Procedure: docs/deployment.md «Мост самообновления для плат < 1.0.5.75».")" || return 1
    printf '%s' "$sha"
}

# ── Per-branch plan / execution ────────────────────────────────────────────
[ "$PUSH" = 1 ] && echo "== MODE: PUSH" || echo "== MODE: DRY-RUN (nothing is pushed; add --push to execute)"
FAILS=0 DONE=0 SKIPPED=0
for branch in $TARGETS; do
    old_tip="$(printf '%s\n' "$REMOTE_HEADS" | awk -v r="refs/heads/$branch" '$2==r {print $1}')"
    if [ -z "$old_tip" ]; then echo "-- $branch: not on $REMOTE — skipped"; SKIPPED=$((SKIPPED + 1)); continue; fi
    if ! version_lt "$branch" "$MAIN_VER"; then
        echo "-- $branch: version >= main ($MAIN_VER) — not stale, skipped (a release branch in progress is never overwritten)"
        SKIPPED=$((SKIPPED + 1)); continue
    fi
    if [ "$old_tip" = "$MAIN_TIP" ]; then echo "-- $branch: tip == main — skipped"; SKIPPED=$((SKIPPED + 1)); continue; fi
    old_subject="$(g log -1 --format=%s "$old_tip" 2>/dev/null || echo '?')"
    sha="$(build_bridge_commit "$branch")" || { echo "-- $branch: FAILED to build the bridge commit"; FAILS=$((FAILS + 1)); continue; }
    tag="archive/$branch"
    tag_exists="$(printf '%s\n' "$REMOTE_ARCHIVE_TAGS" | awk -v r="refs/tags/$tag" '$2==r {print $1}')"
    echo "-- $branch"
    echo "   old tip : $old_tip  ($old_subject)"
    echo "   new sha : $sha  (bridge($branch) → $MAIN_VER)"
    if [ -n "$tag_exists" ]; then echo "   tag     : $tag already on $REMOTE at $tag_exists — kept (pre-bridge tip)"
    else echo "   tag     : git push $REMOTE $old_tip:refs/tags/$tag"; fi
    echo "   push    : git push --force-with-lease=refs/heads/$branch:$old_tip $REMOTE $sha:refs/heads/$branch"
    [ "$PUSH" = 1 ] || { DONE=$((DONE + 1)); continue; }

    if [ -z "$tag_exists" ]; then
        g push -q "$REMOTE" "$old_tip:refs/tags/$tag" || { echo "   FAIL: tag push"; FAILS=$((FAILS + 1)); continue; }
        echo "   tag pushed"
    fi
    g push -q --force-with-lease="refs/heads/$branch:$old_tip" "$REMOTE" "$sha:refs/heads/$branch" \
        || { echo "   FAIL: branch push (tip moved? re-run)"; FAILS=$((FAILS + 1)); continue; }
    # Post-verify. raw.githubusercontent answers real HTTP status codes, so
    # `curl -f` is the right probe here (and may lag the push by a few seconds).
    now_tip="$(g ls-remote --heads "$REMOTE" "refs/heads/$branch" | awk '{print $1}')"
    raw_ver=""
    for _ in 1 2 3; do
        raw_ver="$(curl -fsSL --max-time 15 "$RAW_BASE/$sha/www/network_config/VERSION" 2>/dev/null | read_version)" && [ -n "$raw_ver" ] && break
        sleep 5
    done
    if [ "$now_tip" = "$sha" ] && [ "$raw_ver" = "$MAIN_VER" ]; then
        echo "   verified: ls-remote tip == $sha, raw VERSION == $MAIN_VER"; DONE=$((DONE + 1))
    else
        echo "   FAIL verify: ls-remote tip=$now_tip raw VERSION='${raw_ver:-<none>}' (expected $sha / $MAIN_VER)"; FAILS=$((FAILS + 1))
    fi
done
echo "== branches: ok=$DONE skipped=$SKIPPED failed=$FAILS"
[ "$FAILS" -eq 0 ]
