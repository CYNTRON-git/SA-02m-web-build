"""Replay the independent applicability record for the exact managed OPC UA consumer."""
import hashlib
import json
import re
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

from .providers import canonical, require, sha


def load(inventory, repo, folder, expected_digest):
    report=inventory.file("independent OpenSSL review",folder/"review.md")
    require(sha(report)==expected_digest,"unregistered independent OpenSSL review")
    block,=re.findall(r"^```json\r?\n(.*?)\r?\n```",report.read_text(encoding="utf-8"),re.S|re.M)
    review=json.loads(block)
    inputs,artifact=review["evidence"],review["artifact"]
    freeze=inventory.file("OPC source freeze",folder/"owned-source-sha256.json")
    require(sha(freeze)==artifact["source_freeze_sha256"],"OPC source freeze changed")
    for row in json.loads(freeze.read_bytes()):
        path=inventory.file("OPC reviewed "+row["path"],repo/row["path"])
        require(sha(path)==row["sha256"] and path.stat().st_size==row["bytes"],"OPC reviewed source changed")
    entry="opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua-managed.py"
    config=inventory.file("OPC reference configuration",repo/"etc/sa02m-mqtt-opcua.conf")
    owners={name:row for name,row in inventory.consumers["entries"].items() if row["group"]=="opcua"}
    require(set(owners)=={"sa02m-mqtt-opcua"} and owners["sa02m-mqtt-opcua"]["target"]=="/"+entry and
            owners["sa02m-mqtt-opcua"]["roots"]==["/opt/sa02m-mqtt-opcua"],"reviewed consumer set changed")
    require(inventory.inputs["OPC reviewed "+entry]==artifact["managed_entry_sha256"] and
            sha(config)==artifact["reference_config_sha256"] and
            inventory.inputs["python opcua lock"]==artifact["private_opcua_lock_sha256"],"reviewed OPC profile changed")
    for name,key in [("paho-mqtt","paho_wheel_sha256"),("asyncua","asyncua_wheel_sha256"),("cryptography","wheel_sha256")]:
        require(inventory.inputs["wheel "+name]==artifact[key],"reviewed wheel changed "+name)
    native=inventory.runtime["groups"]["opcua"]["native"]
    require(native["openssl"]==artifact["openssl_version"] and native["members"]["cryptography"]==
            {"path":artifact["elf"],"sha256":artifact["elf_sha256"]},"reviewed OpenSSL ELF changed")
    with zipfile.ZipFile(inventory.wheelhouse/artifact["wheel"]) as wheel:
        sbom,=[p for p in wheel.namelist() if p.endswith(".dist-info/sboms/sbom.json")]
        require(hashlib.sha256(wheel.read(sbom)).hexdigest()==artifact["component_sbom_sha256"] and
                wheel.getinfo(artifact["elf"]).file_size==artifact["elf_bytes"],"reviewed native SBOM changed")
    archive=inventory.file("reviewed OpenSSL CNA archive",folder/"openssl-release-metadata.tar.gz")
    metrics=inventory.file("reviewed external OpenSSL metrics",folder/"native-cve-metrics.json")
    require(sha(archive)==inputs["upstream_archive_sha256"] and sha(metrics)==inputs["metrics_sha256"],
            "reviewed advisory provenance changed")
    blobs={}
    with tarfile.open(archive) as source:
        for member in source:
            if re.fullmatch(r"[^/]+/secjson/CVE-[0-9]{4}-[0-9]+\.json",member.name):
                require(member.isfile() and member.size<2**20,"oversized CNA record")
                raw=source.extractfile(member).read()
                name=member.name.rsplit("/",1)[1]
                require(name not in blobs,"duplicate upstream CNA record")
                blobs[name]=hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
    scored={r["id"]:r for r in json.loads(metrics.read_bytes())}
    require(len(blobs)==inputs["upstream_records"] and {r["id"] for r in review["records"]}==set(scored)
            and len(scored)==review["summary"]["all_matching"],"incomplete reviewed catalogue")
    return review,blobs,scored


