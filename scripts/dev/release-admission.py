#!/usr/bin/env python3
# comment-mutation-proof-exempt: computes decisions over actual artifacts, official provider responses and device receipts.
"""Admit the exact release artifacts and device inventories against fresh official feeds."""
import argparse
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
sys.path.insert(0,str(HERE))
from release_admission.inventory import Inventory
from release_admission.native_node import check as native_node
from release_admission.providers import Transport, canonical, level, osv_matches, osv_records, require, sha
from release_admission import ubuntu as noble
from release_admission import openssl

POLICY=REPO/"vendor-src/admission/policy.json"
SCOPES=("node-red","python","python-ambient","homekit","development","native-node","external-node","ubuntu","vendor-kernel","opcua-native")


def read(path):
    return json.loads(path.read_bytes())


def load_nodered():
    spec=importlib.util.spec_from_file_location("nodered_admission",HERE/"nodered-admission.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decision(results):
    if any(s["status"]=="FAIL" for s in results.values()):
        return "FAIL"
    if set(results)!=set(SCOPES) or any(s["status"]!="PASS" for s in results.values()):
        return "INDETERMINATE"
    return "PASS"


def blocks_finding(scope,row,policy):
    independently_excluded=(scope=="opcua-native" and row.get("package")=="OpenSSL"
        and row.get("locations")==["opcua:sa02m-mqtt-opcua"] and row.get("review_sha256")==policy["opcua_review_sha256"]
        and row.get("applicability")=="not_affected")
    return bool(set(row["levels"]).intersection(policy["blocking"])) and not independently_excluded


def aggregate(records, matches, inventory, cvss):
    result=defaultdict(list)
    for (eco,name,version),ids in matches.items():
        places=inventory.paths[(eco,name,version)]
        rows={}
        for identifier in ids:
            rec=records[identifier]
            if rec.get("withdrawn"):
                continue
            try:
                severity=level(rec,cvss)
            except (ValueError,KeyError,TypeError) as exc:
                severity="UNKNOWN"
                for scope in {p["scope"] for p in places}:
                    inventory.gaps[scope].append(identifier+": "+str(exc))
            names=set(rec.get("aliases",[]))|{identifier}
            # Upstream and related CVEs can describe several distinct fixes in
            # a single USN; only explicit aliases merge an advisory identity.
            merge=[key for key,row in rows.items() if names.intersection(row["identifiers"])]
            entry={"identifiers":set(names),"levels":{severity},"upstream":set(rec.get("upstream",[])),
                   "fixed":set(),"package":name,"version":version,"ecosystem":eco}
            for affected in rec.get("affected",[]):
                p=affected["package"]
                if p["ecosystem"]==eco and p["name"].lower()==name.lower():
                    for ran in affected.get("ranges",[]):
                        entry["fixed"].update(e["fixed"] for e in ran["events"] if "fixed" in e)
            for key in merge:
                old=rows.pop(key)
                for field in ("identifiers","levels","upstream","fixed"):
                    entry[field].update(old[field])
            rows[min(entry["identifiers"])]=entry
        for entry in rows.values():
            for key in ("identifiers","levels","upstream","fixed"):
                entry[key]=sorted(entry[key])
            for scope in {p["scope"] for p in places}:
                result[scope].append(dict(entry,locations=sorted({p["path"] for p in places if p["scope"]==scope})))
    return result


def execute(args):
    policy=read(POLICY)
    require(tuple(policy["scopes"])==SCOPES and policy["blocking"]==["HIGH","CRITICAL"] and
            policy["ttl_seconds"]==86400 and policy["exceptions"]==[], "unsupported release policy")
    out=args.out
    if args.action!="check":
        out.mkdir(parents=True,exist_ok=False)
        started=time.time()
        previous=None
    else:
        previous=read(out/"admission.json")
        started=previous["started"]
        require(0<=time.time()-started<=86400,"stale/future report")
    net=Transport(out/"provider",previous["responses"] if previous else None)
    inv=Inventory()
    for path in [Path(__file__),POLICY,*sorted((HERE/"release_admission").glob("*.py"))]:
        inv.file(path.relative_to(REPO).as_posix(),path)
    pin=policy["cvss"]
    wheel=inv.file("CVSS parser",POLICY.parent/pin["file"])
    require(wheel.stat().st_size==pin["bytes"] and sha(wheel)==pin["sha256"],"unverified CVSS parser")
    sys.path.insert(0,str(wheel))
    import cvss
    require(str(cvss.__file__).startswith(str(wheel)), "unexpected CVSS parser origin")
    inv.add("PyPI",pin["name"],pin["version"],"development","scanner:"+pin["file"])
    node=load_nodered()
    version=inv.project(REPO,args.artifact_dir,node)
    targets=inv.json("release targets",args.targets)
    require(targets.get("schema")==1 and targets["devices"] and len(targets["devices"])<=32,"invalid target inventory")
    if targets.get("homekit_arm_wheel"):
        inv.hk_native=inv.homekit_wheel(args.targets.parent/targets["homekit_arm_wheel"])
    else:
        inv.hk_native=None
        inv.gaps["homekit"].append("locked ARM cryptography wheel unavailable for native dependency coverage")
    for name,data in sorted(targets["devices"].items()):
        def path(key):
            return args.targets.parent/data[key] if data.get(key) else None
        inv.board(name,path("inventory"),path("boot_artifacts"),path("python_receipt"),path("homekit_integrity"),
                  path("system_python"),path("homekit_origins"),path("homekit_native"))
        inv.external_node(name,path("node_lineage"),path("node_packages"),path("node_versions"))
    results={k:{"findings":[],"gaps":inv.gaps[k],"status":"INDETERMINATE"} for k in SCOPES}
    def isolate(scopes, callback):
        try:
            return callback()
        except Exception as exc:
            for s in scopes:
                inv.gaps[s].append(str(exc))
            return None
    def load_native_review():
        require(targets.get("openssl_review"),"independent OPC UA OpenSSL evidence missing")
        return openssl.load(inv,REPO,args.targets.parent/targets["openssl_review"],policy["opcua_review_sha256"])
    native_review=isolate(["opcua-native"],load_native_review)
    if native_review:
        for name,data in targets["devices"].items():
            source=data.get("opcua_profile")
            observation=inv.json("installed OPC UA profile "+name,args.targets.parent/source) if source else {}
            inv.devices[name]["opcua_profile_matches"]=openssl.selected_profile(observation,inv.devices[name],
                    inv.inputs,inv.runtime["groups"])
    def ubuntu_scan():
        if not args.ubuntu_capture:
            return noble.collect(args.targets,net)
        folder=args.ubuntu_capture
        snapshot=inv.json("Ubuntu advisory capture",folder/"ubuntu.json")
        require(0<=time.time()-snapshot["started"]<=policy["ttl_seconds"],"stale/future Ubuntu capture")
        sub=Transport(folder/"provider",snapshot["responses"])
        current=noble.collect(args.targets,sub)
        require(canonical(current)==canonical(snapshot["result"]) and sub.seen==snapshot["responses"],"Ubuntu replay differs")
        return current
    eco_net=net
    queries={k for k in inv.paths if k[0]!="Ubuntu:24.04:LTS"}
    # Native receipts establish on-device origins during the full decision.
    # Source closure and exact observed package queries can be captured while
    # the independent target verification is still running.
    provider_inputs={k:v for k,v in inv.inputs.items() if k!="release targets" and
                     not k.startswith(("native Python receipt ","installed OPC UA profile ","HomeKit native origins "))}
    if args.ecosystem_capture:
        folder=args.ecosystem_capture
        snapshot=read(folder/"ecosystems.json")
        require(snapshot["schema"]==2 and snapshot["provider_inputs"]==provider_inputs and
                snapshot["queries"]==[list(k) for k in sorted(queries)],"ecosystem capture inputs differ")
        require(0<=time.time()-snapshot["started"]<=policy["ttl_seconds"],"stale/future ecosystem capture")
        inv.file("ecosystem advisory capture",folder/"ecosystems.json")
        eco_net=Transport(folder/"provider",snapshot["responses"])
    for eco in ("PyPI","npm","crates.io"):
        keys={k for k in queries if k[0]==eco}
        scopes={row["scope"] for key in keys for row in inv.paths[key]}
        def analyze(part):
            m=osv_matches(part,eco_net)
            missing={}
            records=osv_records(m,eco_net,missing)
            for key,matched in m.items():
                for identifier in set(matched)&set(missing):
                    for scope in {p["scope"] for p in inv.paths[key]}:
                        inv.gaps[scope].append(identifier+": OSV record unavailable")
                    del matched[identifier]
            return aggregate(records,m,inv,cvss)
        ordered=sorted(keys)
        for start in range(0,len(ordered),100):
            part=ordered[start:start+100]
            affected_scopes={place["scope"] for key in part for place in inv.paths[key]}
            try:
                batch=analyze(part)
            except Exception as exc:
                for s in affected_scopes:
                    affected=[f"{n}@{v}" for e,n,v in part if any(p["scope"]==s for p in inv.paths[e,n,v])]
                    inv.gaps[s].append(f"{eco} OSV batch {start//100+1}: {exc}; affected inputs: "+", ".join(affected))
                continue
            for s,f in (batch or {}).items():
                results[s]["findings"].extend(f)
    if native_review:
        assessed=isolate(["opcua-native"],lambda:openssl.evaluate(native_review,eco_net,inv))
        if assessed:
            findings,gaps=assessed
            results["opcua-native"]["findings"].extend(findings)
            inv.gaps["opcua-native"].extend(gaps)
            results["opcua-native"]["pyca_workspace_crates"]=inv.local_crates
    if args.action=="capture-ecosystems":
        (out/"ecosystems.json").write_text(json.dumps({"schema":2,"started":started,"inputs":inv.inputs,
              "provider_inputs":provider_inputs,"queries":sorted(queries),
              "responses":net.seen,"results":{s:r for s,r in results.items() if s not in {"ubuntu","vendor-kernel"}}},sort_keys=True)+"\n")
        print(json.dumps({"captured_responses":len(net.seen)}))
        return 0
    if args.ecosystem_capture:
        require(eco_net.seen==snapshot["responses"],"incomplete ecosystem replay")
    ubuntu=isolate(["ubuntu"],ubuntu_scan)
    if ubuntu:
        results["ubuntu"]["withdrawn"]=ubuntu["withdrawn"]
        results["ubuntu"]["findings"]=ubuntu["findings"]
        inv.gaps["ubuntu"].extend(ubuntu["gaps"])
    node_policy=node.read_json(node.POLICY)
    inv.file("Node semver interpreter",args.tool_dir/"node")
    def homekit_scan():
        require(targets.get("openssl_review"),"OpenSSL CNA archive missing")
        archive=args.targets.parent/targets["openssl_review"]/"openssl-release-metadata.tar.gz"
        with tempfile.TemporaryDirectory(dir=out) as stage:
            work=Path(stage)
            node.tools_verified(node_policy,args.tool_dir)
            module=node.semver_module(args.artifact_dir,REPO,work,node_policy)
            def compare(cases,version):
                return node.range_matches({"openssl":cases},{"openssl":[version]},args.tool_dir/"node",module,work)
            return openssl.homekit(inv.hk_native,archive,policy["openssl_cna_archive_sha256"],net,compare,cvss)
    if inv.hk_native:
        component=isolate(["homekit"],homekit_scan)
        if component:
            results["homekit"]["findings"].extend(component.pop("findings"))
            inv.gaps["homekit"].extend(component.pop("gaps"))
            results["homekit"]["native"]=component
    def node_check(core_version):
        with tempfile.TemporaryDirectory(dir=out) as stage:
            return native_node(core_version,args.artifact_dir,args.tool_dir,Path(stage),node,node_policy,net)
    for core_version in sorted(set(inv.node_versions)|{version}):
        scopes=["native-node"] if core_version==version else []
        if core_version in inv.node_versions:
            scopes.append("external-node")
        native=isolate(scopes,lambda:node_check(core_version))
        if native:
            for scope in scopes:
                results[scope].setdefault("upstream",{})[core_version]=native
                for row in native["findings"]:
                    places=inv.node_versions[core_version] if scope=="external-node" else ["archive:bin/node"]
                    results[scope]["findings"].append({"identifiers":[row["id"],*row["aliases"]],"levels":[row["severity"]],
                        "package":"node","version":core_version,"locations":places})
    def npm_check():
        sub=out/"npm"
        rc=subprocess.run([sys.executable,"-I","-B",str(HERE/"nodered-admission.py"),args.action,
                           "--artifact-dir",str(args.artifact_dir),"--tool-dir",str(args.tool_dir),"--out",str(sub)],timeout=450).returncode
        data=read(sub/"admission.json")
        require(rc in (0,1) and data["status"] in {"PASS","FAIL"},"npm provider unavailable")
        results["node-red"]["npm_bulk"]={"status":data["status"],"raw_sha256":data["raw_sha256"],
                                       "query_sha256":data["query_sha256"],"findings":data["findings"]}
        return data["status"]
    npm_status=isolate(["node-red"],npm_check)
    for scope,row in results.items():
        findings=row["findings"]
        blocked=any(blocks_finding(scope,f,policy) for f in findings)
        if any("UNKNOWN" in f["levels"] for f in findings):
            row["gaps"].append("one or more advisory severities unavailable")
        row["gaps"]=sorted(set(row["gaps"]))
        row["status"]="FAIL" if blocked or (scope=="node-red" and npm_status=="FAIL") else "INDETERMINATE" if row["gaps"] else "PASS"
    report={"schema":1,"started":started,"ttl_seconds":86400,"inputs":inv.inputs,"responses":net.seen,
            "coverage":{s:sum(p["scope"]==s for rows in inv.paths.values() for p in rows) for s in SCOPES},
            "devices":inv.devices,"scopes":results,"status":decision(results),"lower_findings_owner":policy["lower_findings_owner"]}
    if previous:
        require(canonical(report)==canonical(previous),"cached decision/input/coverage mismatch")
    else:
        (out/"admission.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"status":report["status"],"scopes":{s:r["status"] for s,r in results.items()}}))
    return {"PASS":0,"FAIL":1,"INDETERMINATE":2}[report["status"]]


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["scan","check","capture-ubuntu","capture-ecosystems"])
    for name in ("artifact-dir","tool-dir","targets","out"):
        parser.add_argument("--"+name,required=name in {"targets","out"},type=Path)
    parser.add_argument("--ubuntu-capture",type=Path)
    parser.add_argument("--ecosystem-capture",type=Path)
    try:
        args=parser.parse_args()
        if args.action=="capture-ubuntu":
            data=noble.capture(args.targets,args.out)
            print(json.dumps({"findings":len(data["findings"]),"gaps":len(data["gaps"])}))
            raise SystemExit(0)
        require(args.artifact_dir and (args.action=="capture-ecosystems" or args.tool_dir),"artifact-dir and tool-dir required")
        raise SystemExit(execute(args))
    except Exception as exc:
        print(json.dumps({"status":"INDETERMINATE","error":str(exc)}))
        raise SystemExit(2)
