#!/usr/bin/env python3
"""test-alice-conf-homes.py — every preserve / backup / restore list names the
LIVE Alice conf layout, the HomeKit bridge's conf and pairing store (section
10) and the Home Connect client's conf and token store (section 13) where each
belongs. Quality row `alice-conf-homes`.

Why it exists: the Alice confs live in /etc/sa02m-alice/ (code home
opt/sa02m-alice/sa02m_alice/common/constants.py ETC_DIR, created by
etc/tmpfiles.d/sa02m-alice.conf, filled by scripts/06-alice.sh), but every list
outside the package still named the older flat layout
(/etc/sa02m-alice-client.conf, /etc/sa02m-alice-devices.conf):

  * the OTA never-deploy guards — etc/sa02m-update-runner.sh PRESERVE_PATHS
    (bash, documentation-only: SC2034) and its bootstrap PRESERVE_PREFIXES
    (enforced), opt/sa02m-update/lib/validate_package.py PRESERVE_PATHS
    (enforced), scripts/offline-update-deploy-map.json never_deploy — guarded a
    layout no current board has. Latent: DST_RE does not admit /etc/sa02m-alice/
    either, so the guard was the second line, not the only one;
  * the user backup (etc/sa02m-web-backup.sh) silently left the live Alice
    rooms/devices/client confs out of every downloaded archive — the policy in
    docs/ALICE_INTEGRATION.md says «включить conf»;
  * the SSH restore (etc/sa02m-restore-backup.sh) would have rejected a fixed
    backup AS A WHOLE («path not allowlisted»), and chowned every restored file
    root:root, which locks the CGI (www-data) out of root:www-data files —
    /etc/sa02m_web.env today, the Alice confs as soon as they are restored.

The flat names stay in every list (the older layout; factory reset still
handles both — etc/sa02m-factory-reset-runner.sh), and this row asserts that
too, so a later cleanup cannot silently drop an old board's coverage.

Method — behavioural where the code can be run, never a grep for a string:
  0. the live layout is READ from its code home (constants imported in a clean
     env) and cross-checked against tmpfiles and the installer (non-vacuity);
  1. validate_package.path_is_preserved() is imported and called;
  2. the runner's bootstrap tuple AND its shipped predicate line are extracted
     from the heredoc and evaluated;
  3. the runner's bash array is expanded by bash itself (set -f);
  4. the deploy map's never_deploy is loaded as JSON;
  5. the SHIPPED collect_paths() of the backup is extracted, retargeted at a
     seeded sandbox root and run; 5e runs the WHOLE backup script the same way
     (its conf dir group-writable like the board's): the installer's root-made nginx
     link is archived through to its target, while a symlink, a FIFO or a hard
     link planted at a live Alice conf, and a symlinked conf directory (or any
     directory) in a parent others can write, are skipped with a WARN, the
     backup exits 0, and a planted victim's bytes are nowhere in the stream;
  6. the restore's ALLOW list + allowed() are extracted and exec'd, and every
     path the backup of step 5 emits must pass it (backup/restore agreement);
  7. 7t: the backup and the restore carry one trusted-path resolver (follow a
     symlink or descend a directory only where nobody but root could have made
     or replaced it) as byte-identical twins. The restore's atomic_install()
     and its helpers are extracted and run: an existing file keeps its owner, a
     new one takes root + its directory's group (7a/7b); a symlink planted at
     the temp name (the exact `<dest>.tmp.<pid>` the first owner-keeping
     version used, plus neighbours) is never followed, and a symlinked dest, a
     non-regular dest and a symlinked parent in a directory others can write
     are refused, each leaving a victim byte- and mode-identical (7c-7g). A
     missing conf dir is created with the tmpfiles owner/mode (7h; 7s
     cross-checks that spec against tmpfiles and the installer). Only 7a/7b/7h
     observe an owner and need root; a non-root host reports them as SKIP,
     never a pass, and runs everything else — CI runs this row non-root;
  8. the WHOLE restore script runs end to end with /etc/ retargeted into a
     sandbox and systemctl/nginx shimmed: a planted symlinked dest fails
     --apply in the preflight with nothing written (not even the archive's
     other files); a clean --apply restarts only the ACTIVE Alice units; a
     restore without Alice confs touches none; the installer's root-made
     /etc/nginx/sites-enabled link validates and is written through (8d);
  9. the installer's conf mode/group block (scripts/06-alice.sh, between the
     conf-install loop and the systemd block) runs with the dir retargeted and
     www-data replaced by the invoking group: regular confs get their modes; a
     planted symlink or hard link is reported and its target left unchanged.
  10. the HomeKit bridge homes (docs/contracts/homekit-bridge.md §13-§14),
     read from sa02m_homekit.constants and cross-checked against its tmpfiles:
     the conf and the pairing store (state.json and a `.hk-` sidecar) are
     preserved by all four OTA guards; the backup archives the conf and NEVER
     the pairing store (P4 — asserted on the listed paths and on the raw
     stream of the whole script, whose /var/lib/ is retargeted too), and skips
     a symlink planted at the conf; the restore admits the conf, refuses the
     pairing store, creates a missing conf dir with the tmpfiles owner/mode,
     restores the conf without touching a HomeKit unit (the daemon re-reads it
     every 2 s) and fails the preflight on a symlinked conf; the runner's 0440
     re-mode loop names every committed etc/sudoers.d/ drop-in (open world).
  11/12. the HomeKit installer's conf seed and its tmpfiles conf (see those
     sections).
  13. the Home Connect client homes (docs/contracts/home-connect.md §11-§12),
     read from sa02m_homeconnect.constants and cross-checked against its
     tmpfiles: the conf, tokens.json, budget.json and a `.hc-` sidecar are
     preserved by all four OTA guards; the backup archives the conf and NEVER
     the token store (P4 — listed paths and the raw stream of the whole
     script); the restore admits the conf, refuses the token store, creates a
     missing conf dir with the tmpfiles owner/mode, touches no Home Connect
     unit (the client re-reads its conf every 2 s) and fails the preflight on
     a symlinked conf. 14: the 06d conf seed — rendered by the installed
     package itself — never follows a plant and runs isolated (`python3 -I`);
     an unrenderable package leaves no empty conf. 15: the Home Connect
     tmpfiles conf reaches /etc/tmpfiles.d/ only through 06d.
Negative controls: /opt/sa02m-alice/… (the code tree OTA DOES deploy) must not
be preserved by any guard; /etc/shadow must not pass the restore; the HomeKit
code tree stays deployable.

RED observed 2026-09-27 on 9c4355d (1.0.6.54): 1–6 FAIL for both live confs in
every home, 7a FAIL (owner 0:0 instead of 1234:4321), 7b FAIL (gid 0 instead of
4242). The symlink cases RED on the first owner-keeping restore (review B1):
7c victim 0600 0:0 → 0660 1234:4321 with the archive's bytes, 7d/7e/7f not
refused (7f wrote through the parent), 7s/7h no directory spec, 8a rc=0 with
the victim rewritten, 8b no Alice restart — 9 FAIL. Review round 2, on the
pre-round-2 tree: 5e2/5e4/5e5 streamed the planted victim's bytes in the
archive, 5e3 dropped the FIFO silently (no WARN), 8d refused the nginx link in
--dry-run and --apply (every board's own backup unrestorable), 9b/9c chmod'd
the victim 0600 → 0660. Mutation cases in comment-mutation-proof, each RED in
a non-root run: the validator entry (1), the runner bootstrap entry (2), the
restore ALLOW entry (6, 8a/8b), the restore's refusal line (7d/7e/7f), its
DIR_SPEC entry (7s), the resolver's symlink rule in each twin (5e2/5e5 resp.
7d/7f/8a, plus 7t), the backup's directory rule (5e5), regular-file rule
(5e3) and hard-link rule (5e4), and the installer's regular-file rule (9c).
Section 10, RED observed 2026-09-27 against the branch's own pre-HomeKit lists
(HEAD 485f385 for the runner, validator, deploy map, pack, backup, restore): 21
FAIL — every guard in 10.1-10.4 for all three paths, 10.5/10.5e1/10.5e2 (no
conf in the archive, no WARN), 10.6 (conf refused), 10.7 (no DIR_SPEC), 10.8a/b
(«path not allowlisted»), 10.9 (sa02m-homekit not re-moded). Its mutation
cases (validator and runner-bootstrap entries, restore ALLOW entry, DIR_SPEC
entry) each RED as root and as nobody; adding the pairing store to the backup
list turns 10.5/10.5e1/10.6 RED.
Sections 13-15, RED observed 2026-09-28 with the runner, validator, deploy
map, backup and restore at HEAD d7d9c4a: 25 FAIL (every 13.1-13.8 guard, plus
10.9's open-world sweep naming the un-re-moded sa02m-homeconnect drop-in).
14f RED on a bare `python3 -B -` seed (the planted sitecustomize ran); 14c/14d
RED on a seed opening the conf without O_NOFOLLOW / the nlink check. Mutation
cases (each RED, one at a time, with the proof's own awk): the validator entry
(13.1), the runner-bootstrap entry (13.2), the restore ALLOW entry
(13.6/13.8a/b), the restore DIR_SPEC entry (13.7), and the 06d seed's
`os.fchmod(fd, 0o660)` (14a/14b/14f).
The backup list is not cased: its entries sit inside a backslash-continued
`for p in \\` list, where a `#` breaks the syntax of the whole loop — a RED for
the wrong reason.

Run: python3 scripts/dev/test-alice-conf-homes.py   (python3 + bash)
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

if os.name == "nt":
    print("SKIP  alice-conf-homes retargets Linux paths and needs a symlink-capable host")
    raise SystemExit(77)

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "etc/sa02m-update-runner.sh"
VALIDATOR_DIR = ROOT / "opt/sa02m-update"
DEPLOY_MAP = ROOT / "scripts/offline-update-deploy-map.json"
BACKUP = ROOT / "etc/sa02m-web-backup.sh"
RESTORE = ROOT / "etc/sa02m-restore-backup.sh"
ALICE_PKG = ROOT / "opt/sa02m-alice"
TMPFILES = ROOT / "etc/tmpfiles.d/sa02m-alice.conf"
INSTALLER = ROOT / "scripts/06-alice.sh"

LEGACY = ["/etc/sa02m-alice-client.conf", "/etc/sa02m-alice-devices.conf"]
CODE_TREE = "/opt/sa02m-alice/sa02m_alice/client/main.py"

fails = 0


def ok(msg: str) -> None:
    print("ok    " + msg)


def bad(msg: str) -> None:
    global fails
    fails += 1
    print("FAIL  " + msg)


def skip(msg: str) -> None:
    print("SKIP  " + msg)


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        bad(f"cannot read {p.relative_to(ROOT)}: {e}")
        return ""


def generic_hit(dst: str, rule: str) -> bool:
    """The never-deploy rule semantics of validate_package.path_is_preserved:
    a trailing-slash rule is a directory prefix, one `*` a same-depth glob,
    anything else an exact path. Used only for the two homes that ship no
    predicate of their own (the bash array and the JSON map)."""
    if rule.endswith("/"):
        return dst == rule.rstrip("/") or dst.startswith(rule)
    if "*" in rule:
        pre, _, suf = rule.partition("*")
        return dst.startswith(pre) and dst.endswith(suf) and dst.count("/") == rule.count("/")
    return dst == rule


# ── 0. the live layout, from its code home ─────────────────────────────────
print("── 0. live Alice conf layout (sa02m_alice.common.constants) ──")
env = {k: v for k, v in os.environ.items() if k != "SA02M_ALICE_ETC"}
env["PYTHONPATH"] = str(ALICE_PKG)
r = subprocess.run(
    [sys.executable, "-c",
     "from sa02m_alice.common import constants as C;"
     "print(C.ETC_DIR); print(C.CLIENT_CONF); print(C.DEVICES_CONF)"],
    env=env, capture_output=True, text=True, timeout=30,
)
lines = r.stdout.split()
if r.returncode != 0 or len(lines) != 3:
    bad(f"0 could not read the live layout from constants (rc={r.returncode}): {r.stderr.strip()[:200]}")
    print(f"alice-conf-homes: {fails} FAILURE(S)")
    sys.exit(1)
ETC_DIR, CLIENT, DEVICES = lines
LIVE = [CLIENT, DEVICES]
if ETC_DIR.startswith("/etc/") and all(p.startswith(ETC_DIR.rstrip("/") + "/") for p in LIVE):
    ok(f"0a live confs {CLIENT}, {DEVICES} (ETC_DIR={ETC_DIR})")
else:
    bad(f"0a live layout is not an /etc directory holding both confs: {lines}")
tmp_txt = read(TMPFILES)
if re.search(r"^d\s+" + re.escape(ETC_DIR) + r"\s", tmp_txt, re.M):
    ok(f"0b tmpfiles creates {ETC_DIR}")
else:
    bad(f"0b {TMPFILES.relative_to(ROOT)} has no `d {ETC_DIR}` line — the layout the lists must name moved")
inst = [l for l in read(INSTALLER).splitlines() if not l.lstrip().startswith("#")]
if any(f'"{ETC_DIR}/$f"' in l and "install -m" in l for l in inst):
    ok(f"0c scripts/06-alice.sh installs the confs into {ETC_DIR}/")
else:
    bad(f"0c scripts/06-alice.sh no longer installs the confs into {ETC_DIR}/ — the layout the lists must name moved")

WANT = LIVE + LEGACY

# ── 1. validate_package.path_is_preserved ─────────────────────────────────
print("── 1. OTA validator (opt/sa02m-update/lib/validate_package.py) ──")
sys.path.insert(0, str(VALIDATOR_DIR))
try:
    from lib import validate_package as vp  # noqa: E402
except Exception as e:  # pragma: no cover - reported, not raised
    bad(f"1 cannot import validate_package: {e}")
    vp = None
if vp is not None:
    for p in WANT:
        (ok if vp.path_is_preserved(p) else bad)(f"1 path_is_preserved({p})")
    if vp.path_is_preserved(CODE_TREE):
        bad(f"1 negative control: the code tree {CODE_TREE} is preserved — OTA could no longer deploy the Alice package")
    else:
        ok("1 negative control: the Alice code tree stays deployable")

# ── 2. runner bootstrap PRESERVE_PREFIXES + its shipped predicate ─────────
print("── 2. runner bootstrap guard (etc/sa02m-update-runner.sh PRESERVE_PREFIXES) ──")
rtxt = read(RUNNER)
m_tuple = re.search(r"^PRESERVE_PREFIXES = (\(.*?^\))", rtxt, re.S | re.M)
m_pred = re.search(r"^    for p in PRESERVE_PREFIXES:\n\s+if (.+):\s*$", rtxt, re.M)
if not (m_tuple and m_pred):
    bad("2 could not extract PRESERVE_PREFIXES and its predicate from the runner (non-vacuity)")
else:
    prefixes = ast.literal_eval(m_tuple.group(1))
    pred = compile(m_pred.group(1), "runner-predicate", "eval")
    hit = lambda dst: any(eval(pred, {}, {"dst": dst, "p": p}) for p in prefixes)  # noqa: E731
    for p in WANT:
        (ok if hit(p) else bad)(f"2 runner bootstrap refuses a deploy onto {p}")
    (bad if hit(CODE_TREE) else ok)("2 negative control: the Alice code tree stays deployable")

# ── 3. runner bash PRESERVE_PATHS (documentation-only array) ──────────────
print("── 3. runner bash PRESERVE_PATHS ──")
m_arr = re.search(r"^PRESERVE_PATHS=\(\n(.*?)^\)", rtxt, re.S | re.M)
if not m_arr:
    bad("3 could not extract the PRESERVE_PATHS array (non-vacuity)")
else:
    script = "set -f\nPRESERVE_PATHS=(\n" + m_arr.group(1) + ")\nprintf '%s\\n' \"${PRESERVE_PATHS[@]}\"\n"
    rb = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    arr = [l for l in rb.stdout.splitlines() if l]
    if rb.returncode != 0 or len(arr) < 5:
        bad(f"3 bash could not expand the array (rc={rb.returncode}, {len(arr)} entries)")
    else:
        for p in WANT:
            (ok if any(generic_hit(p, r) for r in arr) else bad)(f"3 PRESERVE_PATHS covers {p}")
        (bad if any(generic_hit(CODE_TREE, r) for r in arr) else ok)(
            "3 negative control: the Alice code tree is not in PRESERVE_PATHS")

# ── 4. deploy map never_deploy ────────────────────────────────────────────
print("── 4. scripts/offline-update-deploy-map.json never_deploy ──")
try:
    nd = json.loads(read(DEPLOY_MAP) or "{}").get("never_deploy") or []
except ValueError as e:
    nd = []
    bad(f"4 deploy map is not JSON: {e}")
if len(nd) < 5:
    bad(f"4 never_deploy has {len(nd)} entries — the list was not found (non-vacuity)")
else:
    for p in WANT:
        (ok if any(generic_hit(p, r) for r in nd) else bad)(f"4 never_deploy covers {p}")
    (bad if any(generic_hit(CODE_TREE, r) for r in nd) else ok)(
        "4 negative control: the Alice code tree is not in never_deploy")

# ── 5. the shipped backup collect_paths(), on a seeded sandbox ────────────
print("── 5. user backup (etc/sa02m-web-backup.sh collect_paths) ──")
emitted: list[str] = []
btxt = read(BACKUP)
m_fn = re.search(r"^collect_paths\(\) \{\n.*?^\}\n", btxt, re.S | re.M)
if not m_fn:
    bad("5 could not extract collect_paths() from the backup script (non-vacuity)")
else:
    fn, n_sub = re.subn(r"(?<=\s)/etc/", '"$SANDBOX"/etc/', m_fn.group(0))
    with tempfile.TemporaryDirectory() as sb:
        seed = [ETC_DIR + "/sa02m-alice-client.conf", ETC_DIR + "/sa02m-alice-devices.conf",
                ETC_DIR + "/sa02m-alice-server.conf", *LEGACY, "/etc/sa02m_web.env"]
        for p in seed:
            f = Path(sb + p)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x\n", encoding="utf-8")
        rb = subprocess.run(["bash", "-c", fn + "\ncollect_paths\n"], capture_output=True, text=True,
                            timeout=30, env={**os.environ, "SANDBOX": sb})
        out = [l for l in rb.stdout.splitlines() if l]
    foreign = [l for l in out if not l.startswith(sb)]
    if rb.returncode != 0 or n_sub < 5 or not out or foreign:
        bad(f"5 retargeted collect_paths did not run cleanly (rc={rb.returncode}, {n_sub} paths retargeted, "
            f"{len(out)} emitted, {len(foreign)} outside the sandbox): {rb.stderr.strip()[:200]}")
    else:
        emitted = [l[len(sb):] for l in out]
        for p in WANT:
            (ok if p in emitted else bad)(f"5 the backup archives {p}")

# ── 5e. the WHOLE backup script, end to end: what www-data can plant ───────
# The backup runs as root (sudo from web_backup.cgi) and streams the archive
# to the panel, and /etc/sa02m-alice is root:www-data 0771 — so www-data (any
# panel session: cmd_exec.cgi) can put any name where the backup reads. Each
# case plants what www-data could plant at a www-data-writable source, runs the
# SHIPPED script with /etc/ retargeted into a sandbox, and asserts the victim's
# bytes are nowhere in the stream. The sandbox's conf dir is group-writable like
# the real one: the backup trusts a name only where no one but root (here: the invoking
# user) can create it, which is what separates a plant from the installer's own
# /etc/nginx/sites-enabled link (5e1 — that one must still be archived).
print("── 5e. user backup end to end (sandboxed, planted sources) ──")
if not (shutil.which("bash") and shutil.which("tar")):
    skip("5e needs bash + tar — not run on this host (a skip is not a pass)")
else:
    import gzip  # noqa: E402
    import io  # noqa: E402
    import tarfile  # noqa: E402

    SECRET = b"root:$6$planted-victim-secret:19000:0:99999:7:::\n"
    MARK = b"planted-victim-secret"

    def seed_backup(sb: Path) -> dict:
        etc = sb / "etc"
        (etc / "nginx/sites-available").mkdir(parents=True)
        (etc / "nginx/sites-enabled").mkdir(parents=True)
        (etc / "sa02m_web.env").write_bytes(b"env\n")
        (etc / "nginx/sites-available/network_config").write_bytes(b"server {}\n")
        (etc / "nginx/sites-enabled/000-sa02m-network_config").symlink_to("../sites-available/network_config")
        adir = etc / "sa02m-alice"
        adir.mkdir()
        adir.chmod(0o770)
        (adir / "sa02m-alice-client.conf").write_bytes(b"client\n")
        (adir / "sa02m-alice-devices.conf").write_bytes(b"devices\n")
        vdir = sb / "secret"
        vdir.mkdir(mode=0o700)
        victim = vdir / "shadow"
        victim.write_bytes(SECRET)
        victim.chmod(0o600)
        return {"etc": etc, "adir": adir, "victim": victim, "vdir": vdir}

    def run_backup(sb: Path):
        """Run the retargeted SHIPPED backup. Returns (rc, stderr, {source path:
        archived bytes} from the manifest, decompressed stream, #retargets)."""
        body = btxt.replace("/etc/", f"{sb}/etc/")
        script = sb / "backup.sh"
        script.write_text(body, encoding="utf-8")
        env = {**os.environ, "SA02M_WEB_VERSION_FILE": str(sb / "no-version"),
               "SA02M_DEVICE_ID_FILE": str(sb / "no-machine-id")}
        try:
            r = subprocess.run(["bash", str(script)], capture_output=True, timeout=60, env=env)
        except subprocess.TimeoutExpired:
            return 124, "timed out after 60 s (a FIFO opened blocking?)", {}, b"", 0
        files, raw = {}, b""
        if r.returncode == 0:
            raw = gzip.decompress(r.stdout)
            with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:gz") as tf:
                man = json.loads(tf.extractfile("backup-manifest.json").read())
                for ent in man.get("paths", []):
                    files[ent["path"][len(str(sb)):]] = tf.extractfile(ent["archive_path"]).read()
        return r.returncode, r.stderr.decode("utf-8", "replace"), files, raw, body.count(f"{sb}/etc/")

    A_CLIENT, A_DEVICES = ETC_DIR + "/sa02m-alice-client.conf", ETC_DIR + "/sa02m-alice-devices.conf"
    NGINX = "/etc/nginx/sites-enabled/000-sa02m-network_config"

    def backup_case(tag: str, what: str, plant, expect_absent: list, expect_kept: dict, warn_for: list):
        with tempfile.TemporaryDirectory() as tsb:
            sb = Path(tsb)
            env_ = seed_backup(sb)
            plant(env_)
            rc, err, files, raw, n_sub = run_backup(sb)
        problems = []
        if n_sub < 5:
            problems.append(f"retarget touched only {n_sub} /etc/ paths (non-vacuity)")
        if rc != 0:
            problems.append(f"rc={rc} ({err.strip()[-200:]!r}) — a refused optional entry must not abort the backup")
        if MARK in raw:
            problems.append("the victim's bytes ARE in the archive")
        problems += [f"{p} archived" for p in expect_absent if p in files]
        problems += [f"{p} missing or wrong bytes ({files.get(p)!r})" for p, b in expect_kept.items() if files.get(p) != b]
        problems += [f"no WARN naming {p}" for p in warn_for if p not in err]
        (bad if problems else ok)(f"{tag} {what}" + (": " + "; ".join(problems) if problems else ""))

    kept_all = {"/etc/sa02m_web.env": b"env\n", A_CLIENT: b"client\n", A_DEVICES: b"devices\n", NGINX: b"server {}\n"}
    backup_case("5e1", "clean board: every home archived, the installer's root-made nginx link through to its target",
                lambda e: None, [], kept_all, [])

    def plant_symlink(e):
        (e["adir"] / "sa02m-alice-client.conf").unlink()
        (e["adir"] / "sa02m-alice-client.conf").symlink_to(e["victim"])
    backup_case("5e2", "a symlink planted at the live client conf is skipped with a WARN; the victim never reaches the archive",
                plant_symlink, [A_CLIENT], {k: v for k, v in kept_all.items() if k != A_CLIENT}, [A_CLIENT])

    def plant_fifo(e):
        (e["adir"] / "sa02m-alice-devices.conf").unlink()
        os.mkfifo(e["adir"] / "sa02m-alice-devices.conf")
    backup_case("5e3", "a FIFO planted at the live devices conf neither hangs the backup nor is archived; WARN",
                plant_fifo, [A_DEVICES], {k: v for k, v in kept_all.items() if k != A_DEVICES}, [A_DEVICES])

    def plant_hardlink(e):
        (e["adir"] / "sa02m-alice-client.conf").unlink()
        os.link(e["victim"], e["adir"] / "sa02m-alice-client.conf")
    backup_case("5e4", "a hard link to the victim planted at the live client conf is skipped with a WARN",
                plant_hardlink, [A_CLIENT], {k: v for k, v in kept_all.items() if k != A_CLIENT}, [A_CLIENT])

    def plant_parent(e):
        # The conf directory itself replaced by a symlink, in a parent others
        # can write: on a board /etc is root-only, so this models the class
        # (a directory on the path that is not root's), not a live exploit.
        # The nginx directory is then a directory inside that parent too —
        # refused by the same rule (a swap between check and open).
        shutil.rmtree(e["adir"])
        (e["vdir"] / "sa02m-alice-client.conf").write_bytes(SECRET)
        e["adir"].symlink_to(e["vdir"])
        e["etc"].chmod(0o770)
    backup_case("5e5", "a symlinked conf directory (and any directory) in a parent others can write is never descended; "
                "a plain file there is still archived",
                plant_parent, [A_CLIENT, A_DEVICES, NGINX], {"/etc/sa02m_web.env": b"env\n"}, [ETC_DIR, "/etc/nginx"])

# ── 6. restore ALLOW list, and backup/restore agreement ───────────────────
print("── 6. SSH restore allow-list (etc/sa02m-restore-backup.sh ALLOW) ──")
stxt = read(RESTORE)
m_allow = re.search(r"^ALLOW = \[\n.*?^\]\n", stxt, re.S | re.M)
m_def = re.search(r"^def allowed\(.*?\n(?:    .*\n)+", stxt, re.M)
allowed = None
if not (m_allow and m_def):
    bad("6 could not extract ALLOW / allowed() from the restore script (non-vacuity)")
else:
    ns: dict = {"re": re}
    exec(m_allow.group(0) + m_def.group(0), ns)  # noqa: S102 - the shipped source under test
    allowed = ns["allowed"]
    for p in WANT:
        (ok if allowed(p) else bad)(f"6 the restore admits {p}")
    (bad if allowed("/etc/shadow") else ok)("6 negative control: /etc/shadow is refused")
    if emitted:
        refused = [p for p in emitted if not allowed(p)]
        if refused:
            bad(f"6 the restore refuses what the backup writes — every such archive fails AS A WHOLE: {refused}")
        else:
            ok(f"6 every one of the {len(emitted)} paths the backup emits passes the restore")

# ── 7. restore atomic_install(): ownership + symlink hardening ─────────────
# The restore runs as root and /etc/sa02m-alice is root:www-data 0771, so a
# www-data account (any panel session: cmd_exec.cgi) can create names in the
# directory it writes into. Every case below plants what www-data could plant
# (in a www-data-writable directory, as on the board) and asserts a victim file stays byte-,
# mode- and owner-identical. Only the cases that OBSERVE an owner (7a/7b/7h)
# need root; 7c-7g run everywhere — CI runs this row as a non-root user, and
# the comment-mutation proof of the restore's refusal line depends on them.
print("── 7. restore atomic_install() ownership + symlink hardening ──")
HELPERS = ("RestoreRefused", "DIR_SPEC", "dest_check", "ensure_parent", "atomic_install")
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
TWIN_RE = re.compile(r"^# >>> trusted-path resolver.*?^# <<< trusted-path resolver\n", re.S | re.M)


def extract_block(src: str, name: str) -> str | None:
    """A top-level def/class body, or a top-level `NAME = {…}` dict literal."""
    m = re.search(r"^(?:def|class) " + name + r"\b.*?\n(?:(?:    .*)?\n)+", src, re.M)
    if m:
        return m.group(0)
    m = re.search(r"^" + name + r" = \{\n.*?^\}\n", src, re.S | re.M)
    return m.group(0) if m else None


blocks = {n: extract_block(stxt, n) for n in HELPERS}
missing = [n for n, b in blocks.items() if b is None]
m_twin_r = TWIN_RE.search(stxt)
m_twin_b = TWIN_RE.search(btxt)
# 7t: the trusted-path resolver is one algorithm in two self-contained root
# scripts (neither can import the other on a board); the twins must not drift.
if not (m_twin_r and m_twin_b):
    bad(f"7t trusted-path resolver block missing (restore: {bool(m_twin_r)}, backup: {bool(m_twin_b)}) — "
        "the symlink rule the backup and the restore share is gone from one of them")
elif m_twin_r.group(0) != m_twin_b.group(0):
    bad("7t the trusted-path resolver in etc/sa02m-restore-backup.sh and etc/sa02m-web-backup.sh differ — "
        "the backup and the restore no longer agree on which names root may follow")
else:
    ok("7t the backup and the restore carry the same trusted-path resolver (byte-identical twins)")
# 7s needs no root: the directory spec the restore creates a missing Alice conf
# dir with must be the one tmpfiles and the installer give it.
spec = None
if blocks["DIR_SPEC"]:
    ns_spec: dict = {}
    exec(blocks["DIR_SPEC"], ns_spec)  # noqa: S102 - the shipped source under test
    spec = ns_spec["DIR_SPEC"].get(ETC_DIR)
m_tmpf = re.search(r"^d\s+" + re.escape(ETC_DIR) + r"\s+(\d+)\s+(\S+)\s+(\S+)", tmp_txt, re.M)
want_spec = (int(m_tmpf.group(1), 8), m_tmpf.group(2), m_tmpf.group(3)) if m_tmpf else None
inst_spec = any(f"install -d -m {oct(want_spec[0])[2:].zfill(4)} -o {want_spec[1]} -g {want_spec[2]} {ETC_DIR}" in l
                for l in inst) if want_spec else False
if spec is not None and spec == want_spec and inst_spec:
    ok(f"7s a missing {ETC_DIR} is created {oct(spec[0])} {spec[1]}:{spec[2]} — the tmpfiles line and the installer agree")
else:
    fmt = lambda t: t and (oct(t[0]), *t[1:])  # noqa: E731
    bad(f"7s restore DIR_SPEC[{ETC_DIR}]={fmt(spec)} vs tmpfiles {fmt(want_spec)} (installer install -d agrees: {inst_spec}) "
        "— a restore onto a board without the dir creates it unwritable for the CGI")

if blocks["atomic_install"] is None:
    bad("7 could not extract atomic_install() from the restore script (non-vacuity)")
else:
    if missing:
        bad(f"7 restore hardening helpers absent: {missing} — the cases below run the bare atomic_install()")
    import grp  # noqa: E402
    import pwd  # noqa: E402
    import stat as stat_mod  # noqa: E402
    ns = {"os": os, "shutil": shutil, "Path": Path, "stat": stat_mod, "tempfile": tempfile,
          "pwd": pwd, "grp": grp, "sys": sys}
    if m_twin_r:
        exec(m_twin_r.group(0), ns)  # noqa: S102 - the shipped source under test
    for n in HELPERS:
        if blocks[n]:
            exec(blocks[n], ns)  # noqa: S102 - the shipped source under test
    ai = ns["atomic_install"]

    def snap(p: Path) -> tuple:
        st = os.lstat(p)
        return (p.read_bytes(), oct(stat_mod.S_IMODE(st.st_mode)), st.st_uid, st.st_gid)

    def make_victim(where: Path, name: str = "shadow") -> Path:
        """A 0600 file in a 0700 directory: root's where the host allows the
        chown, else the invoking user's — the byte/mode identity assertions
        hold either way (7c-7g need no owner change to observe a write)."""
        where.mkdir(exist_ok=True)
        if IS_ROOT:
            os.chown(where, 0, 0)
        os.chmod(where, 0o700)
        v = where / name
        v.write_bytes(b"root-secret\n")
        if IS_ROOT:
            os.chown(v, 0, 0)
        os.chmod(v, 0o600)
        return v

    def leftovers(d: Path, name: str) -> list:
        return sorted(p.name for p in d.iterdir() if p.name != name and name in p.name)

    def attempt(src_p: Path, dest: Path, mode: int = 0o660):
        try:
            ai(src_p, str(dest), mode)
        except Exception as e:  # noqa: BLE001 - any refusal counts; the type is not the contract
            return e
        return None

    with tempfile.TemporaryDirectory() as sb:
        src = Path(sb, "src.conf")
        src.write_text("new\n", encoding="utf-8")
        d = Path(sb, "sa02m-alice")
        d.mkdir()
        if IS_ROOT:
            os.chown(d, 0, 4242)
        os.chmod(d, 0o770)
        live = d / "sa02m-alice-client.conf"
        live.write_text("old\n", encoding="utf-8")
        fresh = d / "sa02m-alice-devices.conf"
        if IS_ROOT:
            os.chown(live, 1234, 4321)
            err = attempt(src, live)
            st = live.stat()
            body = live.read_text(encoding="utf-8")
            if err is None and (st.st_uid, st.st_gid) == (1234, 4321) and body == "new\n":
                ok("7a an existing file keeps its owner (1234:4321) and gets the new bytes")
            else:
                bad(f"7a restored file is {st.st_uid}:{st.st_gid} body={body!r} err={err!r} — expected 1234:4321 'new' "
                    "(root:root locks the CGI out of a root:www-data conf)")
            err = attempt(src, fresh)
            st = fresh.stat() if fresh.exists() else None
            if err is None and st and (st.st_uid, st.st_gid) == (0, 4242):
                ok("7b a new file is root with its directory's group (0:4242)")
            else:
                bad(f"7b new restored file is {st and (st.st_uid, st.st_gid)} err={err!r} — expected 0:4242 (the directory's group)")
            # 7b2: a conf dir DIR_SPEC gives a non-root owner (the HomeKit / Home
            # Connect dirs are www-data's, setgid to the daemon's group — contract
            # homekit-bridge.md §13): a NEW file takes the dir's owner and group,
            # or the CGI could not read the restored conf. The spec is keyed by
            # the board path; the sandbox dir is registered under the same shape.
            dh = Path(sb, "sa02m-homekit")
            dh.mkdir()
            os.chown(dh, 61033, 61001)
            os.chmod(dh, 0o2750)
            ns["DIR_SPEC"][str(dh)] = (0o2750, "www-data", "sa02m-homekit")
            try:
                err = attempt(src, dh / "sa02m-homekit.conf", 0o640)
            finally:
                del ns["DIR_SPEC"][str(dh)]
            new_hk = dh / "sa02m-homekit.conf"
            st = new_hk.stat() if new_hk.exists() else None
            if err is None and st and (st.st_uid, st.st_gid, stat_mod.S_IMODE(st.st_mode)) == (61033, 61001, 0o640):
                ok("7b2 a new file in a conf dir DIR_SPEC gives to www-data takes the dir's owner and group (61033:61001 0640)")
            else:
                bad(f"7b2 new restored HomeKit conf is {st and (st.st_uid, st.st_gid, oct(stat_mod.S_IMODE(st.st_mode)))} "
                    f"err={err!r} — expected 61033:61001 0640 (the CGI must be able to read it)")
        else:
            skip("7a/7b owner-keeping cases need root to observe a chown — not run on this host (a skip is not a pass)")
            fresh.write_text("old\n", encoding="utf-8")

        # 7c: a symlink planted at the temp name. The pre-fix code used the
        # predictable `<dest>.tmp.<pid>` and followed it with copy2/chmod/chown;
        # the plant sits at the exact name it would use (this process's pid,
        # since the function runs in-process) plus its neighbours.
        victim = make_victim(Path(sb, "victim-c"))
        before = snap(victim)
        for pid in range(max(1, os.getpid() - 3), os.getpid() + 4):
            (d / f"{live.name}.tmp.{pid}").symlink_to(victim)
        err = attempt(src, live)
        after = snap(victim)
        live_ok = not live.is_symlink() and live.is_file() and live.read_bytes() == b"new\n"
        if after == before and err is None and live_ok:
            ok("7c a symlink planted at the temp name is never followed: victim identical, live conf restored as a regular file")
        else:
            bad(f"7c planted temp-name symlink: victim {before[1:]}→{after[1:]} bytes-changed={after[0] != before[0]}, "
                f"live regular+new={live_ok}, err={err!r} — root wrote/chmod/chowned through a www-data plant")
        for p in d.glob(f"{live.name}.tmp.*"):
            p.unlink()

        # 7d: the dest itself is a symlink → refused, nothing written.
        victim = make_victim(Path(sb, "victim-d"))
        before = snap(victim)
        fresh.unlink(missing_ok=True)
        fresh.symlink_to(victim)
        err = attempt(src, fresh)
        after = snap(victim)
        still_link = fresh.is_symlink() and os.readlink(fresh) == str(victim)
        extra = leftovers(d, fresh.name)
        if err is not None and after == before and still_link and not extra:
            ok(f"7d a symlinked dest is refused ({err}); victim identical, nothing written")
        else:
            bad(f"7d symlinked dest: refused={err is not None}, victim identical={after == before}, "
                f"dest still the plant={still_link}, leftovers={extra}")
        fresh.unlink()

        # 7e: the dest is not a regular file (a FIFO) → refused.
        os.mkfifo(fresh)
        err = attempt(src, fresh)
        is_fifo = stat_mod.S_ISFIFO(os.lstat(fresh).st_mode)
        extra = leftovers(d, fresh.name)
        if err is not None and is_fifo and not extra:
            ok(f"7e a non-regular dest (FIFO) is refused ({err}); nothing written")
        else:
            bad(f"7e FIFO dest: refused={err is not None}, still a FIFO={is_fifo}, leftovers={extra}")
        fresh.unlink()

        # 7f: the parent directory is a symlink planted where others can write
        # (a group-writable directory, like /etc/sa02m-alice) → refused. A link only root
        # could have made (root-owned, in a directory only root can write) is
        # the installer's, and is followed — 8d pins that side.
        vdir = Path(sb, "victim-f")
        victim = make_victim(vdir, live.name)
        before = snap(victim)
        link_dir = d / "alice-link"
        link_dir.symlink_to(vdir)
        err = attempt(src, link_dir / live.name)
        after = snap(victim)
        extra = leftovers(vdir, live.name)
        if err is not None and after == before and not extra:
            ok(f"7f a symlinked parent directory in a directory others can write is refused ({err}); victim identical")
        else:
            bad(f"7f symlinked parent: refused={err is not None}, victim identical={after == before}, leftovers={extra}")
        link_dir.unlink()

        # 7g: a failure after the temp file exists (unreadable source) removes it;
        # neither that nor 7a-7f leaves a temp file behind.
        err = attempt(Path(sb, "no-such-src"), live)
        (ok if err is not None and live.read_bytes() == b"new\n" else bad)(
            f"7g a failed copy raises ({type(err).__name__}) and leaves the live conf untouched")
        extra = leftovers(d, "sa02m-alice-")
        extra = [n for n in extra if n not in (live.name, fresh.name)]
        (ok if not extra else bad)(f"7g no temp file left in the conf dir (found: {extra})")

        # 7h: a missing Alice conf dir is created with its tmpfiles owner/mode.
        if spec is None:
            bad("7h no DIR_SPEC entry for the Alice conf dir — a missing dir is created root:root 0755")
        elif not IS_ROOT:
            skip("7h creating the conf dir root:www-data needs root to chown — not run on this host (a skip is not a pass)")
        else:
            try:
                want_gid = grp.getgrnam(spec[2]).gr_gid
                want_uid = pwd.getpwnam(spec[1]).pw_uid
            except KeyError:
                want_gid = None
            if want_gid is None:
                skip(f"7h user/group {spec[1]}:{spec[2]} absent on this host — not run (a skip is not a pass)")
            else:
                nd = Path(sb, "etc-new", "sa02m-alice")
                ns["DIR_SPEC"] = {str(nd): spec}
                err = attempt(src, nd / live.name)
                if nd.is_dir():
                    dst = os.lstat(nd)
                    got = (stat_mod.S_IMODE(dst.st_mode), dst.st_uid, dst.st_gid)
                    fst = os.lstat(nd / live.name) if (nd / live.name).exists() else None
                else:
                    got, fst = None, None
                if err is None and got == (spec[0], want_uid, want_gid) and fst and fst.st_gid == want_gid:
                    ok(f"7h a missing conf dir is created {oct(spec[0])} {spec[1]}:{spec[2]}; the conf lands with group {spec[2]}")
                else:
                    bad(f"7h missing conf dir created as {got} (want {(spec[0], want_uid, want_gid)}), "
                        f"conf gid={fst and fst.st_gid}, err={err!r}")

# ── 8. the SHIPPED restore script, end to end in a sandbox ─────────────────
# Section 7 runs the functions; this runs the whole script (shell wrapper +
# python body) with every /etc/ path retargeted into a sandbox and shims for
# systemctl/nginx, so the preflight wiring and the post-restore restarts are
# observed, not read.
print("── 8. restore script end to end (sandboxed --apply) ──")
if not (shutil.which("bash") and shutil.which("tar")):
    skip("8 needs bash + tar — not run on this host (a skip is not a pass)")
else:
    import hashlib  # noqa: E402
    import io  # noqa: E402
    import tarfile  # noqa: E402

    def run_restore(sb: Path, files: dict, active: set, mode: str = "--apply"):
        """Build an archive of `files` (sandbox-relative dest → bytes) and run the
        retargeted restore on it. Returns (rc, output, systemctl calls)."""
        body = stxt.replace("/etc/", f"{sb}/etc/")
        script = sb / "restore.sh"
        script.write_text(body, encoding="utf-8")
        shims = sb / "shims"
        shims.mkdir(exist_ok=True)
        calls = sb / "systemctl.calls"
        (shims / "systemctl").write_text(
            "#!/bin/bash\nprintf '%s\\n' \"$*\" >> " + str(calls) + "\n"
            "if [ \"$1\" = is-active ]; then\n  case \" " + " ".join(sorted(active)) + " \" in\n"
            "    *\" ${@: -1} \"*) exit 0 ;;\n  esac\n  exit 3\nfi\nexit 0\n", encoding="utf-8")
        (shims / "nginx").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        for f in shims.iterdir():
            f.chmod(0o755)
        paths, buf = [], io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            members = {}
            for i, (dest, data) in enumerate(files.items()):
                ap = f"files/{i}"
                members[ap] = data
                paths.append({"path": f"{sb}{dest}", "archive_path": ap,
                              "sha256": hashlib.sha256(data).hexdigest(), "mode": "0o660"})
            members["backup-manifest.json"] = json.dumps({"schema_version": 1, "paths": paths}).encode()
            for name, data in members.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        arc = sb / "backup.tar.gz"
        arc.write_bytes(buf.getvalue())
        if calls.exists():
            calls.unlink()
        env = {**os.environ, "PATH": f"{shims}:{os.environ.get('PATH', '')}",
               "SA02M_WEB_BACKUP": str(sb / "no-backup-bin"), "SA02M_BACKUP_EXPORT": str(sb / "export")}
        r = subprocess.run(["bash", str(script), mode, str(arc)], capture_output=True, text=True,
                           timeout=60, env=env)
        got = calls.read_text().splitlines() if calls.exists() else []
        return r.returncode, r.stdout + r.stderr, got, body.count(f"{sb}/etc/")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        adir = sb / "etc/sa02m-alice"
        adir.mkdir(parents=True)
        adir.chmod(0o770)  # group-writable as on the board (root:www-data 0771): others can plant names here
        env_f = sb / "etc/sa02m_web.env"
        env_f.write_bytes(b"old-env\n")
        conf = adir / "sa02m-alice-client.conf"
        victim = sb / "victim"
        victim.write_bytes(b"root-secret\n")
        victim.chmod(0o600)
        conf.symlink_to(victim)
        files = {"/etc/sa02m_web.env": b"new-env\n", "/etc/sa02m-alice/sa02m-alice-client.conf": b"new-conf\n"}
        rc, out, calls, n_sub = run_restore(sb, files, {"sa02m-alice-client"})
        untouched = (env_f.read_bytes() == b"old-env\n" and conf.is_symlink()
                     and victim.read_bytes() == b"root-secret\n" and not (sb / "export").exists())
        if n_sub < 8:
            bad(f"8 retarget touched only {n_sub} /etc/ paths in the restore body (non-vacuity)")
        elif rc != 0 and "symlink" in out and untouched:
            ok("8a a symlinked dest fails the whole --apply in the preflight: rc!=0, reason printed, nothing written (not even the other file)")
        else:
            bad(f"8a symlinked dest under --apply: rc={rc}, untouched={untouched}, output: {out.strip()[-300:]!r}")
        conf.unlink()
        conf.write_bytes(b"old-conf\n")
        rc, out, calls, _ = run_restore(sb, files, {"sa02m-alice-client"})
        restored = env_f.read_bytes() == b"new-env\n" and conf.read_bytes() == b"new-conf\n"
        restarts = [c for c in calls if c.startswith("restart ")]
        probed = sorted(c.split()[-1] for c in calls if c.startswith("is-active"))
        if (rc == 0 and restored and "restart sa02m-alice-client" in restarts
                and probed == ["sa02m-alice-client", "sa02m-alice-config", "sa02m-cloud-control"]
                and not [c for c in restarts if "alice-config" in c or "cloud-control" in c]):
            ok("8b a clean --apply restores both files and restarts ONLY the active Alice unit (inactive ones are never started)")
        else:
            bad(f"8b clean --apply: rc={rc}, restored={restored}, probed={probed}, restarts={restarts}, "
                f"output: {out.strip()[-300:]!r}")
        rc, out, calls, _ = run_restore(sb, {"/etc/sa02m_web.env": b"env-only\n"}, {"sa02m-alice-client"})
        if rc == 0 and not [c for c in calls if "alice" in c or "cloud" in c]:
            ok("8c a restore without Alice confs touches no Alice unit")
        else:
            bad(f"8c env-only --apply: rc={rc}, systemctl calls={calls}")

        # 8d: the installer's own link (scripts/03-webserver.sh: ln -sf
        # sites-available/network_config sites-enabled/000-sa02m-network_config)
        # is in every board backup. Only root can make a name in that directory,
        # so the restore writes THROUGH it, as it always did — refusing it would
        # fail every restore (even --dry-run) of every board's own backup.
        avail = sb / "etc/nginx/sites-available/network_config"
        avail.parent.mkdir(parents=True)
        avail.write_bytes(b"old-site\n")
        link = sb / "etc/nginx/sites-enabled/000-sa02m-network_config"
        link.parent.mkdir(parents=True)
        link.symlink_to("../sites-available/network_config")
        nginx_files = {"/etc/sa02m_web.env": b"env-2\n", "/etc/nginx/sites-enabled/000-sa02m-network_config": b"new-site\n"}
        rc_d, out_d, _, _ = run_restore(sb, nginx_files, set(), mode="--dry-run")
        rc, out, calls, _ = run_restore(sb, nginx_files, set())
        through = (link.is_symlink() and os.readlink(link) == "../sites-available/network_config"
                   and avail.read_bytes() == b"new-site\n" and env_f.read_bytes() == b"env-2\n")
        if rc_d == 0 and rc == 0 and through:
            ok("8d the installer's root-made nginx link validates and is written through (link kept, target restored)")
        else:
            bad(f"8d root-made nginx link: dry-run rc={rc_d}, apply rc={rc}, link kept + target restored={through}, "
                f"output: {(out_d + out).strip()[-300:]!r}")

# ── 9. installer conf modes: root never chmod/chgrps through a plant ──────
# scripts/06-alice.sh (a full install, as root) sets the confs' mode and group
# right after creating them — in the same root:www-data 0771 directory. chmod
# and chgrp follow a symlink, so a planted `sa02m-alice-client.conf ->
# /etc/sudoers.d/x` would hand www-data group-write on the target (root
# escalation). The SHIPPED lines between the conf-install loop and the systemd
# block run here with the conf dir retargeted into a sandbox and www-data
# replaced by the invoking user's group — no root needed.
print("── 9. installer conf modes (scripts/06-alice.sh) never follow a plant ──")
itxt = read(INSTALLER)
m9 = re.search(r"^for f in sa02m-alice-client\.conf [^\n]*\n.*?^done\n(.*?)^# ── systemd", itxt, re.S | re.M)
if not m9 or ETC_DIR not in m9.group(1):
    bad(f"9 could not extract the conf mode/group block after the conf-install loop in {INSTALLER.relative_to(ROOT)} (non-vacuity)")
else:
    import grp as grp9  # noqa: E402
    import stat as stat9  # noqa: E402
    my_group = grp9.getgrgid(os.getegid()).gr_name

    def run_modes(sb: Path):
        adir = sb / "sa02m-alice"
        block = m9.group(1).replace(ETC_DIR, str(adir)).replace("www-data", my_group)
        try:
            r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + block], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return 124, "timed out (a FIFO opened blocking?)"
        return r.returncode, r.stdout + r.stderr

    def seed_modes(sb: Path) -> dict:
        adir = sb / "sa02m-alice"
        adir.mkdir()
        adir.chmod(0o770)
        for n in ("sa02m-alice-client.conf", "sa02m-alice-devices.conf", "sa02m-alice-server.conf"):
            (adir / n).write_bytes(b"conf\n")
            (adir / n).chmod(0o600)
        vdir = sb / "victim"
        vdir.mkdir(mode=0o700)
        v1, v2 = vdir / "sudoers-x", vdir / "shadow"
        for v in (v1, v2):
            v.write_bytes(b"root-secret\n")
            v.chmod(0o600)
        return {"adir": adir, "v1": v1, "v2": v2}

    def mode_of(p: Path) -> int:
        return stat9.S_IMODE(os.lstat(p).st_mode)

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e9 = seed_modes(sb)
        rc, out = run_modes(sb)
        got = {n: oct(mode_of(e9["adir"] / n)) for n in ("sa02m-alice-client.conf", "sa02m-alice-devices.conf", "sa02m-alice-server.conf")}
        want = {"sa02m-alice-client.conf": "0o660", "sa02m-alice-devices.conf": "0o660", "sa02m-alice-server.conf": "0o640"}
        if rc == 0 and got == want:
            ok("9a regular confs get their modes (client/devices 0660, server 0640)")
        else:
            bad(f"9a conf modes: rc={rc}, got {got}, want {want}: {out.strip()[-200:]!r}")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e9 = seed_modes(sb)
        (e9["adir"] / "sa02m-alice-client.conf").unlink()
        (e9["adir"] / "sa02m-alice-client.conf").symlink_to(e9["v1"])
        (e9["adir"] / "sa02m-alice-devices.conf").unlink()
        os.link(e9["v2"], e9["adir"] / "sa02m-alice-devices.conf")
        before = [(mode_of(v), os.lstat(v).st_gid) for v in (e9["v1"], e9["v2"])]
        rc, out = run_modes(sb)
        after = [(mode_of(v), os.lstat(v).st_gid) for v in (e9["v1"], e9["v2"])]
        server_ok = oct(mode_of(e9["adir"] / "sa02m-alice-server.conf")) == "0o640"
        if before[0] == after[0] and "sa02m-alice-client.conf" in out:
            ok("9b a symlink planted at the client conf is left alone and reported; its target's mode/group unchanged")
        else:
            bad(f"9b planted symlink: target mode/gid {oct(before[0][0])}/{before[0][1]} → {oct(after[0][0])}/{after[0][1]}, "
                f"reported={'sa02m-alice-client.conf' in out} — root chmod/chgrp'd through a www-data plant")
        if before[1] == after[1] and "sa02m-alice-devices.conf" in out:
            ok("9c a hard link planted at the devices conf is left alone and reported; the linked file unchanged")
        else:
            bad(f"9c planted hard link: file mode/gid {oct(before[1][0])}/{before[1][1]} → {oct(after[1][0])}/{after[1][1]}, "
                f"reported={'sa02m-alice-devices.conf' in out}")
        (ok if rc == 0 and server_ok else bad)(
            f"9d the installer carries on past the plants (rc={rc}) and still sets the regular server conf 0640 ({server_ok})")

    # 9e: where the HomeKit bridge is installed (group sa02m-alice-devices
    # exists) the device document is the bridge's read grant
    # (www-data:sa02m-alice-devices 0640, usr/local/sbin/sa02m-daemon-access.sh)
    # — 06-alice must leave it alone instead of handing it back to www-data,
    # which cut the bridge off. The group is made to exist by naming the
    # invoking user's own group in its place.
    hk_arg = " sa02m-alice-devices <<'PY'"
    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e9 = seed_modes(sb)
        adir = sb / "sa02m-alice"
        block = m9.group(1).replace(ETC_DIR, str(adir)).replace("www-data", my_group)
        if hk_arg not in block:
            bad("9e the 06-alice mode block names no sa02m-alice-devices group — nothing tells it the device "
                "document is the HomeKit bridge's grant")
        else:
            block = block.replace(hk_arg, f" {my_group} <<'PY'")
            r9e = subprocess.run(["bash", "-c", "set -euo pipefail\n" + block], capture_output=True, text=True, timeout=60)
            got = {n: oct(mode_of(adir / n)) for n in ("sa02m-alice-client.conf", "sa02m-alice-devices.conf", "sa02m-alice-server.conf")}
            want = {"sa02m-alice-client.conf": "0o660", "sa02m-alice-devices.conf": "0o600", "sa02m-alice-server.conf": "0o640"}
            if r9e.returncode == 0 and got == want:
                ok("9e with sa02m-alice-devices present the device document is left alone (still 0600), the others get their modes")
            else:
                bad(f"9e HomeKit-owned device document: rc={r9e.returncode}, got {got}, want {want}")

