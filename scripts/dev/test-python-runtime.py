#!/usr/bin/python3
# comment-mutation-proof-exempt: exercises wheel contents, runtime entry points, native integrity and refusal on filesystem mutations.
"""Offline product catalogue, trust and consumer-boundary regressions."""
from contextlib import redirect_stdout
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[2]
CAT=ROOT/"opt/sa02m-python-manager"
sys.path.insert(0,str(CAT))
from integrity import Unavailable, sha, tree_digest, trusted
from native_guard import stamp,check
import runtime
from runtime import Catalogue


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.manifest=json.loads((CAT/"runtime.json").read_bytes())
        self.map=json.loads((CAT/"consumers.json").read_bytes())

    def test_exact_wheels_and_transitive_requirements(self):
        data=self.manifest
        pins=data["packages"]
        wheelhouse=CAT/"wheelhouse"/data["wheelset"]
        self.assertEqual(set(p.name for p in wheelhouse.iterdir()),{r["file"] for r in pins.values()})
        for name,row in pins.items():
            path=wheelhouse/row["file"]
            self.assertEqual((path.stat().st_size,sha(path)),(row["bytes"],row["sha256"]))
            with zipfile.ZipFile(path) as archive:
                metadata,=[p for p in archive.namelist() if p.endswith(".dist-info/METADATA")]
                meta=BytesParser().parsebytes(archive.read(metadata))
                self.assertEqual(meta["Version"],row["version"])
                self.assertFalse(any(p.endswith(".pth") for p in archive.namelist()))
        for group, config in data["groups"].items():
            expected=[f"{n}=={pins[n]['version']} --hash=sha256:{pins[n]['sha256']}" for n in config["packages"]]
            self.assertEqual((CAT/(group+".lock")).read_text().splitlines(),expected)

    def test_runtime_group_tracks_exact_consumers(self):
        entries=self.map["entries"]
        for name,row in entries.items():
            self.assertIn(row["group"],self.manifest["groups"])
            self.assertTrue((ROOT/row["source"]).is_file(),name)
            if row["kind"]=="script":
                path=row["target"].replace("/usr/local/libexec/sa02m-phase-energy","/opt/sa02m-phase-energy")
                self.assertTrue((ROOT/path.removeprefix("/")).is_file(),name)
            if name.startswith("sa02m-"):
                self.assertIn("ExecStart=",(ROOT/row["source"]).read_text())
        for service in ("sa02m-modbus-mqtt","sa02m-telemetry","sa02m-rs485-roster","sa02m-phase-energy"):
            self.assertEqual(entries[service]["group"],"mqtt")
        for item in self.map["preserved"]:
            self.assertTrue(all((ROOT/path).is_file() for path in item["sources"]))

    def test_bridge_has_exact_native_targets_and_single_metadata(self):
        rows=json.loads((CAT/"bridge.json").read_bytes())
        self.assertEqual(sha(CAT/"bridge.json"),self.manifest["bridge_sha256"])
        files={r["path"] for r in rows["files"]}
        self.assertEqual(len(files),len(rows["files"]))
        self.assertEqual(set(rows["packages"]),{"python3-cffi-backend","python3-cryptography"})
        self.assertTrue(any(p.endswith("_rust.abi3.so") for p in files))
        self.assertTrue(any(p.startswith("_cffi_backend.") and p.endswith(".so") for p in files))
        self.assertFalse(any("cryptography.egg-info" in p or p.endswith((".pth",".pyc")) for p in files))

    def test_opcua_uses_private_native_closure_and_new_entry(self):
        group=self.manifest["groups"]["opcua"]
        self.assertFalse(group["bridge"])
        self.assertEqual(len(group["packages"]),13)
        self.assertTrue({"cryptography","asyncua","cffi","pycparser"}<=set(group["packages"]))
        entry=self.map["entries"]["sa02m-mqtt-opcua"]
        self.assertEqual(entry["group"],"opcua")
        self.assertTrue(entry["target"].endswith("/sa02m-mqtt-opcua-managed.py"))
        self.assertTrue((ROOT/entry["source"]).read_text().count("/sa02m-mqtt-opcua.py -c")==1)
        native=group["native"]
        self.assertEqual(native["openssl"],"4.0.3")
        path=CAT/"wheelhouse"/self.manifest["wheelset"]
        for pin,ident in native["members"].items():
            with zipfile.ZipFile(path/self.manifest["packages"][pin]["file"]) as wheel:
                import hashlib
                self.assertEqual(hashlib.sha256(wheel.read(ident["path"])).hexdigest(),ident["sha256"])

    def test_hostile_pythonpath_and_foreign_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/"sitecustomize.py"
            script.write_text("raise SystemExit(69)")
            env=dict(os.environ,PYTHONPATH=tmp,PYTHONUSERBASE=tmp)
            proc=subprocess.run([sys.executable,"-I","-B",str(CAT/"launch.py"),"missing"],env=env,capture_output=True,text=True)
            self.assertNotEqual(proc.returncode,0)
            self.assertIn("KeyError",proc.stderr)
            proc=subprocess.run([sys.executable,"-I","-S","-B",str(CAT/"runtime.py"),"verify","../bad"],env=env,capture_output=True,text=True)
            self.assertEqual(proc.returncode,1)
            self.assertEqual(json.loads(proc.stdout)["code"],"verification_failed")

    def test_native_guard_rejects_sticky_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            target=root/"native.so"
            target.write_bytes(b"native-fixture")
            signature=[42,43,14,5,6]
            (root/"native-guard.json").write_text(json.dumps({"all":[str(target)],"mqtt":[],
                    "signatures":{str(target):{"stat":signature,"sha256":sha(target)}}}))
            with self.assertRaisesRegex(ValueError,"unsafe native"):check(root/"mqtt")

    @unittest.skipUnless(os.geteuid()==0, "requires root-owned fixture under /run")
    def test_native_guard_accepts_identical_clone_and_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory(dir="/run") as tmp:
            root=Path(tmp)
            target=root/"native.so"
            target.write_bytes(b"original")
            signature={"stat":stamp(target),"sha256":sha(target)}
            (root/"native-guard.json").write_text(json.dumps({"all":[str(target)],"mqtt":[],
                    "signatures":{str(target):signature}}))
            check(root/"mqtt")
            replacement=root/"copy"
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
            check(root/"mqtt")
            target.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError,"native runtime changed"):check(root/"mqtt")

    def test_insecure_ancestor_and_wrong_owner_or_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"empty"
            p.touch()
            with self.assertRaises(Unavailable):trusted(p)
            with self.assertRaises(ValueError):stamp(Path(tmp))

    def test_startup_hook_or_extra_symlink_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/"hidden.pth").touch()
            with self.assertRaises(Unavailable):tree_digest(p,{})
            (p/"hidden.pth").unlink()
            (p/"global").symlink_to("/usr/local")
            with self.assertRaises(Unavailable):tree_digest(p,{})

    @unittest.skipUnless(os.geteuid()==0,"requires a trusted catalogue fixture under /run")
    def test_wheelset_refuses_missing_corrupt_or_untracked_wheel(self):
        with tempfile.TemporaryDirectory(dir="/run") as tmp:
            clone=Path(tmp)/"manager"
            shutil.copytree(CAT,clone)
            # Bind-mount modes on a Windows test host can be broader than the
            # production install. Make trust properties explicit in the fixture.
            for path in (clone,*clone.rglob("*")):
                path.chmod(0o755 if path.is_dir() else 0o644)
            cat=Catalogue(clone)
            prior=cat.wheels.parent/"old-release"
            prior.mkdir()
            (prior/"obsolete.whl").touch()
            with patch("runtime.validate_abi"),patch("runtime.validate_bridge"),patch("runtime.validate_native"):
                cat.validate()
                unknown=cat.wheels/"untracked.whl"
                unknown.touch()
                with self.assertRaisesRegex(Unavailable,"wheelhouse inventory mismatch"):cat.validate()
                unknown.unlink()
                wheel=next(cat.wheels.glob("*.whl"))
                saved=wheel.read_bytes()
                wheel.unlink()
                with self.assertRaises(FileNotFoundError):cat.validate()
                wheel.write_bytes(bytes(len(saved)))
                with self.assertRaisesRegex(Unavailable,"file mismatch"):cat.validate()

    def test_verify_retries_only_a_real_subprocess_deadline(self):
        def expired():
            subprocess.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.04)

        def corrupt():
            raise ValueError("file mismatch: timed out after 35 seconds")

        def passed():
            return {"ok": True}

        cases = (
            ("verify", [expired, passed], 0, None, 1),
            ("verify", [expired, expired], 1, "verification_timeout", 1),
            ("verify", [corrupt], 1, "verification_failed", 0),
            ("verify", [expired, corrupt], 1, "verification_failed", 1),
            ("prepare", [expired], 1, "verification_failed", 0),
        )
        for action, steps, expected_rc, code, retries in cases:
            with self.subTest(action=action, steps=[step.__name__ for step in steps]):
                argv = ["runtime.py", action] + (["a"*64] if action == "verify" else [])
                attempts = iter(steps)
                output = io.StringIO()
                with (patch.object(sys, "argv", argv),
                      patch.object(runtime, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1))),
                      patch.object(runtime, "Catalogue"), patch.object(runtime, "Manager") as manager,
                      patch.object(runtime, "sleep", create=True) as pause, redirect_stdout(output)):
                    command = getattr(manager.return_value, action)
                    command.side_effect = lambda *args: next(attempts)()
                    self.assertEqual(runtime.main(), expected_rc)
                result = json.loads(output.getvalue())
                self.assertIs(result["ok"], expected_rc == 0)
                self.assertEqual(result.get("code"), code)
                self.assertEqual(command.call_count, len(steps))
                self.assertEqual(manager.call_count, len(steps))
                if code == "verification_timeout":
                    self.assertNotIn(sys.executable, result["error"])
                self.assertEqual(pause.call_count, retries)

    def test_system_manager_requires_no_site_and_returns_json(self):
        p=subprocess.run([sys.executable,"-I","-B",str(CAT/"runtime.py"),"prepare"],capture_output=True,text=True)
        self.assertEqual(p.returncode,1)
        self.assertIn("without site",json.loads(p.stdout)["error"])


if __name__=="__main__":
    unittest.main()
