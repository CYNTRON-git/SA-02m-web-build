#!/usr/bin/env python3
"""Codemod: live-path `install -m` sites -> `sa02m_atomic_install -m` (scripts/lib.sh).

Why a script (web-workflow.md, Rule of 500): the move touches dozens of sites
across scripts/*.sh and install.sh, so it must be re-runnable and reviewable as
ONE rule, not as hand edits. The rule: an `install -m ...` command whose
DESTINATION is a live path the running system reads without a restart — a systemd unit or
drop-in under /etc/systemd/system/, or a helper under /usr/local/{bin,sbin,
lib,libexec}/ — is rewritten to the atomic helper, which lands the file as
tmp + fsync + rename-over (old-or-new, never a 0-byte file). Everything else
(/etc confs, udev rules, logrotate, sudoers, /opt payloads, variable targets)
is left alone: those have their own guards or are re-read only at boot.

Continuation lines (`install -m 755 "$SRC" \\` + `    /usr/local/sbin/x`) are
joined for classification; only the FIRST physical line is rewritten. Options
(-m/-o/-g) pass through — the helper accepts the same ones. Idempotent: an
already-rewritten line (`sa02m_atomic_install`) never matches again.

Modes:
  --list    print every in-scope site still on raw `install -m` (no write)
  --check   exit 1 when any such site remains (the gate form; prints them)
  --all     with --list: also print the already-converted sites (non-vacuity
            evidence for the harness — the sweep sees files)
  (default) rewrite in place; prints the sites it changed

Scope: scripts/*.sh + install.sh - every shell file that can reach
sa02m_atomic_install, which lives in scripts/lib.sh and is sourced out of the
EXTRACTED install tree (install.sh:77). scripts/ is never deployed to the
device, so a script under etc/ - which runs standalone on the board, sourcing
only its own /usr/local/lib/sa02m-web-*-lib.sh - cannot call THIS helper. That
is the only thing "out of scope" means here: a device-side script writes live
paths atomically by carrying its OWN copy of the shape, and three already do
(etc/sa02m-update-runner.sh:1010 atomic_install_file, tmp + fdatasync + mv +
dir fsync; etc/sa02m-factory-reset-runner.sh:321 atomic_install_file, the same
shape plus the wipe allow-list and rollback journal that file owns;
etc/sa02m-web-update-apply.sh:59 atomic_install_script, the same shape with the
CRLF normalisation folded into the staged copy). They are NOT byte-identical
and there is no cmp pin between them - each is scoped to its own caller's
duties, which is why a fourth copy is a decision, not a formality.

Live-path `install -m` sites still under etc/, complete as of 1.0.6.41 (found
by `grep -rn 'install -m' etc/` and resolving every destination, including the
variable ones, to what it holds at run time):

    etc/sa02m-web-service-ctl.sh:1359 -> /etc/systemd/system/nodered.service
        A unit write - the incident's own shape. Survives because this file
        carries no atomic helper yet; converting it means a fourth copy of the
        shape (or a device-side lib the file already sources) plus its own
        drive-to-failure. NOT closed, and not blocked by anything but the work.

    etc/sa02m-update-runner.sh:1293 -> "$rel", an absolute path replayed from
        the pre-update rollback archive; its members are the manifest's
        deploy[].dst entries (build_rollback_archive, :968-985), so /usr/local/**
        and /etc/systemd/system/** are exactly what it restores. Survives only
        because nobody looked: this file DEFINES atomic_install_file 283 lines
        above, so the conversion needs no new helper - and this is the site that
        runs when the board is already mid-failure.

Everything else under etc/ that matches `install -m` writes a destination that
is not a live path, so it is outside the rule rather than an exception to it:

    etc/sa02m-web-update-apply.sh:391   -> /etc/tmpfiles.d/*        (read by
        systemd-tmpfiles on demand, never mid-flight)
    etc/sa02m-web-update-apply.sh:427   -> /etc/sudoers.d/sa02m-www (staged and
        visudo -c-validated first; re-read per sudo invocation)
    etc/sa02m-web-service-ctl.sh:895,898 -> /opt/mplc4/*.so         (re-read on
        MPLC4 restart, and the pack is stopped around the write)
    etc/sa02m-commit-web-env.sh:14      -> /etc/sa02m_web.env
    etc/sa02m-web-auth-lib.sh:113       -> "$f" = /etc/sa02m_web.env
    etc/sa02m-hw-backend-guard.sh:62    -> /etc/sa02m_hw.conf
    etc/sa02m-status-blocks-guard.sh:112,180 -> /etc/sa02m_status_blocks.conf
    etc/sa02m-prepare-working-board.sh:58 -> "$file" = /etc/sa02m_{hw,status_blocks,storage}.conf
    etc/sa02m-armbian-branding.sh:40,71 -> /etc/armbian{,-image}-release,
        /etc/update-motd.d/10-armbian-header
    etc/sa02m-update-runner.sh:122      -> "$STATEDIR/state/*"      (own state)
    etc/sa02m-update-runner.sh:399      -> "$STATEDIR/runner/$txn/runner", a
        per-transaction scratch self-copy that is exec'd immediately - not a
        live path (this entry used to be recorded as ":394" and as live; both
        were wrong)
    etc/sa02m-update-runner.sh:1018,1019,1021 and
    etc/sa02m-factory-reset-runner.sh:337 -> "$tmp", the staging file INSIDE
        atomic_install_file - these are the atomic shape, not violations of it
    etc/sa02m-check-service-perms.sh:63 -> `install -d`: a directory, not a
        file write (the helper refuses -d for the same reason)

FILES is deliberately NOT widened to etc/. Two independent reasons, either one
sufficient: the rewrite mode would emit `sa02m_atomic_install`, a name no etc/
script can resolve; and the seven converted sites in
etc/sa02m-web-update-apply.sh write "$tgt", a shell variable, which
destination() cannot classify - sweeping that file here would be a check that
passes because it tested nothing. Those sites are pinned behaviourally instead,
by scripts/dev/test-install-atomic.sh sections 8-9 (an allow-list of the two
sanctioned raw destinations + a converted-call floor + a drive-to-failure).
The two survivors above are tracked in .ai-dev/backlog.md.

Line endings are preserved byte-for-byte (the tree is LF; a CRLF checkout
stays CRLF). Run from the repo root: python3 scripts/dev/codemod-install-atomic.py
Harness: scripts/dev/test-install-atomic.sh.
"""
import glob
import os
import re
import sys