# ── 10. the HomeKit bridge homes (docs/contracts/homekit-bridge.md §13-§14) ─
# The same lists, the second optional module: the conf is the operator's
# decision (preserved by OTA, archived by the backup, admitted by the restore),
# the state dir holds the pairing keys (preserved by OTA, NEVER archived — P4:
# the archive goes to the panel, and a restore to another board would clone the
# accessory). Every assertion runs the shipped code the sections above run.
print("── 10. HomeKit homes (preserve / backup / restore / OTA sudoers mode) ──")
HK_PKG = ROOT / "opt/sa02m-homekit"
HK_TMPFILES = ROOT / "opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf"
HK_INSTALLER = ROOT / "scripts/06c-homekit.sh"
env_hk = {k: v for k, v in os.environ.items() if not k.startswith("SA02M_HOMEKIT_")}
env_hk["PYTHONPATH"] = str(HK_PKG)
r = subprocess.run(
    [sys.executable, "-c",
     "from sa02m_homekit import constants as C;"
     "print(C.ETC_DIR); print(C.CONF_FILE); print(C.VAR_DIR); print(C.STATE_FILE); print(C.TMP_PREFIX)"],
    env=env_hk, capture_output=True, text=True, timeout=30,
)
hk = r.stdout.split()
if r.returncode != 0 or len(hk) != 5:
    bad(f"10.0 could not read the HomeKit layout from sa02m_homekit.constants (rc={r.returncode}): {r.stderr.strip()[:200]}")
