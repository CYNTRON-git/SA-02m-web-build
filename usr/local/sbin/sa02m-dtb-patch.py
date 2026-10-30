#!/usr/bin/env python3
r"""Patch a decompiled SA-02m boot DTB (dts text) so every PHY binds its own driver.

What it patches (text-level, node-targeted; everything else in the blob is kept
byte-for-byte, so blobs that differ elsewhere — the rtc/usb0_vbus fdtput edits on
some boards — each keep their own differences):

  /soc/mdio@1c0b080/ethernet-phy@0   (eth0, EMAC PHY — IC+ IP101A on BOTH HW variants)
      compatible = "ethernet-phy-id0243.0c54", <the node's other strings>;
      reset-deassert-us = <0x7a120>;                       (500 ms)
  /soc/ethernet@1c50000              (eth1, GMAC — SA-02m-2 only, the node exists everywhere)
      status = "okay"; syscon = <0x02>;
      mdio/ethernet-phy@0: compatible = "icplus,ip101a", ...; reset-deassert-us = <0x7a120>;
  /soc/i2c@1c2b800                   status = "okay"   (pre-start + PCA9536)
  dc1sw                              regulator-always-on   (vcc-gmac-phy)

Why the ID string and not a vendor string on the EMAC node: Linux phylib picks a
PHY driver by the PHY ID read over MDIO, never by a vendor `compatible` (icplus.c
has no OF match table). The eth0 PHY's ID is read while its reset pin is still
asserted, so it answers 0x00000000, nothing matches and the kernel binds the
"Generic PHY" fallback. The ONE DT mechanism that overrides a failed ID read is
the FIRST compatible string in the form `ethernet-phy-idAAAA.BBBB`: fwnode_get_phy_id()
parses it and the core creates the device with that ID without reading anything;
the icplus driver's own match_phy_device() then probes a register at device_add(),
after the reset deassert + reset-deassert-us sleep, when the PHY is awake. The GMAC
PHY keeps `icplus,ip101a` as today (its ID read succeeds; forcing an ID there would
fabricate a PHY on a board whose GMAC PHY is not populated).

Text forms of a string list. dtc >= 1.7 (the board's 1.7.0, CI's) decompiles a
multi-string property as ONE literal with `\0` escapes —
`compatible = "icplus,ip101a\0ethernet-phy-ieee802.3-c22";` — while older dtc wrote
`"icplus,ip101a", "ethernet-phy-ieee802.3-c22"`; both compile to the same bytes.
Every rule here runs on the CANONICAL list (quoted literals split on `\0`), and
a node whose list is already right is returned byte-for-byte: a form is never
rewritten for its own sake, so the on-board cycle (dtc decompile -> patch -> dtc
compile -> decompile again) is a fixed point and the wrapper's `cmp` reads
«already patched». A rewrite emits the `"a", "b"` form. A PHY node whose list
lacks `ethernet-phy-ieee802.3-c22` gets it appended — the self-heal for a blob
damaged by the earlier quoted-literal split (found on bench 1.135 before the
first merge of this change; the record is docs/bugs/BUGLOG.md 2026-10-30).

On-device caller: /usr/local/sbin/sa02m-dtb-patch.sh (dtc decompile -> this ->
dtc compile -> fdtget verify -> atomic FAT write). Tests: scripts/dev/test-dtb-patch.py
(quality row dtb-patch-unit); fixtures scripts/dev/fixtures/dtb-patch/ — the hand-written
sun8i-a40i-sk.pre.dts, and the bench 1.135 decompiles trimmed to these nodes:
sun8i-a40i-sk.real.dts (the original blob, dtc 1.7 form) and sun8i-a40i-sk.damaged.dts
(after three applies of the pre-fix patcher).

CLI: sa02m-dtb-patch.py [--in PATH] [--out PATH]   (both default /tmp/patch.dts — the
legacy call shape). Prints one line per change, or `unchanged`. Exit 0 = ok,
1 = a required node is missing (not an SA-02m DTB), 2 = usage / IO error.
Idempotent: patching the output again — in either text form — changes nothing.
Stdlib only, Python 3.10+.
"""
import argparse
import re
import sys

