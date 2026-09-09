#!/usr/bin/env bash
# comment-mutation-proof-exempt: behavioural over git blobs and parsed asset URLs - the verdict comes from comparing the working tree's served assets and their ?v=/&r= pairs against a git ref, not from a pinned source line; there is no line whose comment-out this gate is meant to notice (the drive-to-failure is reverting an &r= token, recorded in the header).
# cache-bust-r — a served asset that changed since the reference release state
# carries a changed `?v=`+`&r=` pair, so a board that already cached the old
# URL refetches the new bytes.
#
# WHY THIS EXISTS. `?v=<version>` busts once per release; `&r=<slug>` is the
# only bust for a change that ships under the SAME `?v=` — every commit that
# lands on a release branch bench boards already OTA'd from (they clone the
# branch). `sync-app-version.py` rewrites `?v=` and never touches `&r=`, and
# nothing checked it (audit 2026-09-08 C11/F12): a forgotten bump ships a
# stale bundle against a new backend and the page breaks only on the board
# (docs/agent-rules/sa02m-domain.md, Version discipline — the rule's home).
#
# REFERENCE STATE (the first that exists):
#   1. origin/<VERSION>     the pushed state of THIS release branch — what a
#                           board tracking the branch actually runs. The check
#                           with teeth: same `?v=`, so only `&r=` can bust.
#   2. origin/<VERSION-1>   the previous release (last component decremented)
#   3. origin/main
# A remote-tracking ref is used as-is; where one is absent (CI's depth-1
# checkout) a bounded `git fetch --depth=1` is tried unless
# CACHE_BUST_R_NO_FETCH is set. No candidate ⇒ printed as a SKIP, exit 0.
# The WORKING TREE is what is compared, never HEAD — the build beat runs on the
# Builder's uncommitted edits. HONESTY: in CI the checked-out tree is clean and
# equals the pushed origin/<VERSION>, so CI reports "0 with changed bytes" —
# non-vacuous (assets were compared) but toothless. The gate bites on the
# LOCAL build beat — unpushed edits vs the pushed branch — which is where a
# forgotten `&r=` is still cheap to fix.
#
# WHAT IS COMPARED. Every `static/...?v=X[&r=Y]` in index.html / login.html
# (src/href) and every `?v=` import specifier under static/js/**/*.js (the
# browser fetches those itself). For each asset whose git blob differs between
# the working tree and the ref, the (v, r) pair on every URL naming it must
# differ from the ref's pair for that same URL site; an asset the ref did not
# reference is new and skipped. A module-only file (imported, never
# <script>-tagged) is held to the same rule through its import specifier —
# the syncer's IMPORT_SPEC_RE keeps an `&r=` after `?v=` intact.
#
# NON-VACUOUS: unreadable VERSION, missing git, zero asset URLs parsed from the
# working tree, or zero assets comparable against the ref FAILS.
#
# Proven RED (1.0.6.40): with main.css changed on fix/recs and its index.html
# token reverted to the ref's (`&r=recs1` → `&r=audit39a`) → FAIL naming
# static/css/main.css in index.html; GREEN with the bumped token.
#
# Run: bash .ai-dev/quality/checks/cache-bust-r.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1
WEB=www/network_config

ok()   { printf 'cache-bust-r: ok    %s\n' "$1"; }
skip() { printf 'cache-bust-r: skip  %s\n' "$1"; }
fail() { printf 'cache-bust-r: FAIL  %s\n' "$1"; }

command -v git >/dev/null 2>&1 || { fail "git not available"; exit 1; }
[ -f "$WEB/VERSION" ] || { fail "missing $WEB/VERSION"; exit 1; }
VERSION=$(tr -d '\r' <"$WEB/VERSION" | grep -E '^[0-9]+(\.[0-9]+){1,3}$' | head -1 || true)
[ -n "$VERSION" ] || { fail "no version line in $WEB/VERSION"; exit 1; }
PREV=$(printf '%s' "$VERSION" | awk -F. '{ if ($NF > 0) { $NF = $NF - 1; print } }' OFS=.)
HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || true)

resolve_ref() {  # $1=branch → prints a sha or nothing
    local b=$1 sha=""
    sha=$(git rev-parse --verify -q "refs/remotes/origin/$b" 2>/dev/null || true)
    if [ -z "$sha" ] && [ -z "${CACHE_BUST_R_NO_FETCH:-}" ] && git remote get-url origin >/dev/null 2>&1; then
        if timeout 25 git fetch -q --depth=1 origin "$b" >/dev/null 2>&1; then
            sha=$(git rev-parse --verify -q FETCH_HEAD 2>/dev/null || true)
        fi
    fi
    printf '%s' "$sha"
}