else:
    HK_ETC, HK_CONF, HK_VAR, HK_STATE, HK_TMP = hk
    HK_SIDECAR = f"{HK_VAR}/{HK_TMP}Zq81xk.tmp"
    HK_CODE = "/opt/sa02m-homekit/sa02m_homekit/main.py"
    hk_tmp_txt = read(HK_TMPFILES)
    hk_inst = [l for l in read(HK_INSTALLER).splitlines() if not l.lstrip().startswith("#")]
    for d in (HK_ETC, HK_VAR):
        (ok if re.search(r"^d\s+" + re.escape(d) + r"\s", hk_tmp_txt, re.M) else bad)(
            f"10.0 tmpfiles creates {d} (the layout the lists must name)")
    HK_KEEP = [HK_CONF, HK_STATE, HK_SIDECAR]

    # 10.1-10.4 — the four OTA never-deploy guards
    if vp is not None:
        for p in HK_KEEP:
            (ok if vp.path_is_preserved(p) else bad)(f"10.1 path_is_preserved({p})")
        (bad if vp.path_is_preserved(HK_CODE) else ok)("10.1 negative control: the HomeKit code tree stays deployable")
    if m_tuple and m_pred:
        for p in HK_KEEP:
            (ok if hit(p) else bad)(f"10.2 runner bootstrap refuses a deploy onto {p}")
        (bad if hit(HK_CODE) else ok)("10.2 negative control: the HomeKit code tree stays deployable")
    else:
        bad("10.2 runner bootstrap guard not extracted (see 2)")
    arr_hk = []
    if m_arr:
        rb_hk = subprocess.run(["bash", "-c", "set -f\nPRESERVE_PATHS=(\n" + m_arr.group(1) + ")\nprintf '%s\\n' \"${PRESERVE_PATHS[@]}\"\n"],
                               capture_output=True, text=True, timeout=30)
        arr_hk = [l for l in rb_hk.stdout.splitlines() if l]
    if not arr_hk:
        bad("10.3 runner bash PRESERVE_PATHS not expanded (see 3)")
    else:
        for p in HK_KEEP:
            (ok if any(generic_hit(p, x) for x in arr_hk) else bad)(f"10.3 PRESERVE_PATHS covers {p}")
        (bad if any(generic_hit(HK_CODE, x) for x in arr_hk) else ok)("10.3 negative control: the HomeKit code tree is not in PRESERVE_PATHS")
    try:
        nd_hk = json.loads(read(DEPLOY_MAP) or "{}").get("never_deploy") or []
    except ValueError:
        nd_hk = []
    if len(nd_hk) < 5:
        bad(f"10.4 never_deploy has {len(nd_hk)} entries — the list was not found (non-vacuity)")
    else:
        for p in HK_KEEP:
            (ok if any(generic_hit(p, x) for x in nd_hk) else bad)(f"10.4 never_deploy covers {p}")
        (bad if any(generic_hit(HK_CODE, x) for x in nd_hk) else ok)("10.4 negative control: the HomeKit code tree is not in never_deploy")

    # 10.5 — the shipped collect_paths(): the conf yes, the pairing store never
    hk_emitted: list[str] = []
    if not m_fn:
        bad("10.5 collect_paths() not extracted (see 5)")
    else:
        fn_hk, _ = re.subn(r"(?<=\s)/etc/", '"$SANDBOX"/etc/', m_fn.group(0))
        fn_hk = re.sub(r"(?<=\s)/var/lib/", '"$SANDBOX"/var/lib/', fn_hk)
        with tempfile.TemporaryDirectory() as sb:
            for p in (HK_CONF, HK_STATE, HK_SIDECAR, f"{HK_VAR}/aids.json", f"{HK_VAR}/identity.json"):
                f = Path(sb + p)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x\n", encoding="utf-8")
            rbk = subprocess.run(["bash", "-c", fn_hk + "\ncollect_paths\n"], capture_output=True, text=True,
                                 timeout=30, env={**os.environ, "SANDBOX": sb})
            hk_emitted = [l[len(sb):] for l in rbk.stdout.splitlines() if l.startswith(sb)]
        (ok if HK_CONF in hk_emitted else bad)(f"10.5 the backup archives {HK_CONF}")
        leak = [p for p in hk_emitted if p.startswith(HK_VAR.rstrip("/") + "/")]
        (bad if leak else ok)(f"10.5 the backup never lists the pairing store (P4){': ' + str(leak) if leak else ''}")

    # 10.5e — the WHOLE backup end to end: a paired board's archive carries the
    # conf bytes and not one byte of the pairing store; a symlink planted at the
    # conf (its dir is www-data-writable like the Alice one) is skipped.
    if not (shutil.which("bash") and shutil.which("tar")) or "run_backup" not in globals():
        skip("10.5e needs bash + tar — not run on this host (a skip is not a pass)")
    else:
        HK_KEY = b"hk-donor-long-term-secret-key"

        def hk_backup_case(tag, what, plant, want_conf: bool, warn: bool):
            with tempfile.TemporaryDirectory() as tsb:
                sb = Path(tsb)
                e = seed_backup(sb)
                hdir = sb / HK_ETC.lstrip("/")
                hdir.mkdir(parents=True)
                hdir.chmod(0o770)
                (sb / HK_CONF.lstrip("/")).write_bytes(b"[bridge]\nenabled = true\n")
                vdir = sb / HK_VAR.lstrip("/")
                vdir.mkdir(parents=True)
                (sb / HK_STATE.lstrip("/")).write_bytes(HK_KEY + b"\n")
                (sb / HK_SIDECAR.lstrip("/")).write_bytes(HK_KEY + b"\n")
                plant(sb, e)
                # Retarget /var/lib/ too, or a HomeKit line naming it would
                # read the HOST's pairing store instead of the seeded one.
                global btxt
                saved = btxt
                btxt = btxt.replace(" /var/lib/", f" {sb}/var/lib/")
                try:
                    rc, err, files, raw, n_sub = run_backup(sb)
                finally:
                    btxt = saved
            problems = []
            if rc != 0:
                problems.append(f"rc={rc} ({err.strip()[-200:]!r})")
            if HK_KEY in raw:
                problems.append("the pairing store's bytes ARE in the archive")
            if MARK in raw:
                problems.append("the planted victim's bytes ARE in the archive")
            if want_conf and files.get(HK_CONF) != b"[bridge]\nenabled = true\n":
                problems.append(f"{HK_CONF} missing or wrong bytes ({files.get(HK_CONF)!r})")
            if not want_conf and HK_CONF in files:
                problems.append(f"{HK_CONF} archived")
            if warn and HK_CONF not in err:
                problems.append(f"no WARN naming {HK_CONF}")
            (bad if problems else ok)(f"{tag} {what}" + (": " + "; ".join(problems) if problems else ""))

        hk_backup_case("10.5e1", "a paired board's archive carries the HomeKit conf and none of the pairing store",
                       lambda sb, e: None, True, False)

        def hk_plant_symlink(sb, e):
            c = sb / HK_CONF.lstrip("/")
            c.unlink()
            c.symlink_to(e["victim"])
        hk_backup_case("10.5e2", "a symlink planted at the HomeKit conf is skipped with a WARN; the victim never reaches the archive",
                       hk_plant_symlink, False, True)

    # 10.6 — the restore admits the conf, refuses the pairing store, and admits
    # everything the backup emits for a HomeKit board.
    if allowed is None:
        bad("10.6 restore ALLOW not extracted (see 6)")
    else:
        (ok if allowed(HK_CONF) else bad)(f"10.6 the restore admits {HK_CONF}")
        for p in (HK_STATE, HK_SIDECAR):
            (bad if allowed(p) else ok)(f"10.6 the restore refuses {p} (a backup never carries the pairing store)")
        refused = [p for p in hk_emitted if not allowed(p)]
        if hk_emitted and not refused:
            ok(f"10.6 every path the backup emits on a HomeKit board passes the restore ({len(hk_emitted)} paths)")
        else:
            bad(f"10.6 backup/restore disagree on a HomeKit board (emitted={hk_emitted}, refused={refused})")

    # 10.7 — a missing conf dir is created with the tmpfiles owner/mode.
    hk_spec = None
    if blocks["DIR_SPEC"]:
        ns_hk: dict = {}
        exec(blocks["DIR_SPEC"], ns_hk)  # noqa: S102 - the shipped source under test
        hk_spec = ns_hk["DIR_SPEC"].get(HK_ETC)
    m_hk = re.search(r"^d\s+" + re.escape(HK_ETC) + r"\s+(\d+)\s+(\S+)\s+(\S+)", hk_tmp_txt, re.M)
    hk_want = (int(m_hk.group(1), 8), m_hk.group(2), m_hk.group(3)) if m_hk else None
    hk_inst_ok = any(f"install -d -m {oct(hk_want[0])[2:].zfill(4)} -o {hk_want[1]} -g {hk_want[2]} {HK_ETC}" in l
                     for l in hk_inst) if hk_want else False
    if hk_spec is not None and hk_spec == hk_want and hk_inst_ok:
        ok(f"10.7 a missing {HK_ETC} is created {oct(hk_spec[0])} {hk_spec[1]}:{hk_spec[2]} — tmpfiles and scripts/06c-homekit.sh agree")
    else:
        bad(f"10.7 restore DIR_SPEC[{HK_ETC}]={hk_spec} vs tmpfiles {hk_want} (installer agrees: {hk_inst_ok}) "
            "— a restore onto a board without the dir creates it unwritable for the CGI")

    # 10.8 — the SHIPPED restore end to end: the conf lands, no HomeKit unit is
    # touched (the daemon re-reads its conf every 2 s), and a symlink planted
    # at the conf fails the preflight with nothing written.
    if not (shutil.which("bash") and shutil.which("tar")) or "run_restore" not in globals():
        skip("10.8 needs bash + tar — not run on this host (a skip is not a pass)")
    else:
        with tempfile.TemporaryDirectory() as tsb:
            sb = Path(tsb)
            hdir = sb / HK_ETC.lstrip("/")
            hdir.mkdir(parents=True)
            hdir.chmod(0o770)
            conf = sb / HK_CONF.lstrip("/")
            conf.write_bytes(b"old\n")
            rc, out, calls, _ = run_restore(sb, {HK_CONF: b"[bridge]\nenabled = false\n"}, {"sa02m-homekit"})
            if rc == 0 and conf.read_bytes() == b"[bridge]\nenabled = false\n" and not [c for c in calls if "homekit" in c]:
                ok("10.8a a clean --apply restores the HomeKit conf and touches no HomeKit unit (the daemon re-reads it)")
            else:
                bad(f"10.8a HomeKit conf --apply: rc={rc}, bytes={conf.read_bytes()!r}, homekit calls="
                    f"{[c for c in calls if 'homekit' in c]}, output: {out.strip()[-300:]!r}")
            victim = sb / "victim"
            victim.write_bytes(b"root-secret\n")
            victim.chmod(0o600)
            conf.unlink()
            conf.symlink_to(victim)
            rc, out, calls, _ = run_restore(sb, {HK_CONF: b"[bridge]\nenabled = true\n"}, set())
            if rc != 0 and "symlink" in out and conf.is_symlink() and victim.read_bytes() == b"root-secret\n":
                ok("10.8b a symlink planted at the HomeKit conf fails --apply in the preflight; the victim is untouched")
            else:
                bad(f"10.8b planted HomeKit conf symlink: rc={rc}, still a link={conf.is_symlink()}, "
                    f"victim={victim.read_bytes()!r}, output: {out.strip()[-300:]!r}")