def verify_catalogue(transport,blobs):
    listing=transport.fetch("https://api.github.com/repos/openssl/release-metadata/contents/secjson")
    require(isinstance(listing,list) and 0<len(listing)<1000,"incomplete upstream directory")
    current={f["name"]:f["sha"] for f in listing if f["name"].startswith("CVE-") and f["type"]=="file"}
    require(current==blobs,"OpenSSL CNA catalogue changed since independent assessment")


def cve_url(identifier):
    require(re.fullmatch(r"CVE-\d{4}-\d+",identifier),"invalid reviewed CVE")
    _,year,number=identifier.split("-")
    return f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/{year}/{int(number)//1000}xxx/{identifier}.json"


def selected_profile(observation,device,inputs,groups):
    native=device.get("opcua_native",{})
    release=native.get("release_id")
    if not release or not observation:
        return False
    selector=observation.get("selector",{})
    name="sa02m-mqtt-opcua"
    launcher="/usr/local/libexec/sa02m-python-consumer"
    entry="opt/sa02m-mqtt-opcua/sa02m-mqtt-opcua-managed.py"
    config="/etc/sa02m-mqtt-opcua.conf"
    expected={"/"+entry:inputs["OPC reviewed "+entry],config:inputs["OPC reference configuration"],
              launcher:inputs["Python consumer launcher"],"/opt/sa02m-python-manager/launch.py":inputs["Python launch.py"],
              "/opt/sa02m-python-manager/consumers.json":inputs["Python consumers.json"]}
    if (observation.get("files")!=expected or selector!={"schema":1,"release_id":release,
            "manifest_sha256":native["manifest_sha256"],"groups":sorted(groups)}
            or observation.get("receipt_sha256")!=native["receipt_sha256"]):
        return False
    unit=observation.get("unit",{})
    command=unit.get("ExecStart","")
    if (unit.get("Id")!=name+".service" or unit.get("LoadState")!="loaded" or unit.get("User") not in ("","root")
            or command.count("argv[]=")!=1 or command.count("path=")!=1 or
            f"path={launcher} ;" not in command or f"argv[]={launcher} {name} -c {config} ;" not in command):
        return False
    if unit.get("ActiveState")=="inactive":
        return unit.get("SubState")=="dead" and unit.get("MainPID")=="0" and observation.get("argv")==[]
    interpreter=f"/opt/sa02m-python-runtimes/releases/{release}/opcua/bin/python"
    return (unit.get("ActiveState")=="active" and unit.get("SubState")=="running" and
            str(unit.get("MainPID","0")).isdigit() and int(unit["MainPID"])>0 and observation.get("argv")==
            [interpreter,"-I","-B","/opt/sa02m-python-manager/launch.py",name,"-c",config])


def evaluate(bundle,transport,inventory):
    review,blobs,scored=bundle
    gaps=[]
    try:
        verify_catalogue(transport,blobs)
    except (ValueError,KeyError,OSError) as exc:
        gaps.append(str(exc))
    native_ok=all(d.get("opcua_profile_matches") for d in inventory.devices.values()) and bool(inventory.devices)
    if not native_ok:
        gaps.append("matching selected runtime, OPC UA entry and configuration unavailable on one or more release targets")
    catalog_ok=not gaps
    findings=[]
    for row in review["records"]:
        identifier=row["id"]
        scores=scored[identifier]
        url=cve_url(identifier)
        require(url==scores["url"],"unrecognized CVE Record source")
        current=False
        try:
            record=transport.fetch(url)
            key=hashlib.sha256(canonical([url,None])).hexdigest()
            require(record["cveMetadata"]["cveId"]==identifier and transport.seen[key]["sha256"]==scores["sha256"],
                    identifier+": external record changed since independent assessment")
            current=True
        except (ValueError,KeyError,OSError) as exc:
            gaps.append(identifier+": "+str(exc))
        levels={row["upstream_severity"].upper()}
        if row["external_severity"]:
            levels.add({"MEDIUM":"MODERATE"}.get(row["external_severity"],row["external_severity"]))
        disposition=row["status"] if current and catalog_ok else "under_investigation"
        require(disposition in {"not_affected","affected","under_investigation"},"invalid independent disposition")
        if disposition=="under_investigation":
            gaps.append(identifier+": applicability unresolved")
        findings.append({"identifiers":[identifier],"package":"OpenSSL","version":review["artifact"]["openssl_version"],
            "levels":sorted(levels),"external_cvss_v3_1":row["external_cvss_v3_1"],"applicability":disposition,
            "review_sha256":inventory.inputs["independent OpenSSL review"],"locations":["opcua:sa02m-mqtt-opcua"],
            "prerequisite":row["prerequisite"],"invalidate_if":row["invalidate_if"]})
    return findings,gaps


def homekit(component,archive_path,expected_archive,transport,range_matches,cvss):
    """Only the provider's semver ranges go through upstream semver; preserve other grammars as gaps."""
    require(sha(archive_path)==expected_archive,"unregistered upstream OpenSSL archive")
    blobs={}
    ranges={}
    other=[]
    cna_digests={}
    with tarfile.open(archive_path) as archive:
        for member in archive:
            if not re.fullmatch(r"[^/]+/secjson/CVE-\d{4}-\d+\.json",member.name):
                continue
            raw=archive.extractfile(member).read()
            filename=member.name.rsplit("/",1)[1]
            blobs[filename]=hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
            identifier=filename[:-5]
            cna_digests[identifier]=hashlib.sha256(raw).hexdigest()
            cna=json.loads(raw)["containers"]["cna"]
            for product in cna["affected"]:
                if product["product"].lower()!="openssl":continue
                require(product.get("defaultStatus")=="unaffected","unknown OpenSSL default")
                for row in product["versions"]:
                    if row.get("versionType")!="semver" or row["status"]!="affected" or row.get("changes"):
                        other.append({"id":identifier,"range":row})
                        continue
                    bounds=[b for b in ("lessThan","lessThanOrEqual") if b in row]
                    require(len(bounds)<=1,"ambiguous OpenSSL bounds")
                    expr="="+row["version"]
                    if bounds:
                        b=bounds[0]
                        expr=">="+row["version"]+" "+("<" if b=="lessThan" else "<=")+row[b]
                    ranges[str(len(ranges))]={"id":identifier,"expression":expr,"range":row,
                        "vendor":[m["other"]["content"]["text"].strip().upper() for m in cna.get("metrics",[]) if "other" in m]}
    require(blobs and ranges,"empty OpenSSL catalogue")
    verify_catalogue(transport,blobs)
    version=component["version"]
    cases=[{"id":key,"vulnerable_versions":row["expression"]} for key,row in ranges.items()]
    matches=range_matches(cases,version)
    selected=[ranges[key] for (_,v,key),hit in matches.items() if hit]
    gaps=[f"{len(other)} upstream custom or non-semver OpenSSL ranges require provider assessment"] if other else []
    def join(row):
        identifier=row["id"]
        levels=set(row["vendor"])
        notes=[]
        try:
            record=transport.fetch(cve_url(identifier))
            require(record["cveMetadata"]["cveId"]==identifier,"incorrect CVE Record identity")
            for provider in [record["containers"]["cna"],*record["containers"].get("adp",[])]:
                for metric in provider.get("metrics",[]):
                    for key,value in metric.items():
                        if key.startswith("cvss"):
                            parser={"2.0":cvss.CVSS2,"3.0":cvss.CVSS3,"3.1":cvss.CVSS3,"4.0":cvss.CVSS4}[value["version"]]
                            levels.add(parser(value["vectorString"]).severities()[0].upper())
        except (OSError,KeyError,ValueError) as exc:
            notes.append(identifier+": "+str(exc))
        levels=[{"MEDIUM":"MODERATE"}.get(v,v) for v in levels] or ["UNKNOWN"]
        return {"identifiers":[identifier],"levels":sorted(set(levels)),"package":"OpenSSL","version":version,
                "locations":["HomeKit lock:"+component["wheel"]+"!"+component["elf"]],"range":row["range"],
                "cna_sha256":cna_digests[identifier],"applicability":"under_investigation"},notes
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes=list(pool.map(join,selected))
    for _,notes in outcomes:gaps.extend(notes)
    return {"findings":[r for r,_ in outcomes],"gaps":gaps,"uncategorized_ranges":other,
            "compared_semver_ranges":len(ranges),"catalogue_records":len(blobs),"component":component}
