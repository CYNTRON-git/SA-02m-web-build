#!/usr/bin/python3
# comment-mutation-proof-exempt: exercises advisory parsing, query coverage, and decision replay through callable production code.
"""Exercise provider, coverage, severity, alias and fail-closed admission semantics."""
from pathlib import Path
import argparse
import importlib.util
import io
import hashlib
import json
import sys
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from release_admission.inventory import Inventory
from release_admission import homekit_absence, homekit_review, openssl, ubuntu_tracker
from release_admission.native_node import capture as capture_node, catalogue, on_linux, replay as replay_node
from release_admission.ubuntu import tracker_row, upstream
from native_node_payload import derive
from release_admission.providers import Transport,level,osv_matches,osv_records,sha
spec=importlib.util.spec_from_file_location("release",HERE/"release-admission.py")
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
policy=json.loads(release.POLICY.read_bytes())
tool=release.POLICY.parent/policy["cvss"]["file"]
assert sha(tool)==policy["cvss"]["sha256"]
sys.path.insert(0,str(tool))
import cvss


class AdmissionTests(unittest.TestCase):
    def test_homekit_absence_requires_same_boot_packages_and_no_unit_or_runtime(self):
        now = time.time()
        board = {"boot_id":"11111111-2222-3333-4444-555555555555", "kernel_release":"test",
                 "homekit":{"exists":False, "distributions":[]}, "dpkg":{"stdout":"physical packages"}}
        proof = {"schema":1, "target":"136", "captured":now, "boot_id":board["boot_id"], "kernel_release":"test",
                 "dpkg_sha256":hashlib.sha256(b"physical packages").hexdigest(), "unit":dict(homekit_absence.ABSENT_UNIT),
                 "processes":[], "paths":dict.fromkeys(homekit_absence.PATHS, False)}
        self.assertTrue(homekit_absence.matches(proof, board, "136", now))
        for field, value in [("boot_id", "old"), ("dpkg_sha256", "different"), ("captured", now-86401), ("target", "135"),
                             ("processes", [21]), ("paths", {}), ("unit", dict(proof["unit"], ExecStart="unexpected")),
                             ("unit", dict(proof["unit"], DropInPaths="/etc/override.conf")), ("paths", dict.fromkeys(homekit_absence.PATHS, 0))]:
            with self.subTest(field=field, value=value):
                self.assertFalse(homekit_absence.matches(dict(proof, **{field:value}), board, "136", now))
        for homes in ({}, {"exists":False}, {"exists":True, "distributions":[]}, {"exists":False, "distributions":["pyhap"]}):
            self.assertFalse(homekit_absence.matches(proof, dict(board, homekit=homes), "136", now))
        for devices in ({}, {"136":{}}, {"136":{"homekit_absence_verified":True}, "135":{}}):
            inv = Inventory(); inv.devices = devices
            with self.assertRaisesRegex(ValueError, "absent donor"):
                homekit_review.apply({}, inv, None, {}, None, None)

    def test_homekit_review_retains_severity_and_refuses_any_evidence_drift(self):
        identifier = "CVE-2026-12345"
        wheel = "cryptography-50.0.2-cp311-abi3-manylinux_2_31_armv7l.whl"
        component = {"wheel":wheel,"elf":"cryptography/hazmat/bindings/_rust.abi3.so","version":"4.0.3"}
        raw = {"package":"OpenSSL", "version":"4.0.3", "identifiers":[identifier], "range":{"version":"4.0.0"},
               "cna_sha256":"frozen CNA", "levels":["HIGH"], "locations":["HomeKit lock:"+wheel+"!"+component["elf"]]}
        old = [{"id":identifier, "range":{"version":"historic"}}]
        result = {"component":component, "catalogue_records":1, "compared_semver_ranges":1, "uncategorized_ranges":old,
                  "gaps":["1 upstream custom or non-semver OpenSSL ranges require provider assessment"], "findings":[raw]}
        url = openssl.cve_url(identifier)
        record = {"id":identifier, "status":"not_affected", "justification":"vulnerable_code_not_in_execute_path",
                  "prerequisite":"reviewed condition", "record_url":url, "record_sha256":"frozen external CVE",
                  "range":raw["range"], "cna_sha256":raw["cna_sha256"], "levels":raw["levels"]}
        review = {"records":[record], "artifact":dict(component), "upstream_records":1, "upstream_compared_semver_ranges":1,
                  "legacy_ranges":{"count":1,"distinct_cves":1,"rows_sha256":hashlib.sha256(release.canonical(old)).hexdigest()}}
        net = Mock(); key = hashlib.sha256(release.canonical([url,None])).hexdigest(); net.seen = {key:{"sha256":record["record_sha256"]}}
        digest = policy["homekit_review_sha256"]
        approved, = homekit_review.reviewed_findings(result, review, net, digest)
        self.assertEqual(approved["levels"], ["HIGH"])
        self.assertFalse(release.blocks_finding("homekit", approved, policy))
        self.assertTrue(release.blocks_finding("homekit", dict(approved, review_sha256="other"), policy))
        self.assertTrue(release.blocks_finding("homekit", dict(approved, locations=["another wheel"]), policy))
        self.assertTrue(release.blocks_finding("opcua-native", approved, policy))
        for field, value in [("uncategorized_ranges", []), ("findings", [raw,raw]), ("gaps", []), ("component", dict(component,version="4.0.2")),
                             ("findings", [dict(raw, cna_sha256="changed")]), ("findings", [dict(raw, levels=["CRITICAL"])])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                homekit_review.reviewed_findings(dict(result, **{field:value}), review, net, digest)
        net.seen[key]["sha256"] = "changed"
        with self.assertRaises(ValueError):
            homekit_review.reviewed_findings(result, review, net, digest)
        self.assertNotIn("applicability", raw)

    def test_homekit_source_graph_refuses_extra_top_level_module_or_dropin(self):
        repo = HERE.parents[1]
        folder = repo / "vendor-src/admission/homekit"
        review = {"product_files_sha256":sha(folder/"product-files.json"), "package_files_sha256":sha(folder/"package-files.json"),
                  "builder_inputs_sha256":sha(folder/"candidate.json"), "extra_source_files":{}}
        import shutil
        with tempfile.TemporaryDirectory() as place:
            root=Path(place)
            for relative in homekit_review.SOURCE_ROOTS:
                shutil.copytree(repo/relative, root/relative)
            injected=root/"opt/sa02m-homekit/sitecustomize.py"
            injected.touch()
            with self.assertRaisesRegex(ValueError,"module membership changed"):
                homekit_review.verify_graph(Inventory(), root, folder, review, root/"wheelhouse")
            injected.unlink()
            (root/"etc/systemd/system/sa02m-homekit.service.d").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError,"service drop-in"):
                homekit_review.verify_graph(Inventory(), root, folder, review, root/"wheelhouse")

    def test_homekit_openssl_preserves_scope_unknown_grammars_and_provider_score(self):
        identifier="CVE-2026-10000"
        product={"product":"OpenSSL","defaultStatus":"unaffected","versions":[
                 {"versionType":"semver","status":"affected","version":"4.0.0","lessThan":"4.0.3"},
                 {"versionType":"custom","status":"affected","version":"0.9.8a","lessThan":"0.9.8c"}]}
        raw=json.dumps({"containers":{"cna":{"affected":[product],"metrics":[{"other":{"content":{"text":"Low"}}}]}}}).encode()
        blob=hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
        transport=Mock()
        transport.fetch.side_effect=[[{"name":identifier+".json","sha":blob,"type":"file"}],
             {"cveMetadata":{"cveId":identifier},"containers":{"cna":{"metrics":[]},"adp":[{"metrics":[{
              "cvssV3_1":{"version":"3.1","vectorString":"CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"}}]}]}}]
        component={"version":"4.0.2","wheel":"crypto.whl","elf":"rust.so"}
        def matches(rows,version):
            self.assertEqual(rows,[{"id":"0","vulnerable_versions":">=4.0.0 <4.0.3"}])
            return {("openssl",version,"0"):True}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"cna.tar.gz"
            with tarfile.open(path,"w:gz") as archive:
                item=tarfile.TarInfo("repo/secjson/"+identifier+".json");item.size=len(raw)
                archive.addfile(item,io.BytesIO(raw))
            result=openssl.homekit(component,path,sha(path),transport,matches,cvss)
            self.assertEqual(result["findings"][0]["levels"],["HIGH","LOW"])
            self.assertEqual(result["findings"][0]["applicability"],"under_investigation")
            self.assertEqual(len(result["uncategorized_ranges"]),1)
            self.assertTrue(result["gaps"])
            self.assertTrue(release.blocks_finding("homekit",result["findings"][0],policy))
            with self.assertRaises(ValueError):openssl.homekit(component,path,"0"*64,transport,matches,cvss)
    def test_independent_applicability_scope_and_invalidation(self):
        identifier="CVE-2026-10000"
        url="https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/10xxx/"+identifier+".json"
        row={"id":identifier,"status":"not_affected","upstream_severity":"Low","external_severity":"HIGH",
             "external_cvss_v3_1":7.5,"prerequisite":"absent feature","invalidate_if":"changed entry"}
        blobs={identifier+".json":"tree-blob"}
        bundle=({"records":[row],"artifact":{"openssl_version":"4.0.3"}},blobs,
                {identifier:{"url":url,"sha256":"reviewed-record"}})
        inv=Inventory()
        inv.devices={"board":{"opcua_profile_matches":True}}
        inv.inputs["independent OpenSSL review"]=policy["opcua_review_sha256"]
        transport=Mock()
        key=hashlib.sha256(release.canonical([url,None])).hexdigest()
        for changed in (None,"catalogue","metric","receipt"):
            transport.fetch.side_effect=[[{"name":identifier+".json","sha":"new" if changed=="catalogue" else "tree-blob","type":"file"}],
                                         {"cveMetadata":{"cveId":identifier}}]
            transport.seen={key:{"sha256":"new" if changed=="metric" else "reviewed-record"}}
            if changed=="receipt": inv.devices={"board":{}}
            (finding,),gaps=openssl.evaluate(bundle,transport,inv)
            self.assertEqual(bool(gaps),changed is not None)
            self.assertEqual(release.blocks_finding("opcua-native",finding,policy),changed is not None)
            self.assertTrue(release.blocks_finding("homekit",finding,policy))
            self.assertTrue(release.blocks_finding("opcua-native",dict(finding,review_sha256="other"),policy))

    def test_native_receipt_requires_exact_dpkg_revision_and_library_hash(self):
        inv=Inventory()
        catalogue={"openssl":"4.0.3","members":{"cffi":"hash"},"system":{"ffi":{"path":"/lib/ffi",
                   "binary":"libffi8:armhf","versions":{"3":{"bytes":5,"sha256":"correct"}}}}}
        row={"path":"/lib/ffi","binary":"libffi8:armhf","version":"3","bytes":5,"sha256":"correct"}
        native={"openssl":"4.0.3","members":{"cffi":"hash"},"system":{"ffi":row}}
        inv.check_native(catalogue,native)
        for field in ("sha256","version","binary","path"):
            bad=dict(native,system={"ffi":dict(row,**{field:"unexpected"})})
            with self.assertRaises(ValueError):inv.check_native(catalogue,bad)
    def test_transient_network_results_replay_without_reissuing_requests(self):
        url="https://api.osv.dev/v1/querybatch"
        request={"queries":[]}
        response=Mock()
        response.status=200; response.geturl.return_value=url
        response.read.return_value=b'{"results":[]}'
        from contextlib import nullcontext
        for failure,allowed in [(TimeoutError("timeout"),True),(HTTPError(url,503,"busy",None,None),True),
                                (HTTPError(url,404,"gone",None,None),False)]:
            with tempfile.TemporaryDirectory() as tmp, patch("release_admission.providers.urlopen") as network:
                network.side_effect=[failure,nullcontext(response)]
                t=Transport(Path(tmp))
                if allowed:
                    self.assertEqual(t.fetch(url,request),{"results":[]})
                    self.assertEqual(network.call_count,2)
                else:
                    with self.assertRaises(OSError):t.fetch(url,request)
                    self.assertEqual(network.call_count,1)
                replay=Transport(Path(tmp),t.seen)
                network.reset_mock()
                if allowed: self.assertEqual(replay.fetch(url,request),{"results":[]})
                else:
                    with self.assertRaises(OSError):replay.fetch(url,request)
                network.assert_not_called()
                self.assertEqual(t.seen,replay.seen)
                self.assertEqual(list(t.seen.values())[0]["failures"],[str(failure)])
    def test_severity_and_conflicts_use_upstream_parser(self):
        r={"database_specific":{"severity":"moderate"},"severity":[{"type":"CVSS_V3","score":"CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}
        self.assertEqual(level(r,cvss),"CRITICAL")
        self.assertEqual(level({},cvss),"UNKNOWN")
        self.assertEqual(level({"severity":[{"type":"Ubuntu","score":"medium"}]},cvss),"MODERATE")
        with self.assertRaises(ValueError):level({"severity":[{"type":"custom","score":"1"}]},cvss)

    def test_empty_partial_and_paginated_batch(self):
        keys={("PyPI","p","1"),("npm","n","2")}
        mock=Mock()
        mock.fetch.side_effect=[{"results":[{},{"next_page_token":"t"}]},{"results":[{"vulns":[{"id":"GHSA-x","modified":"2026-01-01T00:00:00Z"}]}]}]
        result=osv_matches(keys,mock)
        self.assertEqual(len(result),2)
        self.assertEqual(len(result[("npm","n","2")]),1)
        mock.fetch.side_effect=None;mock.fetch.return_value={"results":[{}]}
        with self.assertRaisesRegex(ValueError,"partial"):osv_matches(keys,mock)
        with self.assertRaises(ValueError):osv_matches(set(),mock)

    def test_osv_timestamp_precision_and_changed_revision(self):
        mock=Mock();mock.fetch.return_value={"id":"A","modified":"2026-01-01T00:00:00.123456789Z"}
        match={("PyPI","t","1"):{"A":"2026-01-01T00:00:00.123456Z"}}
        self.assertIn("A",osv_records(match,mock))
        mock.fetch.return_value["modified"]="2026-01-02T00:00:00.123456Z"
        with self.assertRaises(ValueError):osv_records(match,mock)

    def test_withdrawal_and_network_loss_do_not_hide_other_matches(self):
        mock=Mock()
        key=("PyPI","p","1")
        stamp="2026-01-01T00:00:00Z"
        def response(url):
            identifier=url.rsplit("/",1)[1]
            if identifier=="missing":raise OSError("provider unavailable")
            return {"id":identifier,"modified":stamp,"database_specific":{"severity":"high"},
                    **({"withdrawn":stamp} if identifier=="withdrawn" else {})}
        mock.fetch.side_effect=response
        errors={}
        records=osv_records({key:dict.fromkeys(["current","missing","withdrawn"],stamp)},mock,errors)
        self.assertEqual(set(errors),{"missing"})
        inv=Inventory();inv.add(*key,"python","wheel/p")
        findings=release.aggregate(records,{key:records},inv,cvss)["python"]
        self.assertEqual([row["identifiers"] for row in findings],[["current"]])

    def test_aliases_keep_all_physical_locations_and_distinct_upstreams(self):
        inv=Inventory();key=("npm","p","1")
        for loc in ("first","first/node_modules/p"):inv.add(*key,"native-node",loc)
        records={"A":{"aliases":["B"],"database_specific":{"severity":"low"}},"B":{"aliases":["A"],"database_specific":{"severity":"high"}},
                 "C":{"aliases":[],"upstream":["A"],"database_specific":{"severity":"medium"}}}
        rows=release.aggregate(records,{key:records},inv,cvss)["native-node"]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["levels"],["HIGH","LOW"])
        self.assertEqual(rows[0]["locations"],["first","first/node_modules/p"])

    def test_release_needs_all_scopes(self):
        scopes={s:{"status":"PASS"} for s in release.SCOPES}
        self.assertEqual(release.decision(scopes),"PASS")
        del scopes["vendor-kernel"];self.assertEqual(release.decision(scopes),"INDETERMINATE")
        scopes["ubuntu"]["status"]="FAIL";self.assertEqual(release.decision(scopes),"FAIL")

    def test_replay_requires_exact_url_query_and_recorded_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            trans=Transport(Path(tmp),{})
            with self.assertRaises(ValueError):trans.fetch("https://example.org/")
            with self.assertRaises(ValueError):trans.fetch("https://api.osv.dev/v1/querybatch",{})

    def test_replay_reads_exact_bytes_and_rejects_changed_response(self):
        url="https://api.osv.dev/v1/vulns/example"
        key=hashlib.sha256(release.canonical([url,None])).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/(key+".json")
            path.write_bytes(b'{"ok":true}')
            row={"url":url,"request":None,"bytes":path.stat().st_size,"sha256":sha(path)}
            self.assertEqual(Transport(Path(tmp),{key:row}).fetch(url),{"ok":True})
            path.write_bytes(b'{"ok":null}')
            with self.assertRaisesRegex(ValueError,"response mismatch"):
                Transport(Path(tmp),{key:row}).fetch(url)

    def test_stale_and_future_report_refused_before_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for age in (86401,-600):
                (Path(tmp)/"admission.json").write_text(json.dumps({"started":time.time()-age}))
                proc=subprocess.run([sys.executable,"-I","-B",str(HERE/"release-admission.py"),"check","--out",tmp,
                         "--artifact-dir",tmp,"--tool-dir",tmp,"--targets",str(Path(tmp)/"targets.json")],
                        capture_output=True,text=True)
                self.assertEqual(proc.returncode,2)
                self.assertIn("stale/future",json.loads(proc.stdout)["error"])

    def test_pinned_inputs_and_schema(self):
        self.assertEqual(policy["exceptions"],[])
        self.assertEqual(policy["ttl_seconds"],86400)
        self.assertEqual(tuple(policy["scopes"]),release.SCOPES)
        self.assertEqual(set(policy["providers"]),set(release.SCOPES))

    def test_noble_source_status_and_distinct_priorities(self):
        raw={"id":"CVE-2026-1","priority":"medium","impact":{"baseMetricV4":{"cvssV4":{"baseSeverity":"CRITICAL"}}},
             "packages":[{"name":"python3.12","statuses":[{"release_codename":"jammy","status":"released"},
                        {"release_codename":"noble","status":"needs-triage","pocket":"security"}]}]}
        row=tracker_row("python3.12",raw["id"],raw)
        self.assertEqual((row["status"],row["priority"],row["upstream_levels"]),("needs-triage","MODERATE",["CRITICAL"]))
        self.assertIsNone(tracker_row("python3.11",raw["id"],raw))
        self.assertEqual(upstream({"id":"USN-42-1","upstream":["UBUNTU-CVE-2026-1","CVE-2026-1","CVE-2026-2"]}),
                         {"CVE-2026-1","CVE-2026-2"})

    def test_node_core_uses_official_platform(self):
        self.assertTrue(on_linux({"affectedEnvironments":["all"]}))
        self.assertTrue(on_linux({"affectedEnvironments":["win32","linux"]}))
        self.assertFalse(on_linux({"affectedEnvironments":["win32"]}))
        for env in ([],["unknown"],None):
            with self.assertRaises(ValueError):on_linux({"affectedEnvironments":env})

    def test_ship_capture_paths_are_literal_and_explicit_flags_take_precedence(self):
        args=argparse.Namespace(action="check",ubuntu_capture=None,ubuntu_tracker=None,
                                ecosystem_capture=None,node_capture=Path("pinned"))
        environment={"SA02M_RELEASE_UBUNTU_CAPTURE":"noble with spaces","SA02M_RELEASE_NODE_CAPTURE":"different",
                     "SA02M_RELEASE_UBUNTU_TRACKER":"$(unused)","SA02M_RELEASE_ECOSYSTEM_CAPTURE":""}
        release.configure_captures(args,environment)
        self.assertEqual((args.ubuntu_capture,args.ubuntu_tracker,args.node_capture,args.ecosystem_capture),
                         (Path("noble with spaces"),Path("$(unused)"),Path("pinned"),None))
        args.action="capture-ecosystems";args.ubuntu_capture=None
        release.configure_captures(args,environment)
        self.assertIsNone(args.ubuntu_capture)

    def test_ubuntu_tracker_object_chain_identity_date_and_ambiguous_membership(self):
        cve = "CVE-2026-12345"
        vector = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:H"
        raw = f"Candidate: {cve}\nPriority: high\nCVSS:\n upstream: {vector} [8.2 HIGH]\nnoble_demo: ignored (see notes)\n".encode()
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); objects=folder/"objects";objects.mkdir()
            def save(kind, content):
                h=hashlib.sha1(kind.encode()+b" "+str(len(content)).encode()+b"\0"+content).hexdigest()
                (objects/h).write_bytes(content)
                return h
            def entry(mode, name, digest):
                return mode.encode()+b" "+name.encode()+b"\0"+bytes.fromhex(digest)
            def proof(body=raw, duplicate=False, stamp=None, mode="100644"):
                stamp=int(time.time()) if stamp is None else stamp
                blob=save("blob",body)
                active=save("tree",entry(mode,cve,blob)); empty=save("tree",b"")
                root=save("tree",entry("40000","active",active)+entry("40000","ignored",empty)
                          +entry("40000","retired",active if duplicate else empty))
                commit=save("commit",f"tree {root}\ncommitter Ubuntu <ubuntu@example.invalid> {stamp} +0000\n\nrecord\n".encode())
                snap={"schema":1,"repository":ubuntu_tracker.REPOSITORY,"started":time.time(),"commit":commit}
                return ubuntu_tracker.Tracker(folder,snap,commit,86400,cvss),blob
            tracker, blob=proof(); row=tracker.row(cve,"demo")
            self.assertEqual((row["priority"],row["status"],row["upstream_levels"],row["tracker_git"]["git_blob"]),
                             ("HIGH","ignored",["HIGH"],blob))
            self.assertNotIn("pocket",row)
            v4="CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N/E:P"
            multi,_=proof(raw.replace(b"noble_demo:",(" upstream-v4: "+v4+" [2.1 LOW]\nnoble_demo:").encode()))
            self.assertEqual(multi.row(cve,"demo")["upstream_levels"],["HIGH","LOW"])
            record={"severity":[{"type":"Ubuntu","score":"high"},{"type":"CVSS_V3","score":vector}]}
            self.assertTrue(ubuntu_tracker.compatible_direct_record(record,row,cvss))
            self.assertFalse(ubuntu_tracker.compatible_direct_record(dict(record,severity=[{"type":"Ubuntu","score":"low"}]),row,cvss))
            for body,kwargs in [(raw.replace(cve.encode(),b"CVE-2026-54321"),{}),
                                (raw+b"Priority_demo: low\n",{}),(raw+b"noble_demo: ignored\n",{}),
                                (raw.replace(b"8.2 HIGH",b"8.1 HIGH"),{}),(raw.replace(b"CVSS:3.1",b"x"),{}),
                                (raw,{"duplicate":True}),(raw,{"mode":"120000"})]:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    bad,_=proof(body,**kwargs);bad.row(cve,"demo")
            with self.assertRaisesRegex(ValueError,"commit date"):
                proof(stamp=int(time.time())-86460)
            changed,blob=proof();(objects/blob).write_bytes(raw.replace(b"high",b"tiny"))
            with self.assertRaisesRegex(ValueError,"identity mismatch"):
                changed.row(cve,"demo")

    def test_ubuntu_tracker_parity_rejects_conflicting_decision_like_source(self):
        row={"status":"needed","description":"","priority":"MODERATE","upstream_levels":["HIGH"],
             "tracker_git":{"cvss_generations":["3"]}}
        tracker=Mock(commit="commit");tracker.row.return_value=row
        websites={name:{"packages":[{"name":"demo"}]} for name in ("CVE-2026-1234","CVE-2026-1235")}
        equal=lambda package,cve,web:dict(row)
        proven,audit=ubuntu_tracker.parity(tracker,websites,{"demo"},equal)
        self.assertEqual((len(proven),audit["matched"]),(1,2))
        def changing(package,cve,web):
            return dict(row,priority="HIGH") if cve.endswith("35") else row
        proven,audit=ubuntu_tracker.parity(tracker,websites,{"demo"},changing)
        self.assertFalse(proven)
        self.assertEqual(audit["differences"][0]["fields"],["priority"])
        tracker.row.side_effect=lambda cve,package:dict(row,tracker_git={"cvss_generations":["4"]}) if cve.endswith("35") else row
        proven,_=ubuntu_tracker.parity(tracker,websites,{"demo"},changing)
        self.assertEqual(proven,{ubuntu_tracker.decision_shape(row)})

    def test_ubuntu_tracker_fixed_version_requires_direct_range_and_debian_order(self):
        source={"package":{"name":"demo","ecosystem":"Ubuntu:24.04:LTS"},
                "ranges":[{"type":"ECOSYSTEM","events":[{"introduced":"0"},{"fixed":"1:4.0-3ubuntu1.2"}]}]}
        record={"affected":[source]}
        with self.assertRaisesRegex(ValueError,"direct OSV"):
            ubuntu_tracker.confirmed_release("other","1.0","1:4.0-3ubuntu1.2",record)
        if Path("/usr/bin/dpkg").is_file():
            ubuntu_tracker.confirmed_release("demo","1:4.0-3ubuntu1.1","1:4.0-3ubuntu1.2",record)
            for version in ("1:4.0-3ubuntu1.2","1:4.0-3ubuntu1.10","2:1.0"):
                with self.assertRaisesRegex(ValueError,"installed Debian"):
                    ubuntu_tracker.confirmed_release("demo",version,"1:4.0-3ubuntu1.2",record)

    def test_node_directory_reads_individual_pinned_blobs_and_replays(self):
        commit = "a" * 40
        api = "https://api.github.com/repos/nodejs/security-wg"
        raw = "https://raw.githubusercontent.com/nodejs/security-wg/" + commit + "/vuln/core/"
        record = {"cve": [], "severity": "high", "affectedEnvironments": ["linux"],
                  "vulnerable": "22.x", "patched": ">=22.9.0"}
        data = {name: json.dumps(value).encode() for name, value in
                [("1.json", record), ("2.json", dict(record, severity="low")), ("index.json", {"1": record})]}
        listing = []
        for name, body in data.items():
            digest = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
            listing.append({"name": name, "type": "file", "path": "vuln/core/" + name, "size": len(body), "sha": digest})
        bodies = {api + "/commits?path=vuln/core&per_page=1": json.dumps([{"sha":commit}]).encode(),
                  api + "/contents/vuln/core?ref=" + commit: json.dumps(listing).encode(),
                  **{raw + name: body for name, body in data.items() if name != "index.json"}}

        def open_url(request, timeout):
            body = io.BytesIO(bodies[request.full_url])
            body.status = 200
            body.geturl = lambda: request.full_url
            return body

        with tempfile.TemporaryDirectory() as tmp, patch("release_admission.providers.urlopen", side_effect=open_url):
            net = Transport(Path(tmp))
            revision, records = catalogue(net)
            self.assertEqual((revision, set(records), records["2"]["severity"]), (commit, {"1", "2"}, "low"))
            replay = Transport(Path(tmp), net.seen)
            self.assertEqual(catalogue(replay), (revision, records))
            folder = Path(tmp) / "standalone"
            self.assertEqual(capture_node(folder), 2)
            snapshot = json.loads((folder / "node.json").read_bytes())
            with patch("release_admission.providers.urlopen", side_effect=AssertionError("network on replay")):
                self.assertEqual(replay_node(folder, snapshot, 86400), (revision, records))
                for metadata in (dict(snapshot, catalogue_commit="f"*40), dict(snapshot, record_count=1),
                                 dict(snapshot, started=time.time()-86401), dict(snapshot, started=time.time()+300),
                                 dict(snapshot, responses=dict(list(snapshot["responses"].items())[1:]))):
                    with self.subTest(metadata=set(metadata)), self.assertRaises(ValueError):
                        replay_node(folder, metadata, 86400)
                blob = folder / "provider" / (hashlib.sha256(release.canonical([raw+"1.json",None])).hexdigest()+".json")
                blob.write_bytes(blob.read_bytes().replace(b"high", b"down"))
                with self.assertRaisesRegex(ValueError, "response mismatch"):
                    replay_node(folder, snapshot, 86400)
            self.assertEqual(replay.seen, net.seen)
            self.assertEqual({row["url"] for row in net.seen.values()}, set(bodies))
            # Changed bytes still decode to a valid advisory with the same length.
            bodies[raw + "2.json"] = bodies[raw + "2.json"].replace(b'"low"', b'"bad"')
            with self.assertRaisesRegex(ValueError, "Git blob identity mismatch"):
                catalogue(Transport(Path(tmp)))

    def test_node_rejects_duplicate_and_truncated_directories(self):
        member = {"name":"1.json", "path":"vuln/core/1.json", "type":"file", "size":1, "sha":"a" * 40}
        for listing in ([member, member], [member] * 1000, [dict(member, type="symlink")]):
            transport = Mock()
            transport.fetch.side_effect = [[{"sha":"b" * 40}], listing]
            with self.subTest(size=len(listing)), self.assertRaises(ValueError):
                catalogue(transport)

    def test_native_recipe_preserves_cli_and_publishes_after_verification(self):
        def archive(path, mode, members):
            with tarfile.open(path,mode) as tar:
                for name,body in members.items():
                    info=tarfile.TarInfo(name)
                    if body.startswith(b"link:"):
                        info.type=tarfile.SYMTYPE
                        info.linkname=body[5:].decode()
                        tar.addfile(info)
                    else:
                        info.size=len(body)
                        tar.addfile(info,io.BytesIO(body))
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            old,runtime,out=[folder/n for n in ("upstream.tar.xz","runtime.tar.gz","result.tar.xz")]
            prefix="node/lib/node_modules/npm/"
            members={"node/bin/node":b"binary","node/bin/npm":b"link:../lib/node_modules/npm/bin/npm-cli.js",
                     "node/bin/npx":b"link:../lib/node_modules/npm/bin/npx-cli.js","node/bin/corepack":b"corepack",
                     prefix+"obsolete":b"obsolete"}
            archive(old,"w:xz",members)
            incoming="node-red/node_modules/npm/"
            archive(runtime,"w:gz",{incoming+"package.json":b'{"name":"npm","version":"11.21.3"}',
                    incoming+"bin/npm-cli.js":b"npm",incoming+"bin/npx-cli.js":b"npx"})
            expected=derive(old,sha(old),runtime,sha(runtime),"node","11.21.3",out)
            with tarfile.open(out) as tar:
                self.assertNotIn(prefix+"obsolete",tar.getnames())
                self.assertEqual(tar.extractfile("node/bin/node").read(),b"binary")
                self.assertEqual(tar.getmember("node/bin/npx").linkname,members["node/bin/npx"][5:].decode())
            for digest,version in (("0"*64,"11.21.3"),(sha(old),"99")):
                with self.assertRaises(ValueError):derive(old,digest,runtime,sha(runtime),"node",version,out)
                self.assertEqual(sha(out),expected)
            del members["node/bin/corepack"]
            archive(old,"w:xz",members)
            with self.assertRaisesRegex(ValueError,"public command"):derive(old,sha(old),runtime,sha(runtime),"node","11.21.3",out)
            self.assertEqual(sha(out),expected)


if __name__=="__main__":
    unittest.main()