# 10.9 — the OTA lands every committed sudoers drop-in, the github-overlay path
# at 0644 (the runner's deploy_mode), and its cleanup step re-modes a NAMED list
# to 0440 (visudo -c flags anything else). Open world: every file under
# etc/sudoers.d/ must be in that list — a new drop-in (sa02m-homekit) left out
# ships as a visudo failure on every OTA'd board.
m_loop = re.search(r"^\s*for _name in ([^;\n]+); do\n\s*_path=\"/etc/sudoers\.d/\$_name\"\n\s*if \[ -f \"\$_path\" \]; then\n\s*chmod 0440",
                   rtxt, re.M)
committed = sorted(p.name for p in (ROOT / "etc/sudoers.d").iterdir() if p.is_file()) if (ROOT / "etc/sudoers.d").is_dir() else []
if not m_loop or not committed:
    bad(f"10.9 could not extract the runner's 0440 re-mode loop (found={bool(m_loop)}) or list etc/sudoers.d ({len(committed)} files) (non-vacuity)")
else:
    named = set(m_loop.group(1).split())
    missing_mode = [n for n in committed if n not in named]
    if missing_mode:
        bad(f"10.9 committed sudoers drop-ins the OTA never re-modes to 0440: {missing_mode}")
    else:
        ok(f"10.9 the OTA re-modes all {len(committed)} committed sudoers drop-ins to 0440 (sa02m-homekit included)")

