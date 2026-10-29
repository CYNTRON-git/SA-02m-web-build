#!/usr/bin/env python3
"""test-dtb-patch.py — unit regression for usr/local/sbin/sa02m-dtb-patch.py
(quality row dtb-patch-unit; stdlib unittest, run as a script from the repo root).

Why it exists: the eth0 (EMAC) PHY of every SA-02m boots under the kernel's
"Generic PHY" because its ID is read while the PHY is still in reset
(docs/bugs/BUGLOG.md 2026-10-28). The patcher's job is to put the ID override
`ethernet-phy-id0243.0c54` FIRST in /soc/mdio@1c0b080/ethernet-phy@0's
`compatible` — a text edit on a decompiled blob that no syntax gate can judge,
so its behaviour is pinned here on a hand-written fixture in dtc's decompiled
idiom (scripts/dev/fixtures/dtb-patch/sun8i-a40i-sk.pre.dts: tabs, lowercase
hex, numeric phandles, a decoy `ethernet-phy@0` outside the EMAC mdio so a
non-brace-aware search would pick the wrong node).

Cases:
  U1 EMAC compatible rewritten id-first, ieee fallback second, deassert <0x7a120>;
     the decoy node is untouched.
  U2 GMAC still patched as the legacy patch_dtb_all.py did (status okay,
     syscon <0x02>, phy compatible icplus,ip101a first, deassert <0x7a120>),
     dc1sw regulator-always-on, i2c@1c2b800 okay.
  U3 idempotent: a second patch_all returns identical bytes and no changes.
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
     device-tree-compiler): decompile tools/imaging/boot/sun8i-a40i-sk.dtb,
     patch, compile, `fdtget -ts … compatible` first token is the id string,
     a second compile of the same dts is byte-identical, size within 8 KiB of
     the original. Anchors structure (reset-gpios present), not the pre-fix
     compatible, so a refreshed (already-patched) blob keeps the test green.

Non-vacuous: a missing patcher or fixture is a hard error (the loader raises),
never a skip; every case asserts on the patched text, not on the exit code
alone.

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
REAL_DTB = os.path.join(ROOT, "tools", "imaging", "boot", "sun8i-a40i-sk.dtb")
EMAC_ID = "ethernet-phy-id0243.0c54"
IEEE = "ethernet-phy-ieee802.3-c22"


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
    return re.findall(r'"([^"]*)"', m.group(1))


def own_deassert(mod, node):
    m = mod._own_prop(node, mod._DEASSERT_RE)
    return m.group(0) if m else None


class DtbPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_patcher()
        if not os.path.isfile(FIXTURE):
            raise FileNotFoundError(f"fixture missing: {FIXTURE}")
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
