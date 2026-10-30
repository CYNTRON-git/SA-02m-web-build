#!/usr/bin/env bash
# fix-eth-ota-delivery — etc/fix-eth.sh reaches /usr/local/bin/fix-eth.sh by
# EVERY update path, atomically.
#
# WHY. The 1.0.7.12 late-cable fix lives in etc/fix-eth.sh and lands at
# /usr/local/bin/fix-eth.sh. Neither update path delivered it: the runner's
# map_dst() returned None for the file, all four homes of the destination
# allow-list refused /usr/local/bin/, the offline allow-list and deploy-map did
# not name it, and update-www-only.sh did not install it — so a board updated
# only by web OTA or a signed pack kept the 1.0.7.11 watchdog forever
# (CHANGELOG 1.0.7.12 «Как попадает на платы», BUGLOG 2026-10-28). This row
# pins the delivery chain so the gap cannot silently reopen.
#
# METHOD. Five checks, each against the file as it is handed back:
#   (a) the offline allow-list names `etc/fix-eth.sh` on a live (comment-
#       stripped, lib_check.sh) line;
#   (b) the deploy-map maps it to /usr/local/bin/fix-eth.sh, 0755, root:root;
#   (c) the runner, exec'd — its map_dst slice (MPLC_OTA_PLUGINS … def
#       render_nginx_template, the install-profile P8 idiom) returns that dst,
#       BOTH `DST_RE = re.compile(` blocks (parsed the way ota-dst-allowlist-
#       parity parses them) accept it and still refuse /usr/local/bin/anything,
#       and deploy_mode() answers 0755 for it;
#   (d) the packer, imported: map_deploy_entry() → that dst at 0755; its
#       DST_PREFIX_RE accepts it; build_deploy_list() with a min_updater BELOW
#       FIX_ETH_OTA_SINCE drops the entry and names it on stderr, AT the tier
#       keeps it; FIX_ETH_OTA_SINCE == 1.0.7.19 (the first runner whose
#       allow-list admits the path — an older board fails the whole pack with
#       `dst not allowlisted`, so the packer must not ship it below the tier);
#   (e) scripts/02-network.sh and scripts/update-www-only.sh land the file
#       through `sa02m_atomic_install -m 755` — a bare `install -m` on that
#       live path (truncate-then-fill; a reset in between leaves a 0-byte
#       script the net-watchdog execs every 30 s) FAILS.
# NON-VACUOUS: a missing file, a runner slice that does not parse or exec,
# fewer than two DST_RE blocks, or a packer that does not import — all FAIL.
# RED observed 2026-10-29 on the 1.0.7.12 tree — 10 failures: allow-list,
# deploy-map, map_dst None, DST_RE #1 and #2 refusing, packer map None,
# DST_PREFIX_RE refusing, FIX_ETH_OTA_SINCE absent, build_deploy_list without
# min_updater, update-www-only not installing; GREEN after the change.
# Mutation cases (comment-mutation-proof): the allow-list line, the runner
# `if rel == "etc/fix-eth.sh":` line, the update-www-only install line.
#
# Run: bash .ai-dev/quality/checks/fix-eth-ota-delivery.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/lib_check.sh" || { echo "fix-eth-ota-delivery: cannot source lib_check.sh"; exit 1; }

ROW=fix-eth-ota-delivery
fails=0
fail() { printf '%s: FAIL  %s\n' "$ROW" "$*"; fails=$((fails + 1)); }
pass() { printf '%s: ok    %s\n' "$ROW" "$*"; }

SRC=etc/fix-eth.sh
DST=/usr/local/bin/fix-eth.sh
ALLOW=scripts/offline-update-allowlist.txt
DMAP=scripts/offline-update-deploy-map.json
RUNNER=etc/sa02m-update-runner.sh
PACKER=scripts/pack-offline-update.py
WWW_ONLY=scripts/update-www-only.sh
NET=scripts/02-network.sh

for f in "$SRC" "$ALLOW" "$DMAP" "$RUNNER" "$PACKER" "$WWW_ONLY" "$NET"; do
    [ -f "$f" ] || fail "$f is missing — nothing below can be judged"
done
[ "$fails" -eq 0 ] || { printf '%s: %s check(s) failed\n' "$ROW" "$fails"; exit 1; }

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { fail "no working python interpreter"; printf '%s: %s check(s) failed\n' "$ROW" "$fails"; exit 1; }

# ── (a) the offline allow-list names the source on a live line ──────────────
if stripped_matches "$ALLOW" "^${SRC//./\\.}[[:space:]]*$"; then
    pass "(a) $ALLOW names $SRC on a live line"
else
    fail "(a) $ALLOW does not name $SRC (or only on a commented line) — the signed pack never carries it"
