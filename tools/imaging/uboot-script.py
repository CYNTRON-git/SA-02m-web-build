#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════
# uboot-script.py — the ONE home for the U-Boot legacy script-image (boot.scr)
# byte format used by SA-02m. Both producers and the regression test use it,
# so writer and checker can never drift apart:
#
#   tools/imaging/patch-firstboot-image.sh   (build when mkimage is absent + validate)
#   tools/debian-rootfs/pack-sa02m-image.sh  (normalize + validate mkimage output)
#   scripts/dev/test-uboot-bootscr.sh        (quality row uboot-bootscr-format)
#
# WHY THIS FILE EXISTS — two dead boards, same broken promise. A generator wrote
# `header + script` for ih_type=6 (IH_TYPE_SCRIPT). U-Boot's `source` reads the
# data section of a script image as a MULTI-FILE image: an 8-byte size table
#     [uint32 BE script_len][uint32 BE 0]
# followed by the script bytes. Without the table U-Boot reads the first four
# script bytes as the length and the board never reaches the kernel. Every check
# in the tree passed (the file did contain "threadirqs"), which is why the check
# is now byte-level.
#
# LAYOUT (single-component script image), anchored to the donor artifact a board
# is OBSERVED to boot — 261 bytes total:
#
#   [0..63]    64-byte legacy uImage header (include/image.h)
#   [64..71]    8-byte size table: script_len (BE), 0 (BE)
#   [72..EOF]   the script text, VERBATIM, NOT PADDED
#
#   ih_size = 8 + len(script)          -> 197 for the 189-byte proven script
#   ih_dcrc = crc32(table + script)    -> NOT crc32(script)
#   no trailing pad                    -> 64 + 8 + 189 = 261; a padding writer
#                                         would emit 264 and is rejected
#
# What is deliberately NOT asserted (ih_os, a specific ih_name) and why: see
# docs/contracts/uboot-boot-script.md §1 — the one home for the format rules.
#
# CLI (stdlib only, no deps):
#   uboot-script.py resolve   [--repo-root DIR] [--payload P]  -> payload path
#   uboot-script.py default   [--repo-root DIR]                -> default path
#   uboot-script.py normalize SRC DST                -> CRLF->LF copy of SRC
#   uboot-script.py build     SRC OUT [--name NAME]  -> reference writer
#   uboot-script.py validate  PATH                   -> exit 0 / loud exit 1
# ═══════════════════════════════════════════════════════════════════════════
"""U-Boot legacy script-image (boot.scr) build + validate for SA-02m."""

import argparse
import binascii
import os
import struct
import sys
import time

MAGIC = 0x27051956
HEADER_LEN = 64
TABLE_LEN = 8
HEADER_FMT = ">7I4B32s"

IH_OS_LINUX = 5
IH_ARCH_ARM = 2
IH_TYPE_SCRIPT = 6
IH_COMP_NONE = 0

# The payload a board is observed to boot. Repo-root-relative; the opt-in
# variant is selected with --bootcmd <path> (see resolve_payload below and the
# contract doc). SA02M_BOOTCMD is a convenience for DIRECT, non-sudo runs only:
# sudo's env_reset drops it, which is why the producers pass an argument.
DEFAULT_PAYLOAD = "etc/boot.cmd.sa02m.min"
PAYLOAD_ENV = "SA02M_BOOTCMD"

# A boot.scr whose script does not carry threadirqs lets the PCA9536 I2C IRQ
# storm wedge a working board before sa02m-pre-start (tools/imaging/README.md).
REQUIRED_TOKEN = b"threadirqs"

# Sanity floor for ih_time: 2020-09-13. Catches a writer that forgets the stamp.
MIN_PLAUSIBLE_TIME = 1600000000


class BootScrError(Exception):
    """A boot.scr that is not a valid U-Boot legacy script image."""


def default_payload(repo_root):
    """Return the hardware-verified default payload path."""
    return os.path.join(repo_root, *DEFAULT_PAYLOAD.split("/"))


