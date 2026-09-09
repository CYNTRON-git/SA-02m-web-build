#!/usr/bin/env python3
"""Codemod: live-path `install -m` sites -> `sa02m_atomic_install -m` (scripts/lib.sh).

Why a script (web-workflow.md, Rule of 500): the move touches dozens of sites
across scripts/*.sh, so it must be re-runnable and reviewable as ONE rule, not
as hand edits. The rule: an `install -m ...` command whose DESTINATION is a
live path the running system reads without a restart — a systemd unit or
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

Line endings are preserved byte-for-byte (the tree is LF; a CRLF checkout
stays CRLF). Run from the repo root: python3 scripts/dev/codemod-install-atomic.py
Harness: scripts/dev/test-install-atomic.sh.
"""
import glob
import re
import sys

FILES = sorted(glob.glob("scripts/*.sh"))
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
    if not FILES:
        sys.stderr.write("codemod-install-atomic: no scripts/*.sh found — run from the repo root\n")
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