fi

# ── (b)(c)(d) deploy-map, runner slices, packer ─────────────────────────────
# The live paths are literals INSIDE the python block, never env vars: git-bash
# on Windows rewrites an absolute POSIX path handed to a native interpreter
# (`/usr/local/bin/x` -> `C:/Program Files/Git/usr/local/bin/x`), which would
# turn every verdict below RED for a reason unrelated to the pin.
py_out=$(SRC="$SRC" DMAP="$DMAP" RUNNER="$RUNNER" PACKER="$PACKER" ROW="$ROW" \
    PYTHONIOENCODING=utf-8 "$PY" - <<'PY' 2>&1
import ast, contextlib, importlib.util, io, json, os, re, sys
from pathlib import Path

ROW = os.environ["ROW"]
SRC = os.environ["SRC"]
DMAP, RUNNER, PACKER = os.environ["DMAP"], os.environ["RUNNER"], os.environ["PACKER"]
DST = "/usr/local/bin/fix-eth.sh"
NEAR = "/usr/local/bin/anything"
TIER = "1.0.7.19"
fails = 0
def ok(m): print(f"{ROW}: ok    {m}")
def bad(m):
    global fails; fails += 1
    print(f"{ROW}: FAIL  {m}")

# (b) deploy-map
try:
    dm = json.loads(Path(DMAP).read_text(encoding="utf-8"))
    rule = (dm.get("etc_helper_renames") or {}).get(SRC) or (dm.get("exact_rules") or {}).get(SRC)
    if not rule:
        bad(f"(b) {DMAP} has no rule for {SRC}")
    else:
        got = (rule.get("dst"), rule.get("mode"), rule.get("owner"))
        if got == (DST, "0755", "root:root"):
            ok(f"(b) {DMAP} maps {SRC} -> {DST} 0755 root:root")
        else:
            bad(f"(b) {DMAP} maps {SRC} -> {got}, want ({DST!r}, '0755', 'root:root')")
except Exception as e:
    bad(f"(b) {DMAP} unreadable: {e}")

# (c) the runner: map_dst slice, both DST_RE blocks, deploy_mode slice
try:
    src = Path(RUNNER).read_text(encoding="utf-8")
    a = src.index("MPLC_OTA_PLUGINS = frozenset(")
    b = src.index("def render_nginx_template(", a)
    ns = {"Path": Path, "re": re}
    exec(src[a:b], ns)
    got = ns["map_dst"](SRC)
    if got == DST:
        ok(f"(c) runner map_dst({SRC!r}) -> {DST}")
    else:
        bad(f"(c) runner map_dst({SRC!r}) -> {got!r}, want {DST!r} — web OTA skips the file")
except Exception as e:
    bad(f"(c) runner map_dst slice does not exec: {e}")

try:
    lines = src.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        if lines[i].rstrip() == "DST_RE = re.compile(":
            j = i + 1
            while j < len(lines) and lines[j].rstrip() != ")":
                j += 1
            blocks.append((i + 1, "\n".join(lines[i:j + 1])))
            i = j
        i += 1
    if len(blocks) != 2:
        bad(f"(c) runner: expected exactly 2 `DST_RE = re.compile(` blocks, found {len(blocks)}")
    for n, (lineno, block) in enumerate(blocks, 1):
        pat = ast.literal_eval(ast.parse(block).body[0].value.args[0])
        rx = re.compile(pat)
        if not rx.match(DST):
            bad(f"(c) runner DST_RE #{n} (line {lineno}) refuses {DST} — the update fails `dst not allowlisted`")
        elif rx.match(NEAR):
            bad(f"(c) runner DST_RE #{n} (line {lineno}) ADMITS {NEAR} — wider than the exact path")
        else:
            ok(f"(c) runner DST_RE #{n} (line {lineno}) admits {DST}, refuses {NEAR}")
except Exception as e:
    bad(f"(c) runner DST_RE blocks do not parse: {e}")

try:
    m = re.search(r"^def deploy_mode\(.*?(?=^deploy = \[\])", src, re.M | re.S)
    if not m:
        raise ValueError("deploy_mode body not found (def deploy_mode … deploy = [])")
    ns2 = {}
    exec(m.group(0), ns2)
    mode = ns2["deploy_mode"](SRC, DST)
    if mode == "0755":
        ok(f"(c) runner deploy_mode({SRC!r}, {DST!r}) -> 0755")
    else:
        bad(f"(c) runner deploy_mode({SRC!r}, {DST!r}) -> {mode!r}, want '0755'")
except Exception as e:
    bad(f"(c) runner deploy_mode slice does not exec: {e}")

# (d) the packer
try:
    spec = importlib.util.spec_from_file_location("packer", PACKER)
    pk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pk)