def resolve_payload(repo_root, override=None):
    """Return the boot.cmd payload path.

    Precedence: the explicit --payload argument, then SA02M_BOOTCMD, then the
    default. The ARGUMENT is the mechanism that crosses a `sudo` boundary — a
    command-line value survives unconditionally, while an environment variable
    is stripped by sudo's env_reset and cannot be re-added portably (sudoers
    `setenv` is off by default, so `sudo VAR=value cmd` is REFUSED). The env
    var is a convenience for direct, non-sudo invocation only.
    """
    if override and override.strip():
        return override.strip()
    env_override = os.environ.get(PAYLOAD_ENV, "").strip()
    if env_override:
        return env_override
    return default_payload(repo_root)


def normalize(payload):
    """CRLF -> LF. A CR survives into `setenv bootargs ...` and reaches the
    kernel command line verbatim; it also shifts every byte offset below."""
    return payload.replace(b"\r\n", b"\n")


def build(payload, name="SA-02m", timestamp=None):
    """Return the complete boot.scr bytes for `payload` (already normalized).

    The timestamp is sampled ONCE: the header whose hcrc is computed and the
    header that is written must be the same bytes, or U-Boot fails
    image_check_hcrc with "Bad Header Checksum" whenever the two samples land
    in different seconds.
    """
    if not payload:
        raise BootScrError("empty boot.cmd payload")
    if b"\r" in payload:
        raise BootScrError("boot.cmd payload still contains CR after normalization")

    stamp = int(time.time()) if timestamp is None else int(timestamp)
    name_field = name.encode("ascii", "strict")
    if len(name_field) > 32:
        raise BootScrError("image name longer than 32 bytes: %r" % name)
    name_field = name_field + b"\0" * (32 - len(name_field))

    # Single-component script image: size table + script, no padding.
    data = struct.pack(">II", len(payload), 0) + payload
    dcrc = binascii.crc32(data) & 0xFFFFFFFF

    def header(hcrc):
        return struct.pack(
            HEADER_FMT,
            MAGIC, hcrc, stamp, len(data), 0, 0, dcrc,
            IH_OS_LINUX, IH_ARCH_ARM, IH_TYPE_SCRIPT, IH_COMP_NONE,
            name_field,
        )

    hcrc = binascii.crc32(header(0)) & 0xFFFFFFFF
    return header(hcrc) + data