# The WORKING TREE is compared, never HEAD: the build beat runs on uncommitted
# edits (the Builder does not commit), so origin/<VERSION> == HEAD still means
# "the tree may differ from what boards run". A clean tree at that ref simply
# reports 0 changed assets — non-vacuous (assets were compared), honest.
REF="" REF_NAME=""
for cand in "$VERSION" "$PREV" main; do
    [ -n "$cand" ] || continue
    sha=$(resolve_ref "$cand")
    [ -n "$sha" ] || continue
    REF="$sha"; REF_NAME="origin/$cand"
    break
done
if [ -z "$REF" ]; then
    skip "no reference state reachable (origin/$VERSION, origin/$PREV, origin/main) — &r= discipline NOT verified"
    exit 0
fi
if [ "$REF" = "$HEAD_SHA" ]; then
    printf 'cache-bust-r: note  %s is HEAD itself — only uncommitted edits can differ from it\n' "$REF_NAME"
fi

py=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then py="$p"; break; fi
done
[ -n "$py" ] || { fail "no working python interpreter"; exit 1; }

PYTHONIOENCODING=utf-8 "$py" - "$REF" "$REF_NAME" "$WEB" <<'PY'
import re, subprocess, sys
from pathlib import Path

ref, ref_name, web = sys.argv[1], sys.argv[2], sys.argv[3]
fails = 0
def ok(m):   print(f"cache-bust-r: ok    {m}")
def note(m): print(f"cache-bust-r: note  {m}")
def fail(m):
    global fails; fails += 1; print(f"cache-bust-r: FAIL  {m}")

def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout

HTML_RE = re.compile(r'(?:src|href)="(static/[^"?]+)\?v=([^"&]*)(?:&r=([^"&]*))?"')
IMPORT_RE = re.compile(r"""(?:^|\s)(?:import|export)\b[^'"\n]*?(['"])(\./[^'"?]+\.js)\?v=([^'"&]*)(?:&r=([^'"&]*))?\1""", re.M)

def parse_html(text, site):
    out = {}
    for m in HTML_RE.finditer(text):
        out[(site, m.group(1))] = (m.group(2), m.group(3) or "")
    return out

def parse_imports(text, site, jsdir):
    out = {}
    for m in IMPORT_RE.finditer(text):
        asset = (Path(jsdir) / m.group(2)).as_posix()  # relative to web root
        out[(site, asset)] = (m.group(3), m.group(4) or "")
    return out

def ref_text(path):
    rc, out = git("show", f"{ref}:{path}")
    return out if rc == 0 else None

def ref_blob(path):
    rc, out = git("rev-parse", "--verify", "-q", f"{ref}:{path}")
    return out.strip() if rc == 0 else None

def tree_blob(path):
    rc, out = git("hash-object", path)
    return out.strip() if rc == 0 else None

cur, old = {}, {}
for name in ("index.html", "login.html"):
    p = f"{web}/{name}"
    t = Path(p).read_text(encoding="utf-8", errors="replace") if Path(p).is_file() else ""
    cur.update(parse_html(t, name))
    r = ref_text(p)
    if r is not None:
        old.update(parse_html(r, name))
jsroot = Path(web) / "static" / "js"
for js in sorted(jsroot.rglob("*.js")):
    rel = js.relative_to(web).as_posix()
    jsdir = js.parent.relative_to(web).as_posix()
    cur.update(parse_imports(js.read_text(encoding="utf-8", errors="replace"), rel, jsdir))
    r = ref_text(f"{web}/{rel}")
    if r is not None:
        old.update(parse_imports(r, rel, jsdir))

if not cur:
    fail("zero asset URLs parsed from the working tree — the sweep is vacuous")
    sys.exit(1)

compared = changed = 0
for (site, asset), pair in sorted(cur.items()):
    path = f"{web}/{asset}"
    if not Path(path).is_file():
        fail(f"{site} references {asset} which does not exist in the tree")
        continue
    ob, tb = ref_blob(path), tree_blob(path)
    if ob is None or tb is None:
        continue  # new asset (or unreadable) — nothing to compare against
    compared += 1
    if ob == tb:
        continue
    changed += 1
    oldpair = old.get((site, asset))
    if oldpair is None:
        note(f"{asset} changed but {site} did not reference it at {ref_name} — no old URL to bust")
        continue
    if pair == oldpair:
        fail(f"{asset} changed since {ref_name} but {site} still serves it as ?v={pair[0]}&r={pair[1] or '-'} — bump &r=")
    elif pair[0] != oldpair[0]:
        ok(f"{asset} changed — busted by ?v= ({oldpair[0]} → {pair[0]}) in {site}")
    else:
        ok(f"{asset} changed — busted by &r= ({oldpair[1] or '-'} → {pair[1]}) in {site}")

if compared == 0:
    fail(f"no served asset comparable against {ref_name} — the reference tree has none of them (non-vacuity)")
else:
    ok(f"{compared} asset URL(s) compared against {ref_name}, {changed} with changed bytes")
if fails:
    print(f"cache-bust-r: {fails} FAILURE(S)")
    sys.exit(1)
print("cache-bust-r: ALL OK")
PY