EMAC_PHY_ID_COMPAT = "ethernet-phy-id0243.0c54"   # IC+ IP101A rev 4 (mii-tool -v / BUGLOG 2026-10-28)
GMAC_PHY_COMPAT = "icplus,ip101a"
PHY_FALLBACK_COMPAT = "ethernet-phy-ieee802.3-c22"  # the generic PHY binding; appended only when a PHY node's list lacks it
RESET_DEASSERT = "<0x7a120>"                      # 500000 us, dtc's lowercase hex idiom
LEGACY_PATH = "/tmp/patch.dts"
_ID_PREFIX = "ethernet-phy-id"
_DTS_NUL = "\\0"                                  # dtc's escape between the strings of ONE literal

_COMPAT_RE = re.compile(r'compatible = ((?:"[^"]*"(?:,\s*)?)+);')
_DEASSERT_RE = re.compile(r"reset-deassert-us = <(0x[0-9a-fA-F]+|\d+)>;")
_STATUS_DISABLED_RE = re.compile(r'status = "disabled";')
_SYSCON_RE = re.compile(r"syscon = <(0x[0-9a-fA-F]+|\d+)>;")
_LABEL_RE = re.compile(r"^(\w[\w.-]*:\s*)?$")


class NodeMissing(SystemExit):
    """A node the patch exists for is absent: fail closed, exit 1, name on stderr."""

    def __init__(self, name):
        print(f"sa02m-dtb-patch: node not found: {name}", file=sys.stderr)
        super().__init__(1)


