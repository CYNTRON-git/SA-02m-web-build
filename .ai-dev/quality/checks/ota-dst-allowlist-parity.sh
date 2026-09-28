#!/usr/bin/env bash
# ota-dst-allowlist-parity — every home of the OTA destination allow-list admits
# the SAME set of live paths.
#
# WHY. Which live path an update may write is decided in four places: the
# update runner twice (its GitHub manifest builder and its offline-package
# extract, both `DST_RE`), the offline packer (`DST_PREFIX_RE`) and the on-board
# package validator (`_DST_PREFIX_RES`). They are copies, so they drift, and a
# drift toward WIDER is a write-anywhere hole in a root process: at 1.0.6.57 the
# runner admitted ANY file under /opt/mplc4/ (the live MPLC runtime tree) while
# the packer and the validator admitted only the two shipped plugins, and the
# packer's plugin alternative lacked its `$`, so `mplc_cyntron.so.bak` passed it
# (audit 2026-09-24 L2). A fifth copy — `dst_prefix_allowlist` in
# scripts/offline-update-deploy-map.json — was read by nothing and had drifted
# too (`/etc/default/` for `/etc/default/sa02m-`, no dhcp hook); it is gone, and
# this gate fails if it grows back. Collapsing the homes into one module is the
# runner-decomposition seam (backlog «Decomposition worklist»); until then this
# row is what keeps them equal.
#
# METHOD. Each home is read as PYTHON, not grepped: the runner's two heredoc
# blocks and the two .py files are parsed with `ast`, so a commented-out
# alternative disappears from the value exactly as it does at run time (no
# comment-blindness by construction). Two checks per home:
#   1. SET PARITY — the runner's and the packer's one alternation is split at its
#      top-level `|`; the validator's tuple is one regex per alternative. The
#      normalised alternative sets must be identical across all four homes.
#   2. PROBES — every home's COMPILED predicate must give the pinned verdict on
#      a table of live paths: each allowed root accepted, and the near misses
#      (another /opt/mplc4 file, a plugin with a suffix, /etc/default/grub,
#      /usr/local/bin, the dhcp hook with a suffix …) refused. Parity alone would
#      pass four homes that drifted wide together; the probes pin the safe set.
# NON-VACUOUS: exactly two runner blocks, >=10 alternatives per home, and every
# home answering every probe — a home that stops parsing FAILS.
# Mutation cases (comment-mutation-proof): one alternative line commented out in
# the validator, the packer and the runner each turns this row RED.
#
# Run: bash .ai-dev/quality/checks/ota-dst-allowlist-parity.sh
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

PY=""
for p in python3 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "ota-dst-allowlist-parity: FAIL  no working python interpreter"; exit 1; }

PYTHONIOENCODING=utf-8 "$PY" - <<'PY'
import ast, json, re, sys
from pathlib import Path

ROW = "ota-dst-allowlist-parity"
fails = 0
def ok(m):
    print(f"{ROW}: ok    {m}")
def bad(m):
    global fails
    fails += 1
    print(f"{ROW}: FAIL  {m}")

RUNNER = "etc/sa02m-update-runner.sh"
PACKER = "scripts/pack-offline-update.py"
VALIDATOR = "opt/sa02m-update/lib/validate_package.py"
DEPLOY_MAP = "scripts/offline-update-deploy-map.json"

def read(p):
    return Path(p).read_text(encoding="utf-8")

def const_str(node, where):
    try:
        v = ast.literal_eval(node)
    except Exception as e:
        raise ValueError(f"{where}: not a string literal ({e})")
    if not isinstance(v, str):
        raise ValueError(f"{where}: not a string literal")
    return v

def split_alternation(pat, where):
    """'^/(a|b(c|d)|e$)' -> ['a', 'b(c|d)', 'e$'] (top-level alternatives)."""
    if not (pat.startswith("^/(") and pat.endswith(")")):
        raise ValueError(f"{where}: expected the shape ^/(alt|alt|...), got {pat[:40]!r}")
    body = pat[3:-1]
    alts, depth, cur, i = [], 0, "", 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            cur += body[i:i + 2]; i += 2; continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"{where}: unbalanced parenthesis")
        if c == "|" and depth == 0:
            alts.append(cur); cur = ""
        else:
            cur += c
        i += 1
    if depth != 0:
        raise ValueError(f"{where}: unbalanced parenthesis")
    alts.append(cur)
    return alts

homes = {}   # name -> (alternatives list, predicate)

# ── 1. the runner: every `DST_RE = re.compile(` … `)` python block ──────────
text = read(RUNNER).splitlines()
blocks, i = [], 0
while i < len(text):
    if text[i].rstrip() == "DST_RE = re.compile(":
        j = i + 1
        while j < len(text) and text[j].rstrip() != ")":
            j += 1
        blocks.append((i + 1, "\n".join(text[i:j + 1])))
        i = j
    i += 1
if len(blocks) != 2:
    bad(f"{RUNNER}: expected exactly 2 `DST_RE = re.compile(` blocks (manifest builder + package extract), found {len(blocks)}")
for n, (lineno, src) in enumerate(blocks, 1):
    where = f"{RUNNER}:{lineno} (DST_RE #{n})"
    try:
        call = ast.parse(src).body[0].value
        pat = const_str(call.args[0], where)
        homes[f"runner DST_RE #{n}"] = (split_alternation(pat, where), re.compile(pat).match)
    except Exception as e:
        bad(f"{where}: cannot read the allow-list: {e}")

