#!/usr/bin/env python3
r"""test-dtb-patch.py — unit regression for usr/local/sbin/sa02m-dtb-patch.py
(quality row dtb-patch-unit; stdlib unittest, run as a script from the repo root).

Why it exists: the eth0 (EMAC) PHY of every SA-02m boots under the kernel's
"Generic PHY" because its ID is read while the PHY is still in reset
(docs/bugs/BUGLOG.md 2026-10-28). The patcher's job is to put the ID override
`ethernet-phy-id0243.0c54` FIRST in /soc/mdio@1c0b080/ethernet-phy@0's
`compatible` — a text edit on a decompiled blob that no syntax gate can judge,
so its behaviour is pinned here on a hand-written fixture in dtc's decompiled
idiom (scripts/dev/fixtures/dtb-patch/sun8i-a40i-sk.pre.dts: tabs, lowercase
hex, numeric phandles, a decoy `ethernet-phy@0` outside the EMAC mdio so a
non-brace-aware search would pick the wrong node) AND on two fixtures cut from
the bench 1.135 decompiles (dtc 1.7.0, 2026-10-29): sun8i-a40i-sk.real.dts
(the original blob — every multi-string property is ONE literal with `\0`
escapes, `"icplus,ip101a\0ethernet-phy-ieee802.3-c22"`, the form the hand-written
fixture never had) and sun8i-a40i-sk.damaged.dts (the blob after three applies
of the pre-fix patcher: GMAC `icplus,ip101a` x4, EMAC id only — c22 lost).

Cases:
  U1 EMAC compatible rewritten id-first, ieee fallback second, deassert <0x7a120>;
     the decoy node is untouched.
  U2 GMAC still patched as the legacy patch_dtb_all.py did (status okay,
     syscon <0x02>, phy compatible icplus,ip101a first, deassert <0x7a120>),
     dc1sw regulator-always-on, i2c@1c2b800 okay.
  U3 idempotent on the hand-written comma-form fixture only (`"a", "b"`, older
     dtc): a second patch_all returns identical bytes and no changes. The dtc 1.7
     `\0` form is U9 (and U8 in CI) — U3 passed on the pre-fix patcher, as T2 did.
  U4 EMAC mdio missing, EMAC phy missing, GMAC missing -> SystemExit code 1.
  U5 an already-id-first blob with a stray icplus,ip101a on the EMAC node:
     id stays first, no duplicate, `unchanged`; a stale ethernet-phy-id0000.0000
     is replaced, not accumulated.
  U6 uppercase hex / decimal reset-deassert-us both normalise to <0x7a120>.
  U7 CLI --in/--out round trip through a subprocess: exit 0, stdout lists the
     changes, the output file equals the in-process result.
  U8 real blob — ONLY when `dtc` and `fdtget` are on PATH (unittest.skipUnless;
     the native skip prints `OK (skipped=1)` and the runner reports the row
     PARTIAL — Windows dev box; real on Linux CI, which installs
     device-tree-compiler): decompile tools/imaging/boot/sun8i-a40i-sk.dtb
     (byte-identical to the bench's original blob; the decompile must carry the
     one-literal `\0` form), patch, compile, `fdtget -ts … compatible` first
     token is the id string, a second compile of the same dts is byte-identical,
     size within 8 KiB of the original; then the on-board cycle again on the
     COMPILED result — decompile, patch: text identical, CLI `unchanged`, both
     PHY lists in canon through fdtget; then the damaged fixture compiled by
     real dtc, decompiled, patched: both lists in canon, stable on a further
     cycle. Anchors structure (reset-gpios present), not the pre-fix
     compatible, so a refreshed (already-patched) blob keeps the test green.
  U9 the real dtc 1.7 form (sun8i-a40i-sk.real.dts): run 1 changes the EMAC
     line only (the GMAC `\0` literal is kept byte-exact, never re-formed),
     runs 2 and 3 return identical text and no changes, c22 present in both PHY
     nodes after every run; the bench's apply-#1 EMAC state
     (`"ethernet-phy-id0243.0c54\0ethernet-phy-ieee802.3-c22"`) is left alone;
     the CLI prints the one change, then `unchanged`.
  U10 the damaged apply-#3 state (sun8i-a40i-sk.damaged.dts): one run repairs
     GMAC to one `icplus,ip101a` + c22 and restores c22 on the EMAC node
     (`… compatible ethernet-phy-ieee802.3-c22 appended`), the result equals the
     real fixture's patched text and a further run is a no-op.

Non-vacuous: a missing patcher or fixture is a hard error (the loader raises),
never a skip; every case asserts on the patched text, not on the exit code
alone.

RED/GREEN for U9/U10 (2026-10-30, the pre-fix patcher — the defect was found on
bench 1.135 before the first merge of this change; BUGLOG 2026-10-30), as
observed: U9 FAIL at its first assertion — run 1 reported the GMAC
change too (`ethernet@1c50000/mdio/ethernet-phy@0: compatible icplus,ip101a
first`) and wrote `"icplus,ip101a", "icplus,ip101a\0ethernet-phy-ieee802.3-c22"`
(the `\0` literal was not `icplus,ip101a`); the id\0c22 sub-case is the step the
tool harness T13 observed on the same patcher (apply #2 rewrote the EMAC node and
lost c22). U10 FAIL — the change list lacked `… compatible
ethernet-phy-ieee802.3-c22 appended`; through the CLI the x4 list grew to x5 and
the EMAC node stayed id-only. GREEN after the canonical-list rewrite of
set_compatible_first().

RED/GREEN (docs/agent-rules/quality-gate-rigor.md): the defect was observed on
the OLD tools/imaging/out/patch_dtb_all.py (fixed path /tmp/patch.dts):
  cp scripts/dev/fixtures/dtb-patch/sun8i-a40i-sk.pre.dts /tmp/patch.dts \
    && python3 <old patch_dtb_all.py> && grep -c ethernet-phy-id0243 /tmp/patch.dts
  -> `dtb patched ok`, count 0 (the EMAC node untouched; on Windows the same
  with /c/tmp, where python resolves /tmp/patch.dts). GREEN: this suite on
  the new module. DTB_PATCHER=<path> re-points the suite at another copy (the
  drive-to-failure recipe: comment the `_patch_phy(phy_text, EMAC_PHY_ID_COMPAT,
  …)` line out of a copy -> U1, U5, U6 FAIL on their assertions; registered in
  comment-mutation-proof).

Run: python3 scripts/dev/test-dtb-patch.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PATCHER = os.environ.get("DTB_PATCHER") or os.path.join(ROOT, "usr", "local", "sbin", "sa02m-dtb-patch.py")
FIXTURE = os.path.join(HERE, "fixtures", "dtb-patch", "sun8i-a40i-sk.pre.dts")
REAL_FIXTURE = os.path.join(HERE, "fixtures", "dtb-patch", "sun8i-a40i-sk.real.dts")
DAMAGED_FIXTURE = os.path.join(HERE, "fixtures", "dtb-patch", "sun8i-a40i-sk.damaged.dts")
REAL_DTB = os.path.join(ROOT, "tools", "imaging", "boot", "sun8i-a40i-sk.dtb")
EMAC_ID = "ethernet-phy-id0243.0c54"
IEEE = "ethernet-phy-ieee802.3-c22"
GMAC_VENDOR = "icplus,ip101a"
DTS_NUL = "\\0"   # the dts escape dtc >= 1.7 writes between the strings of ONE literal
EMAC_C22_LINE = '\t\t\t\tcompatible = "ethernet-phy-ieee802.3-c22";'   # the EMAC PHY line of the real fixture


def load_patcher():
    if not os.path.isfile(PATCHER):
        raise FileNotFoundError(f"patcher missing: {PATCHER}")
    spec = importlib.util.spec_from_file_location("sa02m_dtb_patch", PATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def node_text(mod, text, *headers):
    """Descend through `headers` (brace-aware) and return the innermost node text."""
    cur = text
    for h in headers:
        span = mod.find_node(cur, h)
        if span is None:
            raise AssertionError(f"node {h!r} not found")
        cur = cur[span[0]:span[1]]
    return cur


def own_compatible(mod, node):
    m = mod._own_prop(node, mod._COMPAT_RE)
    if m is None:
        raise AssertionError("no own compatible")
    import re
    # canonical list: dtc >= 1.7 writes `"a\0b"`, older dtc `"a", "b"` — same bytes
    out = []
    for lit in re.findall(r'"([^"]*)"', m.group(1)):
        out.extend(lit.split(DTS_NUL))
    return out


def own_deassert(mod, node):
    m = mod._own_prop(node, mod._DEASSERT_RE)
    return m.group(0) if m else None


class DtbPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_patcher()
        for fx in (FIXTURE, REAL_FIXTURE, DAMAGED_FIXTURE):
            if not os.path.isfile(fx):
                raise FileNotFoundError(f"fixture missing: {fx}")
        cls.pre = read(FIXTURE)
        cls.post, cls.changes = cls.mod.patch_all(cls.pre)

    # U1
    def test_u1_emac_phy_id_first(self):
        phy = node_text(self.mod, self.post, "mdio@1c0b080 {", "ethernet-phy@0 {")
        self.assertEqual(own_compatible(self.mod, phy), [EMAC_ID, IEEE])
        self.assertEqual(own_deassert(self.mod, phy), "reset-deassert-us = <0x7a120>;")
        self.assertIn(f"mdio@1c0b080/ethernet-phy@0: compatible {EMAC_ID} first", self.changes)
        decoy = node_text(self.mod, self.post, "decoy {", "ethernet-phy@0 {")
        self.assertEqual(own_compatible(self.mod, decoy), [IEEE])
        self.assertEqual(own_deassert(self.mod, decoy), "reset-deassert-us = <0x3e8>;")
        self.assertEqual(self.post.count(EMAC_ID), 1)

    # U2
    def test_u2_gmac_legacy_patch_kept(self):
        gmac = node_text(self.mod, self.post, "ethernet@1c50000 {")
        self.assertNotIn('status = "disabled"', gmac)
        self.assertIn('status = "okay";', gmac)
        self.assertIn("syscon = <0x02>;", gmac)
        phy = node_text(self.mod, gmac, "mdio {", "ethernet-phy@0 {")
        self.assertEqual(own_compatible(self.mod, phy), ["icplus,ip101a", IEEE])
        self.assertEqual(own_deassert(self.mod, phy), "reset-deassert-us = <0x7a120>;")
        i2c = node_text(self.mod, self.post, "i2c@1c2b800 {")
        self.assertIn('status = "okay";', i2c)
        dc1sw = node_text(self.mod, self.post, "dc1sw {")
        self.assertIn("\t\t\tregulator-always-on;\n\t\t\tregulator-name", dc1sw)
        self.assertNotIn(EMAC_ID, gmac)

    # U3
    def test_u3_idempotent(self):
        again, changes = self.mod.patch_all(self.post)
        self.assertEqual(again, self.post)
        self.assertEqual(changes, [])

    # U4
    def test_u4_missing_nodes_exit_1(self):
        mod = self.mod
        no_mdio = self.pre.replace("mdio@1c0b080 {", "mdio@deadbeef {")
        with self.assertRaises(SystemExit) as cm:
            mod.patch_all(no_mdio)
        self.assertEqual(cm.exception.code, 1)
        mdio = node_text(mod, self.pre, "mdio@1c0b080 {")
        no_phy = self.pre.replace(mdio, mdio.replace("ethernet-phy@0 {", "ethernet-phy@7 {"))
        self.assertNotEqual(no_phy, self.pre)
        with self.assertRaises(SystemExit) as cm:
            mod.patch_all(no_phy)
        self.assertEqual(cm.exception.code, 1)
        no_gmac = self.pre.replace("ethernet@1c50000 {", "ethernet@1c50001 {")
        with self.assertRaises(SystemExit) as cm:
            mod.patch_all(no_gmac)
        self.assertEqual(cm.exception.code, 1)

    # U5
    def test_u5_id_first_with_stray_vendor_string(self):
        mod = self.mod
        stray = self.post.replace(
            f'compatible = "{EMAC_ID}", "{IEEE}";',
            f'compatible = "{EMAC_ID}", "icplus,ip101a", "{IEEE}";',
        )
        self.assertNotEqual(stray, self.post)
        out, changes = mod.patch_all(stray)
        self.assertEqual(out, stray)
        self.assertEqual(changes, [])
        phy = node_text(mod, out, "mdio@1c0b080 {", "ethernet-phy@0 {")
        self.assertEqual(own_compatible(mod, phy), [EMAC_ID, "icplus,ip101a", IEEE])
        stale = self.post.replace(
            f'compatible = "{EMAC_ID}", "{IEEE}";',
            f'compatible = "ethernet-phy-id0000.0000", "{IEEE}";',
        )
        out, changes = mod.patch_all(stale)
        phy = node_text(mod, out, "mdio@1c0b080 {", "ethernet-phy@0 {")
        self.assertEqual(own_compatible(mod, phy), [EMAC_ID, IEEE])
        self.assertEqual(len(changes), 1)
        vendor_only = self.post.replace(
            f'compatible = "{EMAC_ID}", "{IEEE}";',
            f'compatible = "icplus,ip101a", "{IEEE}";',
        )
        out, _ = mod.patch_all(vendor_only)
        phy = node_text(mod, out, "mdio@1c0b080 {", "ethernet-phy@0 {")
        self.assertEqual(own_compatible(mod, phy), [EMAC_ID, "icplus,ip101a", IEEE])

    # U6
    def test_u6_deassert_forms_normalised(self):
        mod = self.mod
        for form in ("<0x7A120>", "<500000>", "<0x186A0>", "<100000>"):
            src = self.pre.replace("reset-deassert-us = <0x186a0>;", f"reset-deassert-us = {form};")
            self.assertNotEqual(src, self.pre, form)
            out, _ = mod.patch_all(src)
            for headers in (("mdio@1c0b080 {", "ethernet-phy@0 {"),
                            ("ethernet@1c50000 {", "mdio {", "ethernet-phy@0 {")):
                phy = node_text(mod, out, *headers)
                self.assertEqual(own_deassert(mod, phy), "reset-deassert-us = <0x7a120>;", form)

    # U7
    def test_u7_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, "out.dts")
            r = subprocess.run(
                [sys.executable, PATCHER, "--in", FIXTURE, "--out", dst],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = r.stdout.splitlines()
            self.assertEqual(sorted(lines), sorted(self.changes))
            self.assertEqual(read(dst), self.post)
            r2 = subprocess.run(
                [sys.executable, PATCHER, "--in", dst, "--out", os.path.join(td, "again.dts")],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(r2.stdout.strip(), "unchanged")
            r3 = subprocess.run(
                [sys.executable, PATCHER, "--in", os.path.join(td, "absent.dts"), "--out", dst],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(r3.returncode, 2)

    def _phy_lists(self, text):
        """(EMAC list, GMAC list) — the canonical compatible lists of both PHY nodes."""
        mod = self.mod
        emac = node_text(mod, text, "mdio@1c0b080 {", "ethernet-phy@0 {")
        gmac = node_text(mod, text, "ethernet@1c50000 {", "mdio {", "ethernet-phy@0 {")
        return own_compatible(mod, emac), own_compatible(mod, gmac)

    # U9
    def test_u9_real_dtc_form_idempotent(self):
        """The bench 1.135 decompile (dtc 1.7.0): a string list is ONE literal with
        `\\0` escapes. Run 1 patches the EMAC node only; runs 2 and 3 change nothing;
        the untouched GMAC literal is kept byte-exact (never re-formed)."""
        mod = self.mod
        real = read(REAL_FIXTURE)
        self.assertIn(f'compatible = "{GMAC_VENDOR}{DTS_NUL}{IEEE}";', real, "fixture lost the dtc 1.7 form")
        self.assertEqual(real.count(EMAC_C22_LINE), 1)
        out1, changes1 = mod.patch_all(real)
        self.assertEqual(changes1, [f"mdio@1c0b080/ethernet-phy@0: compatible {EMAC_ID} first"])
        self.assertEqual(out1, real.replace(EMAC_C22_LINE, f'\t\t\t\tcompatible = "{EMAC_ID}", "{IEEE}";'))
        gmac_hdr = ("ethernet@1c50000 {", "mdio {", "ethernet-phy@0 {")
        self.assertEqual(node_text(mod, out1, *gmac_hdr), node_text(mod, real, *gmac_hdr))
        prev = out1
        for n in (2, 3):
            out, changes = mod.patch_all(prev)
            self.assertEqual(changes, [], f"run {n} reported changes")
            self.assertEqual(out, prev, f"run {n} rewrote the text")
            self.assertEqual(self._phy_lists(out), ([EMAC_ID, IEEE], [GMAC_VENDOR, IEEE]), f"run {n}")
            prev = out
        self.assertEqual(self._phy_lists(out1), ([EMAC_ID, IEEE], [GMAC_VENDOR, IEEE]))
        # The bench's apply-#1 EMAC state as dtc 1.7 hands it back: id + c22 in ONE
        # literal. The pre-fix patcher dropped that literal whole (it "starts with
        # ethernet-phy-id") and c22 was lost on apply #2.
        d1 = real.replace(EMAC_C22_LINE, f'\t\t\t\tcompatible = "{EMAC_ID}{DTS_NUL}{IEEE}";')
        self.assertNotEqual(d1, real)
        out, changes = mod.patch_all(d1)
        self.assertEqual(changes, [])
        self.assertEqual(out, d1)
        self.assertEqual(self._phy_lists(out), ([EMAC_ID, IEEE], [GMAC_VENDOR, IEEE]))
        # the same through the CLI: run 1 names the one change, run 2 prints `unchanged`
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "p1.dts")
            p2 = os.path.join(td, "p2.dts")
            r1 = subprocess.run([sys.executable, PATCHER, "--in", REAL_FIXTURE, "--out", p1],
                                capture_output=True, text=True, timeout=60)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(r1.stdout.splitlines(), changes1)
            self.assertEqual(read(p1), out1)
            r2 = subprocess.run([sys.executable, PATCHER, "--in", p1, "--out", p2],
                                capture_output=True, text=True, timeout=60)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(r2.stdout.strip(), "unchanged")
            self.assertEqual(read(p2), out1)

    # U10
    def test_u10_damaged_list_repaired(self):
        """The bench's apply-#3 state (three runs of the pre-fix patcher): GMAC
        `icplus,ip101a` x4 + c22 in one literal, EMAC id only. One run brings both
        nodes to canon — one vendor string + c22, id + c22 — and the result is the
        real fixture's patched text (comment header aside); a further run is a no-op."""
        mod = self.mod
        damaged = read(DAMAGED_FIXTURE)
        self.assertEqual(damaged.count(GMAC_VENDOR), 4)
        self.assertIn(f'compatible = "{EMAC_ID}";', damaged)
        self.assertEqual(damaged.count(IEEE), 1)
        out, changes = mod.patch_all(damaged)
        self.assertEqual(sorted(changes), sorted([
            f"ethernet@1c50000/mdio/ethernet-phy@0: compatible {GMAC_VENDOR} first",
            f"mdio@1c0b080/ethernet-phy@0: compatible {IEEE} appended",
        ]))
        self.assertEqual(self._phy_lists(out), ([EMAC_ID, IEEE], [GMAC_VENDOR, IEEE]))
        self.assertEqual(out.count(GMAC_VENDOR), 1)
        self.assertEqual(out.count(IEEE), 2)
        self.assertEqual(out.count(EMAC_ID), 1)
        again, changes2 = mod.patch_all(out)
        self.assertEqual(changes2, [])
        self.assertEqual(again, out)
        # Same tree as the real fixture's patched result, the text form aside: the
        # repaired GMAC line is re-emitted as `"a", "b"`, the real fixture's
        # untouched one stays `"a\0b"` — normalise the form, then the soc node
        # must be identical.
        real_out, _ = mod.patch_all(read(REAL_FIXTURE))
        self.assertEqual(node_text(mod, out, "soc {").replace(DTS_NUL, '", "'),
                         node_text(mod, real_out, "soc {").replace(DTS_NUL, '", "'))

    # U8
    @unittest.skipUnless(shutil.which("dtc") and shutil.which("fdtget"),
                         "dtc/fdtget not on PATH (apt install device-tree-compiler)")
    def test_u8_real_blob_round_trip(self):
        self.assertTrue(os.path.isfile(REAL_DTB), REAL_DTB)
        with tempfile.TemporaryDirectory() as td:
            dts = os.path.join(td, "orig.dts")
            pdts = os.path.join(td, "patched.dts")
            dtb1 = os.path.join(td, "patched1.dtb")
            dtb2 = os.path.join(td, "patched2.dtb")
            subprocess.run(["dtc", "-q", "-I", "dtb", "-O", "dts", "-o", dts, REAL_DTB], check=True, timeout=120)
            text = read(dts)
            emac = node_text(self.mod, text, "mdio@1c0b080 {", "ethernet-phy@0 {")
            self.assertIn("reset-gpios", emac)
            # The decompile must carry the one-literal form this suite exists for
            # (dtc >= 1.7; the bench blob is byte-identical to REAL_DTB) — a dtc that
            # writes `"a", "b"` again would leave the real-form case unexercised here.
            self.assertIn(f'"{GMAC_VENDOR}{DTS_NUL}{IEEE}"', text, "dtc did not write the one-literal form")
            out, _ = self.mod.patch_all(text)
            with open(pdts, "w", encoding="utf-8", newline="") as f:
                f.write(out)
            subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", dtb1, pdts], check=True, timeout=120)
            subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", dtb2, pdts], check=True, timeout=120)
            with open(dtb1, "rb") as a, open(dtb2, "rb") as b:
                self.assertEqual(a.read(), b.read())
            r = subprocess.run(
                ["fdtget", "-ts", dtb1, "/soc/mdio@1c0b080/ethernet-phy@0", "compatible"],
                capture_output=True, text=True, check=True, timeout=60,
            )
            self.assertEqual(r.stdout.split()[0], EMAC_ID)
            r = subprocess.run(
                ["fdtget", dtb1, "/soc/mdio@1c0b080/ethernet-phy@0", "reset-deassert-us"],
                capture_output=True, text=True, check=True, timeout=60,
            )
            self.assertEqual(r.stdout.strip(), "500000")
            delta = os.path.getsize(dtb1) - os.path.getsize(REAL_DTB)
            self.assertLessEqual(abs(delta), 8192)
            self.assertGreaterEqual(os.path.getsize(dtb1), 16384)
            # Run 2 on the COMPILED result, through real dtc again (the on-board
            # cycle): the decompile hands the patched list back as one literal and
            # the patcher must leave it alone — text identical, CLI `unchanged`.
            self._assert_blob_canon_and_stable(td, dtb1, "orig")
            # The bench's damaged apply-#3 state, compiled by real dtc: one run
            # brings both PHY nodes to canon and the result is stable.
            ddtb = os.path.join(td, "damaged.dtb")
            subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", ddtb, DAMAGED_FIXTURE], check=True, timeout=120)
            ddts = os.path.join(td, "damaged.dts")
            dpdts = os.path.join(td, "damaged.patched.dts")
            dpdtb = os.path.join(td, "damaged.patched.dtb")
            subprocess.run(["dtc", "-q", "-I", "dtb", "-O", "dts", "-o", ddts, ddtb], check=True, timeout=120)
            dtext = read(ddts)
            self.assertEqual(dtext.count(GMAC_VENDOR), 4, "dtc round trip lost the damaged list")
            dout, dchanges = self.mod.patch_all(dtext)
            self.assertEqual(len(dchanges), 2, dchanges)
            with open(dpdts, "w", encoding="utf-8", newline="") as f:
                f.write(dout)
            subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", dpdtb, dpdts], check=True, timeout=120)
            self._assert_blob_canon_and_stable(td, dpdtb, "damaged")

    def _assert_blob_canon_and_stable(self, td, dtb, tag):
        """fdtget reads both PHY lists in canon from `dtb`; decompiling it and
        patching again changes nothing (text identical, CLI prints `unchanged`)."""
        for path, want in (("/soc/mdio@1c0b080/ethernet-phy@0", f"{EMAC_ID} {IEEE}"),
                           ("/soc/ethernet@1c50000/mdio/ethernet-phy@0", f"{GMAC_VENDOR} {IEEE}")):
            r = subprocess.run(["fdtget", "-ts", dtb, path, "compatible"],
                               capture_output=True, text=True, check=True, timeout=60)
            self.assertEqual(r.stdout.strip(), want, f"{tag}: {path}")
        dts2 = os.path.join(td, f"{tag}.again.dts")
        out2 = os.path.join(td, f"{tag}.again.patched.dts")
        subprocess.run(["dtc", "-q", "-I", "dtb", "-O", "dts", "-o", dts2, dtb], check=True, timeout=120)
        text2 = read(dts2)
        again, changes = self.mod.patch_all(text2)
        self.assertEqual(changes, [], f"{tag}: run 2 reported changes")
        self.assertEqual(again, text2, f"{tag}: run 2 rewrote the text")
        r = subprocess.run([sys.executable, PATCHER, "--in", dts2, "--out", out2],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "unchanged", f"{tag}: CLI run 2")


if __name__ == "__main__":
    unittest.main(verbosity=1)