except Exception as e:
    bad(f"(d) {PACKER} does not import: {e}")
    pk = None
if pk is not None:
    try:
        dm = pk.load_deploy_map()
        e = pk.map_deploy_entry(SRC, dm)
        if e and e.get("dst") == DST and e.get("mode") == "0755":
            ok(f"(d) packer map_deploy_entry({SRC!r}) -> {DST} 0755")
        else:
            bad(f"(d) packer map_deploy_entry({SRC!r}) -> {e!r}, want dst {DST} mode 0755")
        if pk.DST_PREFIX_RE.match(DST) and not pk.DST_PREFIX_RE.match(NEAR):
            ok(f"(d) packer DST_PREFIX_RE admits {DST}, refuses {NEAR}")
        else:
            bad(f"(d) packer DST_PREFIX_RE: admits {DST}={bool(pk.DST_PREFIX_RE.match(DST))}, "
                f"admits {NEAR}={bool(pk.DST_PREFIX_RE.match(NEAR))}")
        since = getattr(pk, "FIX_ETH_OTA_SINCE", None)
        if since == TIER:
            ok(f"(d) packer FIX_ETH_OTA_SINCE == {TIER}")
        else:
            bad(f"(d) packer FIX_ETH_OTA_SINCE is {since!r}, want {TIER!r}")
        files = [SRC, "www/network_config/VERSION"]
        prev = "1.0.7.18"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            below = pk.build_deploy_list(files, dm, min_updater=prev)
        below_dsts = [d["dst"] for d in below]
        note = err.getvalue()
        if DST in below_dsts:
            bad(f"(d) packer build_deploy_list(min_updater={prev}) ships {DST} — an updater < {TIER} fails the whole pack on it")
        elif SRC not in note or TIER not in note:
            bad(f"(d) packer build_deploy_list(min_updater={prev}) drops {SRC} silently — stderr must name the file and {TIER}; got: {note.strip()!r}")
        else:
            ok(f"(d) packer build_deploy_list(min_updater={prev}) drops {SRC} and names it + {TIER} on stderr")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            at = pk.build_deploy_list(files, dm, min_updater=TIER)
        if DST in [d["dst"] for d in at]:
            ok(f"(d) packer build_deploy_list(min_updater={TIER}) ships {DST}")
        else:
            bad(f"(d) packer build_deploy_list(min_updater={TIER}) does not ship {DST}")
    except SystemExit as e:
        bad(f"(d) packer died (SystemExit {e.code}) — the packer's own allow-list or map refuses {DST}")
    except Exception as e:
        bad(f"(d) packer checks raised: {type(e).__name__}: {e}")

sys.exit(1 if fails else 0)
PY
)
py_rc=$?
printf '%s\n' "$py_out"
if [ "$py_rc" -ne 0 ]; then
    n=$(grep -c ': FAIL  ' <<< "$py_out")
    [ "$n" -gt 0 ] || n=1
    fails=$((fails + n))
fi

# ── (e) the installer and the dev deploy helper land it atomically ──────────
ATOMIC_NET='sa02m_atomic_install -m 755 "$ETC_DIR/fix-eth.sh" /usr/local/bin/fix-eth.sh'
ATOMIC_WWW='sa02m_atomic_install -m 755 "$ETC_REPO/fix-eth.sh" /usr/local/bin/fix-eth.sh'
BARE='(^|[[:space:]])install[[:space:]]+-m[[:space:]]+[0-9]+[[:space:]].*/usr/local/bin/fix-eth\.sh'
if stripped_has "$NET" "$ATOMIC_NET"; then
    pass "(e) $NET installs $DST through sa02m_atomic_install -m 755"
else
    fail "(e) $NET no longer installs $DST through sa02m_atomic_install -m 755"
fi
if stripped_has "$WWW_ONLY" "$ATOMIC_WWW"; then
    pass "(e) $WWW_ONLY installs $DST through sa02m_atomic_install -m 755"
else
    fail "(e) $WWW_ONLY does not install $DST through sa02m_atomic_install -m 755 — a www-only deploy leaves the old watchdog"
fi
for f in "$NET" "$WWW_ONLY"; do
    if stripped_matches "$f" "$BARE"; then
        fail "(e) $f writes $DST with a bare install -m (truncate-then-fill on a live path)"
    else
        pass "(e) $f has no bare install -m on $DST"
    fi
done

[ "$fails" = 0 ] || printf '%s: %s check(s) failed\n' "$ROW" "$fails"
exit $(( fails > 0 ))  # never the count: 77 reads SKIP, 256 reads PASS (run.mjs SKIP_EXIT)