# ── 11. HomeKit installer conf seed: root never writes/chmods through a plant ─
# scripts/06c-homekit.sh (root, on every install.sh refresh once the bridge is
# installed) seeds /etc/sa02m-homekit/sa02m-homekit.conf and re-asserts its
# owner/group/mode (www-data:sa02m-homekit 0640, contract §13) — in a directory
# www-data owns, the same door as section 9. The SHIPPED block between «Conf
# seed» and «systemd» runs with the conf dir retargeted into a sandbox,
# $BASE_DIR at a sandbox seed (written with CRLF, so the CR strip is measured
# too), the owner www-data replaced by the invoking user and $HK_USER (the
# group) by the invoking group.
print("── 11. HomeKit installer conf seed/modes (scripts/06c-homekit.sh) never follow a plant ──")
HK11_DIR = "/etc/sa02m-homekit"
HK11_NAME = "sa02m-homekit.conf"
HK11_SEED = ROOT / "etc/sa02m-homekit/sa02m-homekit.conf"
m11 = re.search(r"^# ── Conf seed[^\n]*\n(.*?)^# ── systemd", read(HK_INSTALLER), re.S | re.M)
if not m11 or HK11_DIR not in m11.group(1) or not HK11_SEED.is_file():
    bad(f"11 could not extract the conf seed block (between «Conf seed» and «systemd») from "
        f"{HK_INSTALLER.relative_to(ROOT)}, or the seed is missing (non-vacuity)")