def find_node(text, header, start=0):
    """Span (begin, end) of the node opened by `header` (e.g. 'mdio@1c0b080 {'),
    searched from `start`; `end` is just past the closing '};'. None when absent.
    Brace-aware: a node holding children ends at ITS closing brace, not at the
    first nested '};' (the defect class a plain text.find('};') hits). The header
    must open a line (an optional dts label before it is tolerated), so
    'xmdio@1c0b080 {' never matches 'mdio@1c0b080 {'."""
    i = text.find(header, start)
    while i >= 0:
        line_start = text.rfind("\n", 0, i) + 1
        if _LABEL_RE.match(text[line_start:i].strip()):
            break
        i = text.find(header, i + 1)
    if i < 0:
        return None
    depth = 0
    for k in range(i + len(header) - 1, len(text)):
        c = text[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                if end < len(text) and text[end] == ";":
                    end += 1
                return (i, end)
    return None


def _own_spans(node_text):
    """Character spans of the node's OWN body (depth 1), children cut out."""
    spans = []
    depth = 0
    cur = None
    for k, c in enumerate(node_text):
        if c == "{":
            depth += 1
            if depth == 1:
                cur = k + 1
            elif depth == 2:
                spans.append((cur, k))
        elif c == "}":
            depth -= 1
            if depth == 1:
                cur = k + 1
            elif depth == 0:
                spans.append((cur, k))
                break
    return spans


def _own_prop(node_text, regex):
    """First match of `regex` among the node's OWN properties (never a child's)."""
    spans = _own_spans(node_text)
    for m in regex.finditer(node_text):
        if any(a <= m.start() < b for a, b in spans):
            return m
    return None


def _sub_own(node_text, regex, repl):
    """Rewrite the node's own first match of `regex`; unchanged text when absent."""
    m = _own_prop(node_text, regex)
    if m is None:
        return node_text
    return node_text[: m.start()] + repl(m) + node_text[m.end():]


def _compat_strings(value):
    """Canonical string list of a `compatible` value, whatever text form dtc
    chose: every quoted literal, each split on the dts `\\0` escape. dtc >= 1.7
    (the board's 1.7.0, CI's) decompiles a multi-string property as ONE literal,
    `"a\\0b"`; older dtc wrote `"a", "b"`; both compile to the same bytes. The
    split is not escape-aware: an escaped backslash followed by a zero (`\\\\0`
    in dts) is split too, and a rewrite would then emit invalid dts. A
    conformant `compatible` cannot contain a backslash at all, and the case
    fails closed — dtc refuses the text, the wrapper exits 5, the FAT is
    untouched. Other escapes (`\\x41`, `\\\\`) pass through unchanged."""
    out = []
    for lit in re.findall(r'"([^"]*)"', value):
        out.extend(lit.split(_DTS_NUL))
    return out


def compatible_strings(node_text):
    """The node's OWN `compatible` as a canonical list; None when it has none."""
    m = _own_prop(node_text, _COMPAT_RE)
    return None if m is None else _compat_strings(m.group(1))


def set_compatible_first(node_text, first, ensure=()):
    """Rewrite the node's own `compatible` so `first` leads. Existing strings
    follow in order; a duplicate of `first` is dropped, and when `first` is an
    `ethernet-phy-id…` override any OTHER id-form entry is dropped too (two IDs
    would be a lie; only the first one is read). Every string of `ensure` that
    the list lacks is appended (the PHY fallback a damaged list lost). The
    rules run on the CANONICAL list (`_compat_strings`), never on the text
    form: the text is returned UNCHANGED when the list is already right — a
    `"a\\0b"` literal is never re-formed just to look different, so a dtc cycle
    (compile, decompile, patch) is a fixed point and the caller's byte
    comparison means «already patched». A rewrite emits the `"a", "b"` form."""
    m = _own_prop(node_text, _COMPAT_RE)
    if m is None:
        return node_text
    strings = _compat_strings(m.group(1))
    rest = [
        s for s in strings
        if s != first and not (first.startswith(_ID_PREFIX) and s.startswith(_ID_PREFIX))
    ]
    new = [first] + rest
    for s in ensure:
        if s not in new:
            new.append(s)
    if new == strings:
        return node_text
    line = "compatible = " + ", ".join(f'"{s}"' for s in new) + ";"
    return node_text[: m.start()] + line + node_text[m.end():]


def set_deassert(node_text):
    return _sub_own(node_text, _DEASSERT_RE, lambda m: f"reset-deassert-us = {RESET_DEASSERT};")


def _replace_span(text, span, new_node):
    return text[: span[0]] + new_node + text[span[1]:]


def _patch_phy(phy_text, first, label, changes):
    """One PHY node: `first` leads compatible, the c22 fallback present, deassert
    pinned; changes named by `label`."""
    before = phy_text
    was = compatible_strings(phy_text) or []
    phy_text = set_compatible_first(phy_text, first, ensure=(PHY_FALLBACK_COMPAT,))
    if phy_text != before:
        if was[:1] == [first] and PHY_FALLBACK_COMPAT not in was:
            changes.append(f"{label}: compatible {PHY_FALLBACK_COMPAT} appended")
        else:
            changes.append(f"{label}: compatible {first} first")
    before = phy_text
    phy_text = set_deassert(phy_text)
    if phy_text != before:
        changes.append(f"{label}: reset-deassert-us 500000")
    return phy_text


def patch_dc1sw(text, changes):
    span = find_node(text, "dc1sw {")
    if span is None:
        return text
    node = text[span[0]:span[1]]
    if "regulator-always-on" in node:
        return text
    m = _own_prop(node, re.compile(r'([ \t]*)regulator-name = "vcc-gmac-phy";'))
    if m is None:
        return text
    indent = m.group(1)
    node = node[: m.start()] + indent + "regulator-always-on;\n" + m.group(0) + node[m.end():]
    changes.append("dc1sw: regulator-always-on")
    return _replace_span(text, span, node)


def patch_i2c(text, changes):
    span = find_node(text, "i2c@1c2b800 {")
    if span is None:
        return text
    node = text[span[0]:span[1]]
    new = _sub_own(node, _STATUS_DISABLED_RE, lambda m: 'status = "okay";')
    if new != node:
        changes.append("i2c@1c2b800: status okay")
        text = _replace_span(text, span, new)
    return text


def patch_gmac(text, changes):
    span = find_node(text, "ethernet@1c50000 {")
    if span is None:
        raise NodeMissing("/soc/ethernet@1c50000 (GMAC)")
    node = text[span[0]:span[1]]
    # status: every `disabled` inside the node, children included (the legacy
    # behaviour — the GMAC subtree carries no node that must stay disabled).
    new = _STATUS_DISABLED_RE.sub('status = "okay";', node)
    if new != node:
        changes.append("ethernet@1c50000: status okay")
        node = new
    new = _sub_own(node, _SYSCON_RE, lambda m: "syscon = <0x02>;")
    if new != node:
        changes.append("ethernet@1c50000: syscon 0x02")
        node = new
    mdio = find_node(node, "mdio {")
    if mdio is not None and 'compatible = "snps,dwmac-mdio"' in node[mdio[0]:mdio[1]]:
        mdio_text = node[mdio[0]:mdio[1]]
        phy = find_node(mdio_text, "ethernet-phy@0 {")
        if phy is not None:
            phy_text = mdio_text[phy[0]:phy[1]]
            phy_text = _patch_phy(phy_text, GMAC_PHY_COMPAT, "ethernet@1c50000/mdio/ethernet-phy@0", changes)
            mdio_text = _replace_span(mdio_text, phy, phy_text)
            node = _replace_span(node, mdio, mdio_text)
    return _replace_span(text, span, node)


def patch_emac_phy(text, changes):
    """The fix this tool exists for: eth0's PHY node gets the ID override first."""
    mdio = find_node(text, "mdio@1c0b080 {")
    if mdio is None:
        raise NodeMissing("/soc/mdio@1c0b080 (EMAC mdio)")
    mdio_text = text[mdio[0]:mdio[1]]
    phy = find_node(mdio_text, "ethernet-phy@0 {")
    if phy is None:
        raise NodeMissing("/soc/mdio@1c0b080/ethernet-phy@0 (EMAC PHY)")
    phy_text = mdio_text[phy[0]:phy[1]]
    phy_text = _patch_phy(phy_text, EMAC_PHY_ID_COMPAT, "mdio@1c0b080/ethernet-phy@0", changes)
    mdio_text = _replace_span(mdio_text, phy, phy_text)
    return _replace_span(text, mdio, mdio_text)


def patch_all(text):
    """Apply every patch; returns (text, changes). Raises NodeMissing (exit 1)
    when the GMAC or the EMAC PHY node is absent."""
    changes = []
    text = patch_dc1sw(text, changes)
    text = patch_i2c(text, changes)
    text = patch_gmac(text, changes)
    text = patch_emac_phy(text, changes)
    return text, changes


def main(argv=None):
    ap = argparse.ArgumentParser(description="patch a decompiled SA-02m DTB (dts text)")
    ap.add_argument("--in", dest="src", default=LEGACY_PATH, help="input dts (default %(default)s)")
    ap.add_argument("--out", dest="dst", default=LEGACY_PATH, help="output dts (default %(default)s)")
    args = ap.parse_args(argv)
    try:
        with open(args.src, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        print(f"sa02m-dtb-patch: cannot read {args.src}: {e}", file=sys.stderr)
        return 2
    new, changes = patch_all(text)
    try:
        with open(args.dst, "w", encoding="utf-8", newline="") as f:
            f.write(new)
    except OSError as e:
        print(f"sa02m-dtb-patch: cannot write {args.dst}: {e}", file=sys.stderr)
        return 2
    if changes:
        for c in changes:
            print(c)
    else:
        print("unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