# ── 2. the packer: DST_PREFIX_RE ────────────────────────────────────────────
try:
    tree = ast.parse(read(PACKER))
    found = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "DST_PREFIX_RE" for t in n.targets)]
    if len(found) != 1:
        raise ValueError(f"expected one DST_PREFIX_RE assignment, found {len(found)}")
    pat = const_str(found[0].value.args[0], PACKER)
    homes["packer DST_PREFIX_RE"] = (split_alternation(pat, PACKER), re.compile(pat).match)
except Exception as e:
    bad(f"{PACKER}: cannot read the allow-list: {e}")

# ── 3. the validator: _DST_PREFIX_RES (one regex per alternative) ──────────
try:
    tree = ast.parse(read(VALIDATOR))
    found = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "_DST_PREFIX_RES" for t in n.targets)]
    if len(found) != 1:
        raise ValueError(f"expected one _DST_PREFIX_RES assignment, found {len(found)}")
    pats = [const_str(c.args[0], VALIDATOR) for c in found[0].value.elts]
    alts = []
    for p in pats:
        if not p.startswith("^/"):
            raise ValueError(f"pattern {p!r} is not anchored at ^/")
        alts.append(p[2:])
    compiled = [re.compile(p) for p in pats]
    homes["validator _DST_PREFIX_RES"] = (alts, lambda d, _c=compiled: any(r.match(d) for r in _c))
except Exception as e:
    bad(f"{VALIDATOR}: cannot read the allow-list: {e}")

# ── 4. no fifth home in the deploy map ──────────────────────────────────────
try:
    dm = json.loads(read(DEPLOY_MAP))
    if "dst_prefix_allowlist" in dm:
        bad(f"{DEPLOY_MAP}: carries `dst_prefix_allowlist` again — a fifth copy of the allow-list nothing reads; delete it")
    else:
        ok(f"{DEPLOY_MAP}: carries no copy of the destination allow-list")
except Exception as e:
    bad(f"{DEPLOY_MAP}: unreadable: {e}")

# ── set parity ──────────────────────────────────────────────────────────────
for name, (alts, _) in homes.items():
    if len(alts) < 10:
        bad(f"{name}: only {len(alts)} alternative(s) — the read is broken, not the list short (expected >=10)")
if len(homes) == 4:
    ref_name = "validator _DST_PREFIX_RES"
    ref = set(homes[ref_name][0])
    for name, (alts, _) in homes.items():
        if len(set(alts)) != len(alts):
            bad(f"{name}: a duplicated alternative")
        s = set(alts)
        if name == ref_name:
            ok(f"{name}: the reference set, {len(s)} alternatives")
        elif s == ref:
            ok(f"{name}: the same {len(s)} alternatives as the validator")
        else:
            for a in sorted(s - ref):
                bad(f"{name} admits `{a}` which the validator does not")
            for a in sorted(ref - s):
                bad(f"{name} lacks `{a}` which the validator has")
else:
    bad(f"only {len(homes)} of 4 homes were read — parity cannot be judged")

# ── probes: the pinned safe set ─────────────────────────────────────────────
ACCEPT = [
    "/var/www/network_config/index.html",
    "/usr/local/sbin/sa02m-web-service-ctl.sh",
    "/usr/local/lib/sa02m-web-auth-lib.sh",
    "/usr/local/libexec/sa02m-update-runner",
    "/opt/sa02m-flasher/sa02m_flasher/service.py",
    "/opt/mplc4/mplc_cyntron.so",
    "/opt/mplc4/mplc_protocol_fast_modbus.so",
    "/etc/systemd/system/sa02m-flasher.service",
    "/etc/nginx/sites-available/network_config",
    "/etc/tmpfiles.d/sa02m.conf",
    "/etc/sudoers.d/sa02m-www",
    "/etc/default/sa02m-watchdog",
    "/etc/sa02m-update/trusted-keys/release.pem",
    "/etc/dhcp/dhclient-exit-hooks.d/eth1-default-route",
]
REFUSE = [
    "/opt/mplc4/start_mplc4.sh",
    "/opt/mplc4/other_plugin.so",
    "/opt/mplc4/mplc_cyntron.so.bak",
    "/opt/mplc4/sub/mplc_cyntron.so",
    "/etc/default/grub",
    "/etc/passwd",
    "/etc/systemd/system/getty@.service",
    "/usr/local/bin/anything",
    "/opt/other/x",
    "/var/www/html/index.html",
    "/etc/dhcp/dhclient-exit-hooks.d/eth1-default-route.bak",
    "/etc/sa02m_web.conf",
]
n_probe = 0
for name, (_, pred) in homes.items():
    for d in ACCEPT:
        n_probe += 1
        if not pred(d):
            bad(f"{name} refuses {d} — an allowed root is missing")
    for d in REFUSE:
        n_probe += 1
        if pred(d):
            bad(f"{name} ADMITS {d} — wider than the safe set")
if n_probe < 4 * (len(ACCEPT) + len(REFUSE)):
    bad(f"only {n_probe} probe verdicts — a home did not answer every probe")
else:
    ok(f"{n_probe} probe verdicts ({len(ACCEPT)} accept + {len(REFUSE)} refuse per home, 4 homes)")

if fails:
    print(f"{ROW}: {fails} FAILURE(S)")
    sys.exit(1)
print(f"{ROW}: ALL OK — the 4 homes admit the same destination set")
PY