else:
    import grp as grp11  # noqa: E402
    import stat as stat11  # noqa: E402
    import pwd as pwd11  # noqa: E402
    group11 = grp11.getgrgid(os.getegid()).gr_name
    user11 = pwd11.getpwuid(os.geteuid()).pw_name
    seed11 = HK11_SEED.read_bytes()

    def run_seed(sb: Path, cwd=None, env=None):
        base = sb / "base"
        (base / "etc/sa02m-homekit").mkdir(parents=True)
        (base / "etc/sa02m-homekit" / HK11_NAME).write_bytes(seed11.replace(b"\n", b"\r\n"))
        # Only the LIVE path is retargeted — not the seed source under $BASE_DIR/etc/.
        block = re.sub(r"(?<![\w}])" + re.escape(HK11_DIR), str(sb / "sa02m-homekit"), m11.group(1))
        block = block.replace("www-data", user11)
        script = ("set -euo pipefail\nlog() { printf '%s\\n' \"$*\"; }\nBASE_DIR=" + str(base)
                  + "\nHK_USER=" + group11 + "\n" + block)
        try:
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=cwd,
                               env=env)
        except subprocess.TimeoutExpired:
            return 124, "timed out (a FIFO opened blocking?)"
        return r.returncode, r.stdout + r.stderr

    def seed_dir(sb: Path) -> dict:
        hdir = sb / "sa02m-homekit"
        hdir.mkdir()
        hdir.chmod(0o770)
        vdir = sb / "victim"
        vdir.mkdir(mode=0o700)
        victim = vdir / "shadow"
        victim.write_bytes(b"root-secret\n")
        victim.chmod(0o600)
        return {"conf": hdir / HK11_NAME, "victim": victim, "vdir": vdir}

    def warned11(out: str) -> bool:
        return any("WARN" in ln and HK11_NAME in ln for ln in out.splitlines())

    def state11(p: Path):
        st = os.lstat(p)
        return (stat11.S_IMODE(st.st_mode), st.st_gid, p.read_bytes(), st.st_uid)

    my_gid = os.getegid()
    my_uid = os.geteuid()
    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir(sb)
        rc, out = run_seed(sb)
        c = e["conf"]
        if rc == 0 and c.is_file() and not c.is_symlink() and state11(c) == (0o640, my_gid, seed11, my_uid):
            ok("11a an absent conf is seeded: the seed bytes (CR stripped), 0640, owner www-data, group sa02m-homekit")
        else:
            got = state11(c) if c.exists() else None
            bad(f"11a seed: rc={rc}, conf={got!r}, want (0o640, {my_gid}, seed bytes, {my_uid}): {out.strip()[-200:]!r}")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir(sb)
        e["conf"].write_bytes(b"[bridge]\nenabled = true\n")
        e["conf"].chmod(0o600)
        rc, out = run_seed(sb)
        (ok if rc == 0 and state11(e["conf"]) == (0o640, my_gid, b"[bridge]\nenabled = true\n", my_uid) else bad)(
            f"11b an existing regular conf keeps its bytes and gets 0640 + www-data:sa02m-homekit back (rc={rc})")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir(sb)
        e["conf"].symlink_to(e["victim"])
        before = state11(e["victim"])
        rc, out = run_seed(sb)
        after = state11(e["victim"])
        if rc == 0 and before == after and e["conf"].is_symlink() and warned11(out):
            ok("11c a symlink planted at the conf is reported and left alone; its target's bytes/mode/group unchanged")
        else:
            bad(f"11c planted symlink: rc={rc}, target {oct(before[0])}/{before[1]} → {oct(after[0])}/{after[1]}, "
                f"bytes changed={before[2] != after[2]}, reported={warned11(out)} — root followed a www-data plant")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir(sb)
        os.link(e["victim"], e["conf"])
        before = state11(e["victim"])
        rc, out = run_seed(sb)
        after = state11(e["victim"])
        (ok if rc == 0 and before == after and warned11(out) else bad)(
            f"11d a hard link planted at the conf is reported and left alone (rc={rc}, "
            f"mode {oct(before[0])} → {oct(after[0])}, reported={warned11(out)})")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir(sb)
        target = e["vdir"] / "sudoers-x"
        e["conf"].symlink_to(target)
        rc, out = run_seed(sb)
        (ok if rc == 0 and not target.exists() else bad)(
            f"11e a dangling symlink planted at the conf is not written through (rc={rc}, target created={target.exists()})")

    # The block must run isolated (`python3 -I -`): root runs it from the
    # operator's shell, whose cwd and environment are not ours. Plants on both
    # paths: a `sitecustomize.py` + `grp.py` in the cwd, and the same dir on an
    # inherited PYTHONPATH. Measured on CPython 3.11: the cwd half alone is inert
    # for THIS block (`grp` is built in, `os`/`stat` are preloaded, `site` runs
    # before sys.path[0] exists) — the PYTHONPATH `sitecustomize` is what runs
    # under a bare `python3 -` and turns this case RED; `-I` ignores both.
    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir(sb)
        cwd11 = sb / "cwd"
        cwd11.mkdir()
        marker11 = sb / "planted-module-ran"
        plant11 = f"open({str(marker11)!r}, 'w').close()\n"
        (cwd11 / "sitecustomize.py").write_text(plant11)
        (cwd11 / "grp.py").write_text(plant11 + "raise ImportError('planted grp.py')\n")
        env11 = dict(os.environ, PYTHONPATH=str(cwd11))
        rc, out = run_seed(sb, cwd=cwd11, env=env11)
        c = e["conf"]
        seeded = c.is_file() and not c.is_symlink() and state11(c) == (0o640, my_gid, seed11, my_uid)
        (ok if rc == 0 and not marker11.exists() and seeded else bad)(
            f"11f the seed block runs isolated (python3 -I): a planted sitecustomize/grp on the cwd or PYTHONPATH "
            f"never runs and the seed still lands (rc={rc}, planted module ran={marker11.exists()}, seeded={seeded})")

# ── 12. the HomeKit tmpfiles conf reaches /etc/tmpfiles.d/ only via 06c ─────
# Everything under etc/tmpfiles.d/ in this tree is shipped by the OTA (runner
# map_dst, the offline deploy map) to EVERY board, and systemd-tmpfiles applies
# it at every boot. The HomeKit conf names the `sa02m-homekit` account, which
# exists only where scripts/06c-homekit.sh ran: on any other board its lines
# fail to resolve at every boot and it creates an empty /etc/sa02m-homekit.
# So the conf lives in the package (inert under /opt) and only the installer
# copies it into /etc/tmpfiles.d/ (review advisory A1, 1.0.6.57).
print("── 12. the HomeKit tmpfiles conf is installer-owned, never OTA'd into /etc/tmpfiles.d ──")
HK12_SRC = "opt/sa02m-homekit/tmpfiles.d/sa02m-homekit.conf"
runner12 = read(RUNNER)
map12 = None
m12 = re.search(r"^MPLC_OTA_PLUGINS = .*?(?=^def deploy_mode)", runner12, re.M | re.S)
if m12:
    ns12 = {"Path": Path}
    try:
        exec(m12.group(0), ns12)  # noqa: S102 — the runner's own source is the contract
        map12 = ns12.get("map_dst")
    except Exception as e12:  # noqa: BLE001
        bad(f"12 could not evaluate map_dst() out of the runner: {e12}")