# Separators are normalised to `/` so a site is named identically on every
# platform: `glob` hands back `scripts\01-system.sh` on Windows, and the
# harness's drive-to-failure cases compare the printed `file:line` against the
# path they reverted. A backslash there made case 7c report a hollow sweep on a
# Windows checkout while CI stayed green — the environment-split shape
# .ai-dev/notes/quality-gate-environment.md exists for.
FILES = sorted(p.replace("\\", "/") for p in glob.glob("scripts/*.sh")) + ["install.sh"]
LIVE_PREFIXES = (
    "/etc/systemd/system/",
    "/usr/local/bin/",
    "/usr/local/sbin/",
    "/usr/local/lib/",
    "/usr/local/libexec/",
)
# `install` as a command word: line start or after a connector; never
# `/usr/bin/install` nor `sa02m_atomic_install` (the \w and / look-behind).
RAW_RE = re.compile(r"(?<![\w/.-])install\s+-m\s+")
CONVERTED_RE = re.compile(r"(?<![\w/.-])sa02m_atomic_install\s+-m\s+")
# The command ends at an unquoted control/redirect token.
END_TOKENS = ("&&", "||", ";", "|", ">", "2>", "<")


def shell_words(text):
    """Split on whitespace outside double quotes (enough for these lines)."""
    words, cur, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            cur += ch
        elif ch.isspace() and not quoted:
            if cur:
                words.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        words.append(cur)
    return words


def destination(logical_after_install):
    """Last argument of the install command (quotes stripped), or None."""
    args = []
    for w in shell_words(logical_after_install):
        if w in END_TOKENS or w.startswith((">", "2>", "<")):
            break
        args.append(w)
    if len(args) < 2:
        return None
    return args[-1].strip('"')


def logical_line(lines, i):
    """Join physical line i with its backslash continuations."""
    text = lines[i].rstrip("\r\n")
    j = i
    while text.endswith("\\") and j + 1 < len(lines):
        j += 1
        text = text[:-1] + " " + lines[j].strip("\r\n").strip()
    return text


def scan(path):
    """Yield (lineno, kind, physical_line) for every in-scope site."""
    with open(path, newline="", encoding="utf-8") as f:
        lines = f.readlines()
    for i, raw in enumerate(lines):
        if raw.lstrip().startswith("#"):
            continue
        m_raw = RAW_RE.search(raw)
        m_conv = CONVERTED_RE.search(raw)
        if not (m_raw or m_conv):
            continue
        text = logical_line(lines, i)
        m = (RAW_RE if m_raw else CONVERTED_RE).search(text)
        dst = destination(text[m.end():])
        if dst is None or not dst.startswith(LIVE_PREFIXES):
            continue
        yield i + 1, ("raw" if m_raw else "converted"), raw


def rewrite(path):
    with open(path, newline="", encoding="utf-8") as f:
        lines = f.readlines()
    changed = []
    for lineno, kind, _ in list(scan(path)):
        if kind != "raw":
            continue
        idx = lineno - 1
        lines[idx] = RAW_RE.sub("sa02m_atomic_install -m ", lines[idx], count=1)
        changed.append(lineno)
    if changed:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.writelines(lines)
    return changed


def main(argv):
    mode = "rewrite"
    show_all = False
    for a in argv:
        if a == "--list":
            mode = "list"
        elif a == "--check":
            mode = "check"
        elif a == "--all":
            show_all = True
        else:
            sys.stderr.write("usage: codemod-install-atomic.py [--list [--all] | --check]\n")
            return 2
    # Non-vacuity: a file named in FILES that is not on disk means the sweep is
    # running somewhere other than the repo root - it must FAIL, never silently
    # check a smaller set (quality-gate-rigor.md: a missing target file FAILS).
    missing = [p for p in FILES if not os.path.isfile(p)]
    if len(FILES) < 2 or missing:
        sys.stderr.write("codemod-install-atomic: incomplete sweep set"
                         " (missing: %s) - run from the repo root\n"
                         % (", ".join(missing) or "scripts/*.sh"))
        return 2
    remaining = 0
    for path in FILES:
        if mode == "rewrite":
            for lineno in rewrite(path):
                print("%s:%d: rewritten" % (path, lineno))
            continue
        for lineno, kind, raw in scan(path):
            if kind == "raw":
                remaining += 1
                print("%s:%d: %s" % (path, lineno, raw.rstrip("\r\n").strip()))
            elif show_all:
                print("%s:%d: [converted] %s" % (path, lineno, raw.rstrip("\r\n").strip()))
    if mode == "check":
        if remaining:
            print("codemod-install-atomic: %d live-path install -m site(s) still raw" % remaining)
            return 1
        print("codemod-install-atomic: ok — no raw live-path install -m site")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