def validate(blob):
    """Raise BootScrError naming the offending value, or return the script bytes.

    Applied to the output of BOTH producers — a future mkimage change or a wrong
    flag is caught exactly like the reference writer's own mistakes.
    """
    total = len(blob)
    # 1 — truncated write
    if total < HEADER_LEN + TABLE_LEN:
        raise BootScrError("file is %d bytes, minimum is %d (header+table)"
                           % (total, HEADER_LEN + TABLE_LEN))

    magic, hcrc, stamp, size, load, entry, dcrc, os_id, arch, itype, comp, name_field = \
        struct.unpack(HEADER_FMT, blob[:HEADER_LEN])

    # 2 — not a uImage at all
    if magic != MAGIC:
        raise BootScrError("bad magic 0x%08x, want 0x%08x" % (magic, MAGIC))

    # 3 — the header written must be the header hcrc was computed over
    zeroed = blob[:4] + b"\0\0\0\0" + blob[8:HEADER_LEN]
    want_hcrc = binascii.crc32(zeroed) & 0xFFFFFFFF
    if hcrc != want_hcrc:
        raise BootScrError("ih_hcrc 0x%08x != recomputed 0x%08x "
                           "(header mutated after hcrc, e.g. a re-sampled timestamp)"
                           % (hcrc, want_hcrc))

    # 4 — wrong mkimage flags
    if itype != IH_TYPE_SCRIPT:
        raise BootScrError("ih_type %d != %d (IH_TYPE_SCRIPT)" % (itype, IH_TYPE_SCRIPT))
    if arch != IH_ARCH_ARM:
        raise BootScrError("ih_arch %d != %d (ARM)" % (arch, IH_ARCH_ARM))
    if comp != IH_COMP_NONE:
        raise BootScrError("ih_comp %d != %d (none)" % (comp, IH_COMP_NONE))

    # 5 — short/long write
    if size != total - HEADER_LEN:
        raise BootScrError("ih_size %d != filesize-64 %d" % (size, total - HEADER_LEN))

    # 6 — THE regression: the 8-byte size table
    script_len, terminator = struct.unpack(">II", blob[HEADER_LEN:HEADER_LEN + TABLE_LEN])
    want_len = total - HEADER_LEN - TABLE_LEN
    if script_len != want_len:
        raise BootScrError(
            "size table @0x40 says script_len=%d (0x%08x), want %d — the data section "
            "is missing its 8-byte size table (U-Boot would read the first four script "
            "bytes as the length and never boot)" % (script_len, script_len, want_len))
    if terminator != 0:
        raise BootScrError("size-table terminator @0x44 is 0x%08x, want 0" % terminator)

    # 7 — dcrc must cover table+script, not the script alone
    want_dcrc = binascii.crc32(blob[HEADER_LEN:]) & 0xFFFFFFFF
    if dcrc != want_dcrc:
        raise BootScrError("ih_dcrc 0x%08x != crc32(table+script) 0x%08x "
                           "(computed over the script alone?)" % (dcrc, want_dcrc))

    script = blob[HEADER_LEN + TABLE_LEN:]

    # 8 — a CR reaches the kernel command line verbatim
    if b"\r" in script:
        raise BootScrError("script contains CR at offset %d (CRLF payload not normalized)"
                           % (script.index(b"\r") + HEADER_LEN + TABLE_LEN))

    # 9 — the semantic check the old grep did, kept
    if REQUIRED_TOKEN not in script:
        raise BootScrError("script has no %s (I2C/PCA9536 IRQ-storm protection)"
                           % REQUIRED_TOKEN.decode())

    # 10 — a writer that forgets the timestamp
    if stamp <= MIN_PLAUSIBLE_TIME:
        raise BootScrError("ih_time %d is not a plausible build timestamp" % stamp)

    # ih_name: only the NUL-padding shape, never a specific string (see header).
    body = name_field.split(b"\0", 1)[0]
    if name_field[len(body):].strip(b"\0"):
        raise BootScrError("ih_name is not NUL-padded: %r" % name_field)

    return script


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    p_res = sub.add_parser("resolve", help="print the boot.cmd payload path")
    p_res.add_argument("--repo-root", default=".")
    p_res.add_argument("--payload", default="",
                       help="explicit override (the sudo-safe mechanism); empty = unset")

    p_def = sub.add_parser("default", help="print the hardware-verified default payload path")
    p_def.add_argument("--repo-root", default=".")

    p_norm = sub.add_parser("normalize", help="write a CRLF->LF copy")
    p_norm.add_argument("src")
    p_norm.add_argument("dst")

    p_build = sub.add_parser("build", help="reference writer (mkimage-free)")
    p_build.add_argument("src")
    p_build.add_argument("out")
    p_build.add_argument("--name", default="SA-02m")

    p_val = sub.add_parser("validate", help="byte-level format check")
    p_val.add_argument("path")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_usage(sys.stderr)
        return 2

    try:
        if args.cmd == "resolve":
            sys.stdout.write(resolve_payload(args.repo_root, args.payload) + "\n")
            return 0

        if args.cmd == "default":
            sys.stdout.write(default_payload(args.repo_root) + "\n")
            return 0

        if args.cmd == "normalize":
            payload = normalize(_read(args.src))
            if not payload.strip():
                raise BootScrError("boot.cmd payload %s is empty" % args.src)
            with open(args.dst, "wb") as fh:
                fh.write(payload)
            return 0

        if args.cmd == "build":
            blob = build(normalize(_read(args.src)), name=args.name)
            validate(blob)  # never write an artifact we would reject
            with open(args.out, "wb") as fh:
                fh.write(blob)
            return 0

        if args.cmd == "validate":
            script = validate(_read(args.path))
            sys.stdout.write(
                "boot.scr OK: %d bytes = %d header + %d table + %d script\n"
                % (HEADER_LEN + TABLE_LEN + len(script), HEADER_LEN, TABLE_LEN, len(script)))
            return 0
    except BootScrError as exc:
        sys.stderr.write("FATAL: %s: %s\n" % (getattr(args, "path", None) or
                                              getattr(args, "src", "boot.scr"), exc))
        return 1
    except OSError as exc:
        sys.stderr.write("FATAL: %s\n" % exc)
        return 1

    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