tmpfiles_dir = ROOT / "etc/tmpfiles.d"
shipped = sorted(p for p in tmpfiles_dir.iterdir() if p.is_file()) if tmpfiles_dir.is_dir() else []
if map12 is None or not shipped:
    bad(f"12 non-vacuity: map_dst extracted={map12 is not None}, etc/tmpfiles.d files={len(shipped)}")
else:
    ota_tmpfiles = [p for p in shipped if (map12(f"etc/tmpfiles.d/{p.name}") or "").startswith("/etc/tmpfiles.d/")]
    (ok if ota_tmpfiles else bad)(
        f"12 control: the OTA maps etc/tmpfiles.d/* onto /etc/tmpfiles.d/ ({len(ota_tmpfiles)} of {len(shipped)} files) — the premise holds")
    naming = [p.name for p in ota_tmpfiles
              if re.search(r"(?m)^[^#\n]*\ssa02m-homekit(\s|$)", read(p))]
    (bad if naming else ok)(
        f"12a no OTA-shipped tmpfiles conf names the sa02m-homekit account{': ' + str(naming) if naming else ''}")
    if not (ROOT / HK12_SRC).is_file():
        bad(f"12b the package home {HK12_SRC} is missing — the installer has nothing to copy")
    else:
        dst12 = map12(HK12_SRC) or ""
        (ok if dst12.startswith("/opt/sa02m-homekit/") else bad)(
            f"12b the OTA lands the package copy inert under /opt/sa02m-homekit/ (map_dst → {dst12!r})")
        inst12 = "\n".join(l for l in read(HK_INSTALLER).splitlines() if not l.lstrip().startswith("#"))
        (ok if re.search(r"tmpfiles\.d/sa02m-homekit\.conf\"?\s+/etc/tmpfiles\.d/sa02m-homekit\.conf", inst12)
            and "$OPT_SRC/tmpfiles.d/sa02m-homekit.conf" in inst12 else bad)(
            "12c scripts/06c-homekit.sh installs the package copy into /etc/tmpfiles.d/ (the only writer)")

# ── 13. the Home Connect client homes (docs/contracts/home-connect.md §11-§12) ─
# The third optional module, the same lists: the conf is the operator's
# decision (enabled + the integrator's Client ID — not a secret: preserved by
# OTA, archived by the backup, admitted by the restore); the state dir holds
# the OAuth tokens of the owner's BSH account and the call budget (preserved by
# OTA, NEVER archived — P4: the archive goes to the panel, and a restore onto
# another board would read the donor household's appliances).
print("── 13. Home Connect homes (preserve / backup / restore) ──")
HC_PKG = ROOT / "opt/sa02m-homeconnect"
HC_TMPFILES = ROOT / "opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf"
HC_INSTALLER = ROOT / "scripts/06d-homeconnect.sh"
env_hc = {k: v for k, v in os.environ.items() if not k.startswith("SA02M_HOMECONNECT_")}
env_hc["PYTHONPATH"] = str(HC_PKG)
r = subprocess.run(
    [sys.executable, "-c",
     "from sa02m_homeconnect import constants as C;"
     "print(C.ETC_DIR); print(C.CONF_FILE); print(C.VAR_DIR); print(C.TOKENS_FILE); print(C.BUDGET_FILE); print(C.TMP_PREFIX)"],
    env=env_hc, capture_output=True, text=True, timeout=30,
)
hcv = r.stdout.split()
if r.returncode != 0 or len(hcv) != 6:
    bad(f"13.0 could not read the Home Connect layout from sa02m_homeconnect.constants (rc={r.returncode}): {r.stderr.strip()[:200]}")
else:
    HC_ETC, HC_CONF, HC_VAR, HC_TOKENS, HC_BUDGET, HC_TMP = hcv
    HC_SIDECAR = f"{HC_VAR}/{HC_TMP}Zq81xk.tmp"
    HC_CODE = "/opt/sa02m-homeconnect/sa02m_homeconnect/main.py"
    hc_tmp_txt = read(HC_TMPFILES)
    hc_inst = [l for l in read(HC_INSTALLER).splitlines() if not l.lstrip().startswith("#")]
    for d in (HC_ETC, HC_VAR):
        (ok if re.search(r"^d\s+" + re.escape(d) + r"\s", hc_tmp_txt, re.M) else bad)(
            f"13.0 tmpfiles creates {d} (the layout the lists must name)")
    HC_KEEP = [HC_CONF, HC_TOKENS, HC_BUDGET, HC_SIDECAR]

    # 13.1-13.4 — the four OTA never-deploy guards
    if vp is not None:
        for p in HC_KEEP:
            (ok if vp.path_is_preserved(p) else bad)(f"13.1 path_is_preserved({p})")
        (bad if vp.path_is_preserved(HC_CODE) else ok)("13.1 negative control: the Home Connect code tree stays deployable")
    if m_tuple and m_pred:
        for p in HC_KEEP:
            (ok if hit(p) else bad)(f"13.2 runner bootstrap refuses a deploy onto {p}")
        (bad if hit(HC_CODE) else ok)("13.2 negative control: the Home Connect code tree stays deployable")
    else:
        bad("13.2 runner bootstrap guard not extracted (see 2)")
    arr_hc = []
    if m_arr:
        rb_hc = subprocess.run(["bash", "-c", "set -f\nPRESERVE_PATHS=(\n" + m_arr.group(1) + ")\nprintf '%s\\n' \"${PRESERVE_PATHS[@]}\"\n"],
                               capture_output=True, text=True, timeout=30)
        arr_hc = [l for l in rb_hc.stdout.splitlines() if l]
    if not arr_hc:
        bad("13.3 runner bash PRESERVE_PATHS not expanded (see 3)")
    else:
        for p in HC_KEEP:
            (ok if any(generic_hit(p, x) for x in arr_hc) else bad)(f"13.3 PRESERVE_PATHS covers {p}")
        (bad if any(generic_hit(HC_CODE, x) for x in arr_hc) else ok)("13.3 negative control: the Home Connect code tree is not in PRESERVE_PATHS")
    try:
        nd_hc = json.loads(read(DEPLOY_MAP) or "{}").get("never_deploy") or []
    except ValueError:
        nd_hc = []
    if len(nd_hc) < 5:
        bad(f"13.4 never_deploy has {len(nd_hc)} entries — the list was not found (non-vacuity)")
    else:
        for p in HC_KEEP:
            (ok if any(generic_hit(p, x) for x in nd_hc) else bad)(f"13.4 never_deploy covers {p}")
        (bad if any(generic_hit(HC_CODE, x) for x in nd_hc) else ok)("13.4 negative control: the Home Connect code tree is not in never_deploy")

    # 13.5 — the shipped collect_paths(): the conf yes, the state dir never
    hc_emitted: list[str] = []
    if not m_fn:
        bad("13.5 collect_paths() not extracted (see 5)")
    else:
        fn_hc, _ = re.subn(r"(?<=\s)/etc/", '"$SANDBOX"/etc/', m_fn.group(0))
        fn_hc = re.sub(r"(?<=\s)/var/lib/", '"$SANDBOX"/var/lib/', fn_hc)
        with tempfile.TemporaryDirectory() as sb:
            for p in (HC_CONF, HC_TOKENS, HC_BUDGET, HC_SIDECAR, f"{HC_VAR}/appliances.json"):
                f = Path(sb + p)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x\n", encoding="utf-8")
            rbk = subprocess.run(["bash", "-c", fn_hc + "\ncollect_paths\n"], capture_output=True, text=True,
                                 timeout=30, env={**os.environ, "SANDBOX": sb})
            hc_emitted = [l[len(sb):] for l in rbk.stdout.splitlines() if l.startswith(sb)]
        (ok if HC_CONF in hc_emitted else bad)(f"13.5 the backup archives {HC_CONF}")
        leak = [p for p in hc_emitted if p.startswith(HC_VAR.rstrip("/") + "/")]
        (bad if leak else ok)(f"13.5 the backup never lists the token store (P4){': ' + str(leak) if leak else ''}")

    # 13.5e — the WHOLE backup end to end: a signed-in board's archive carries
    # the conf bytes and not one byte of the tokens; a symlink planted at the
    # conf (its dir is www-data-writable) is skipped with a WARN.
    if not (shutil.which("bash") and shutil.which("tar")) or "run_backup" not in globals():
        skip("13.5e needs bash + tar — not run on this host (a skip is not a pass)")
    else:
        HC_SECRET = b"hc-donor-refresh-token-secret"
        HC_CONF_BYTES = b"[account]\nenabled = true\nclient_id = ABCDEFGH12345678\n"

        def hc_backup_case(tag, what, plant, want_conf: bool, warn: bool):
            with tempfile.TemporaryDirectory() as tsb:
                sb = Path(tsb)
                e = seed_backup(sb)
                hdir = sb / HC_ETC.lstrip("/")
                hdir.mkdir(parents=True)
                hdir.chmod(0o770)
                (sb / HC_CONF.lstrip("/")).write_bytes(HC_CONF_BYTES)
                vdir = sb / HC_VAR.lstrip("/")
                vdir.mkdir(parents=True)
                for p in (HC_TOKENS, HC_SIDECAR):
                    (sb / p.lstrip("/")).write_bytes(HC_SECRET + b"\n")
                plant(sb, e)
                global btxt
                saved = btxt
                btxt = btxt.replace(" /var/lib/", f" {sb}/var/lib/")
                try:
                    rc, err, files, raw, n_sub = run_backup(sb)
                finally:
                    btxt = saved
            problems = []
            if rc != 0:
                problems.append(f"rc={rc} ({err.strip()[-200:]!r})")
            if HC_SECRET in raw:
                problems.append("the token store's bytes ARE in the archive")
            if MARK in raw:
                problems.append("the planted victim's bytes ARE in the archive")
            if want_conf and files.get(HC_CONF) != HC_CONF_BYTES:
                problems.append(f"{HC_CONF} missing or wrong bytes ({files.get(HC_CONF)!r})")
            if not want_conf and HC_CONF in files:
                problems.append(f"{HC_CONF} archived")
            if warn and HC_CONF not in err:
                problems.append(f"no WARN naming {HC_CONF}")
            (bad if problems else ok)(f"{tag} {what}" + (": " + "; ".join(problems) if problems else ""))

        hc_backup_case("13.5e1", "a signed-in board's archive carries the Home Connect conf and none of the tokens",
                       lambda sb, e: None, True, False)

        def hc_plant_symlink(sb, e):
            c = sb / HC_CONF.lstrip("/")
            c.unlink()
            c.symlink_to(e["victim"])
        hc_backup_case("13.5e2", "a symlink planted at the Home Connect conf is skipped with a WARN; the victim never reaches the archive",
                       hc_plant_symlink, False, True)

    # 13.6 — the restore admits the conf, refuses the state dir, and admits
    # everything the backup emits for a Home Connect board.
    if allowed is None:
        bad("13.6 restore ALLOW not extracted (see 6)")
    else:
        (ok if allowed(HC_CONF) else bad)(f"13.6 the restore admits {HC_CONF}")
        for p in (HC_TOKENS, HC_BUDGET, HC_SIDECAR):
            (bad if allowed(p) else ok)(f"13.6 the restore refuses {p} (a backup never carries the token store)")
        refused = [p for p in hc_emitted if not allowed(p)]
        if hc_emitted and not refused:
            ok(f"13.6 every path the backup emits on a Home Connect board passes the restore ({len(hc_emitted)} paths)")
        else:
            bad(f"13.6 backup/restore disagree on a Home Connect board (emitted={hc_emitted}, refused={refused})")

    # 13.7 — a missing conf dir is created with the tmpfiles owner/mode.
    hc_spec = None
    if blocks["DIR_SPEC"]:
        ns_hc: dict = {}
        exec(blocks["DIR_SPEC"], ns_hc)  # noqa: S102 - the shipped source under test
        hc_spec = ns_hc["DIR_SPEC"].get(HC_ETC)
    m_hc = re.search(r"^d\s+" + re.escape(HC_ETC) + r"\s+(\d+)\s+(\S+)\s+(\S+)", hc_tmp_txt, re.M)
    hc_want = (int(m_hc.group(1), 8), m_hc.group(2), m_hc.group(3)) if m_hc else None
    hc_inst_ok = any(f"install -d -m {oct(hc_want[0])[2:].zfill(4)} -o {hc_want[1]} -g {hc_want[2]} {HC_ETC}" in l
                     for l in hc_inst) if hc_want else False
    if hc_spec is not None and hc_spec == hc_want and hc_inst_ok:
        ok(f"13.7 a missing {HC_ETC} is created {oct(hc_spec[0])} {hc_spec[1]}:{hc_spec[2]} — tmpfiles and scripts/06d-homeconnect.sh agree")
    else:
        bad(f"13.7 restore DIR_SPEC[{HC_ETC}]={hc_spec} vs tmpfiles {hc_want} (installer agrees: {hc_inst_ok}) "
            "— a restore onto a board without the dir creates it unwritable for the CGI")

    # 13.8 — the SHIPPED restore end to end: the conf lands, no Home Connect
    # unit is touched (the client re-reads its conf every 2 s), and a symlink
    # planted at the conf fails the preflight with nothing written.
    if not (shutil.which("bash") and shutil.which("tar")) or "run_restore" not in globals():
        skip("13.8 needs bash + tar — not run on this host (a skip is not a pass)")
    else:
        with tempfile.TemporaryDirectory() as tsb:
            sb = Path(tsb)
            hdir = sb / HC_ETC.lstrip("/")
            hdir.mkdir(parents=True)
            hdir.chmod(0o770)
            conf = sb / HC_CONF.lstrip("/")
            conf.write_bytes(b"old\n")
            new = b"[account]\nenabled = false\nclient_id = ABCDEFGH12345678\n"
            rc, out, calls, _ = run_restore(sb, {HC_CONF: new}, {"sa02m-homeconnect"})
            if rc == 0 and conf.read_bytes() == new and not [c for c in calls if "homeconnect" in c]:
                ok("13.8a a clean --apply restores the Home Connect conf and touches no Home Connect unit (the client re-reads it)")
            else:
                bad(f"13.8a Home Connect conf --apply: rc={rc}, bytes={conf.read_bytes()!r}, homeconnect calls="
                    f"{[c for c in calls if 'homeconnect' in c]}, output: {out.strip()[-300:]!r}")
            victim = sb / "victim"
            victim.write_bytes(b"root-secret\n")
            victim.chmod(0o600)
            conf.unlink()
            conf.symlink_to(victim)
            rc, out, calls, _ = run_restore(sb, {HC_CONF: b"[account]\nenabled = true\n"}, set())
            if rc != 0 and "symlink" in out and conf.is_symlink() and victim.read_bytes() == b"root-secret\n":
                ok("13.8b a symlink planted at the Home Connect conf fails --apply in the preflight; the victim is untouched")
            else:
                bad(f"13.8b planted Home Connect conf symlink: rc={rc}, still a link={conf.is_symlink()}, "
                    f"victim={victim.read_bytes()!r}, output: {out.strip()[-300:]!r}")

