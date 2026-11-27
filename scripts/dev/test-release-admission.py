#!/usr/bin/python3
# comment-mutation-proof-exempt: exercises advisory parsing, query coverage, and decision replay through callable production code.
"""Exercise provider, coverage, severity, alias and fail-closed admission semantics."""
from pathlib import Path
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
from release_admission import openssl
from release_admission.native_node import catalogue, on_linux
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