# ── 14. Home Connect installer conf seed: root never writes/chmods through a plant ─
# scripts/06d-homeconnect.sh (root, on every install.sh refresh once the client
# is installed) seeds /etc/sa02m-homeconnect/sa02m-homeconnect.conf from the
# INSTALLED package's own render() of the defaults and re-asserts its
# owner/group/mode (www-data:sa02m-homeconnect 0640, contract §11) — in a
# directory www-data owns, the section-9/11 door. The SHIPPED block between
# «Conf seed» and «systemd» runs with the conf dir retargeted into a sandbox,
# $INSTALL_DIR at this checkout's package, the owner www-data replaced by the
# invoking user and $HC_USER (the group) by the invoking group.
print("── 14. Home Connect installer conf seed/modes (scripts/06d-homeconnect.sh) never follow a plant ──")
HC14_DIR = "/etc/sa02m-homeconnect"
HC14_NAME = "sa02m-homeconnect.conf"
HC14_PKG = ROOT / "opt/sa02m-homeconnect"
m14 = re.search(r"^# ── Conf seed[^\n]*\n(.*?)^# ── systemd", read(HC14_INSTALLER := ROOT / "scripts/06d-homeconnect.sh"), re.S | re.M)
r14 = subprocess.run([sys.executable, "-c",
                      "import sys; from sa02m_homeconnect import config;"
                      "sys.stdout.write(config.render(config.ClientConfig()))"],
                     env={**{k: v for k, v in os.environ.items() if not k.startswith("SA02M_HOMECONNECT_")},
                          "PYTHONPATH": str(HC14_PKG)}, capture_output=True, text=True, timeout=30)
if not m14 or HC14_DIR not in m14.group(1) or r14.returncode != 0 or "enabled = false" not in r14.stdout:
    bad(f"14 could not extract the conf seed block (between «Conf seed» and «systemd») from "
        f"{HC14_INSTALLER.relative_to(ROOT)}, or the package cannot render its defaults "
        f"(rc={r14.returncode}: {r14.stderr.strip()[-160:]!r}) (non-vacuity)")
else:
    import grp as grp14  # noqa: E402
    import stat as stat14  # noqa: E402
    import pwd as pwd14  # noqa: E402
    group14 = grp14.getgrgid(os.getegid()).gr_name
    user14 = pwd14.getpwuid(os.geteuid()).pw_name
    seed14 = r14.stdout.encode("utf-8")

    def run_seed14(sb: Path, cwd=None, env=None, pkg: Path = HC14_PKG):
        block = re.sub(r"(?<![\w}])" + re.escape(HC14_DIR), str(sb / "sa02m-homeconnect"), m14.group(1))
        block = block.replace("www-data", user14)
        script = ("set -euo pipefail\nlog() { printf '%s\\n' \"$*\"; }\nINSTALL_DIR=" + str(pkg)
                  + "\nHC_USER=" + group14 + "\n" + block)
        try:
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return 124, "timed out (a FIFO opened blocking?)"
        return r.returncode, r.stdout + r.stderr

    def seed_dir14(sb: Path) -> dict:
        hdir = sb / "sa02m-homeconnect"
        hdir.mkdir()
        hdir.chmod(0o770)
        vdir = sb / "victim"
        vdir.mkdir(mode=0o700)
        victim = vdir / "shadow"
        victim.write_bytes(b"root-secret\n")
        victim.chmod(0o600)
        return {"conf": hdir / HC14_NAME, "victim": victim, "vdir": vdir}

    def warned14(out: str) -> bool:
        return any("WARN" in ln and HC14_NAME in ln for ln in out.splitlines())

    def state14(p: Path):
        st = os.lstat(p)
        return (stat14.S_IMODE(st.st_mode), st.st_gid, p.read_bytes(), st.st_uid)

    gid14 = os.getegid()
    uid14 = os.geteuid()
    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        rc, out = run_seed14(sb)
        c = e["conf"]
        if rc == 0 and c.is_file() and not c.is_symlink() and state14(c) == (0o640, gid14, seed14, uid14):
            ok("14a an absent conf is seeded: the package's own render() of the defaults (enabled = false), 0640, "
               "owner www-data, group sa02m-homeconnect")
        else:
            got = state14(c) if c.exists() else None
            bad(f"14a seed: rc={rc}, conf={got!r}, want (0o640, {gid14}, render bytes, {uid14}): {out.strip()[-200:]!r}")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        mine = b"[account]\nenabled = true\nclient_id = ABCDEFGH12345678\n"
        e["conf"].write_bytes(mine)
        e["conf"].chmod(0o600)
        rc, out = run_seed14(sb)
        (ok if rc == 0 and state14(e["conf"]) == (0o640, gid14, mine, uid14) else bad)(
            f"14b an existing regular conf keeps its bytes (the Client ID) and gets 0640 + www-data:sa02m-homeconnect back (rc={rc})")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        e["conf"].symlink_to(e["victim"])
        before = state14(e["victim"])
        rc, out = run_seed14(sb)
        after = state14(e["victim"])
        if rc == 0 and before == after and e["conf"].is_symlink() and warned14(out):
            ok("14c a symlink planted at the conf is reported and left alone; its target's bytes/mode/group unchanged")
        else:
            bad(f"14c planted symlink: rc={rc}, target {oct(before[0])}/{before[1]} → {oct(after[0])}/{after[1]}, "
                f"bytes changed={before[2] != after[2]}, reported={warned14(out)} — root followed a www-data plant")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        os.link(e["victim"], e["conf"])
        before = state14(e["victim"])
        rc, out = run_seed14(sb)
        after = state14(e["victim"])
        (ok if rc == 0 and before == after and warned14(out) else bad)(
            f"14d a hard link planted at the conf is reported and left alone (rc={rc}, "
            f"mode {oct(before[0])} → {oct(after[0])}, reported={warned14(out)})")

    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        target = e["vdir"] / "sudoers-x"
        e["conf"].symlink_to(target)
        rc, out = run_seed14(sb)
        (ok if rc == 0 and not target.exists() else bad)(
            f"14e a dangling symlink planted at the conf is not written through (rc={rc}, target created={target.exists()})")

    # Isolation (`python3 -I`), measured like 11f: a `sitecustomize.py` on an
    # inherited PYTHONPATH runs under a bare `python3 -` and turns this RED.
    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        cwd14 = sb / "cwd"
        cwd14.mkdir()
        marker14 = sb / "planted-module-ran"
        plant14 = f"open({str(marker14)!r}, 'w').close()\n"
        (cwd14 / "sitecustomize.py").write_text(plant14)
        (cwd14 / "grp.py").write_text(plant14 + "raise ImportError('planted grp.py')\n")
        env14 = dict(os.environ, PYTHONPATH=str(cwd14))
        rc, out = run_seed14(sb, cwd=cwd14, env=env14)
        c = e["conf"]
        seeded = c.is_file() and not c.is_symlink() and state14(c) == (0o640, gid14, seed14, uid14)
        (ok if rc == 0 and not marker14.exists() and seeded else bad)(
            f"14f the seed block runs isolated (python3 -I): a planted sitecustomize/grp on the cwd or PYTHONPATH "
            f"never runs and the seed still lands (rc={rc}, planted module ran={marker14.exists()}, seeded={seeded})")

    # A package that cannot render (absent/broken install): no half-made conf
    # is left behind — the O_EXCL file is removed again, the installer WARNs
    # and goes on (the next run seeds it).
    with tempfile.TemporaryDirectory() as tsb:
        sb = Path(tsb)
        e = seed_dir14(sb)
        empty = sb / "no-package"
        empty.mkdir()
        rc, out = run_seed14(sb, pkg=empty)
        (ok if rc == 0 and not e["conf"].exists() and "WARN" in out else bad)(
            f"14g an unrenderable package leaves no empty conf behind and WARNs (rc={rc}, "
            f"conf left={e['conf'].exists()}, warned={'WARN' in out})")

# ── 15. the Home Connect tmpfiles conf reaches /etc/tmpfiles.d/ only via 06d ─
# The section-12 reason, the second module: its lines name the
# `sa02m-homeconnect` account, which exists only where 06d ran.
print("── 15. the Home Connect tmpfiles conf is installer-owned, never OTA'd into /etc/tmpfiles.d ──")
HC15_SRC = "opt/sa02m-homeconnect/tmpfiles.d/sa02m-homeconnect.conf"
if map12 is None or not shipped:
    bad(f"15 non-vacuity: map_dst extracted={map12 is not None}, etc/tmpfiles.d files={len(shipped)}")
else:
    naming15 = [p.name for p in shipped if (map12(f"etc/tmpfiles.d/{p.name}") or "").startswith("/etc/tmpfiles.d/")
                and re.search(r"(?m)^[^#\n]*\ssa02m-homeconnect(\s|$)", read(p))]
    (bad if naming15 else ok)(
        f"15a no OTA-shipped tmpfiles conf names the sa02m-homeconnect account{': ' + str(naming15) if naming15 else ''}")
    if not (ROOT / HC15_SRC).is_file():
        bad(f"15b the package home {HC15_SRC} is missing — the installer has nothing to copy")
    else:
        dst15 = map12(HC15_SRC) or ""
        (ok if dst15.startswith("/opt/sa02m-homeconnect/") else bad)(
            f"15b the OTA lands the package copy inert under /opt/sa02m-homeconnect/ (map_dst → {dst15!r})")
        inst15 = "\n".join(l for l in read(ROOT / "scripts/06d-homeconnect.sh").splitlines() if not l.lstrip().startswith("#"))
        (ok if re.search(r"tmpfiles\.d/sa02m-homeconnect\.conf\"?\s+/etc/tmpfiles\.d/sa02m-homeconnect\.conf", inst15)
            and "$OPT_SRC/tmpfiles.d/sa02m-homeconnect.conf" in inst15 else bad)(
            "15c scripts/06d-homeconnect.sh installs the package copy into /etc/tmpfiles.d/ (the only writer)")


print("")
if fails:
    print(f"alice-conf-homes: {fails} FAILURE(S)")
    sys.exit(1)
print("alice-conf-homes: ALL OK")
