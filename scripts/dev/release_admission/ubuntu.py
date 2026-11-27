"""Correlate official Ubuntu source publication, affected ranges and Noble tracker status."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import time

from .providers import Transport, official_sources, osv_matches, osv_records, require, sha


def sources(targets):
    devices = json.loads(targets.read_bytes())["devices"]
    inputs = {}
    packages = {}
    for device, row in sorted(devices.items()):
        path = targets.parent / row["inventory"]
        inputs[device] = sha(path)
        data = json.loads(path.read_bytes())["dpkg"]
        require(data["rc"] == 0, "dpkg unavailable on " + device)
        for line in data["stdout"].splitlines():
            state, binary, version, arch, source, revision = line.split("\t")
            if state == "installed":
                key = ("Ubuntu:24.04:LTS", source, revision)
                packages.setdefault(key, []).append(f"{device}:{binary}={version}/{arch}")
    require(packages, "empty dpkg coverage")
    return inputs, packages


def upstream(record):
    ids = [record["id"], *record.get("upstream", [])]
    return {x.removeprefix("UBUNTU-") for x in ids if re.fullmatch(r"(UBUNTU-)?CVE-[0-9]{4}-[0-9]+", x)}


def tracker_row(name, cve, record):
    require(record["id"] == cve, "incorrect Ubuntu tracker identity")
    rows = [s for p in record["packages"] if p["name"] == name
            for s in p["statuses"] if s["release_codename"] == "noble"]
    if not rows:
        return None
    require(len(rows) == 1, "ambiguous Ubuntu tracker Noble status")
    priority = record["priority"].upper()
    priority = {"MEDIUM":"MODERATE", "NEGLIGIBLE":"INFO"}.get(priority, priority)
    require(priority in {"INFO","LOW","MODERATE","HIGH","CRITICAL"}, "unknown Ubuntu priority")
    impact = {v.get("baseSeverity", "").upper() for metric in (record.get("impact") or {}).values()
              if isinstance(metric, dict) for v in metric.values() if isinstance(v, dict) and v.get("baseSeverity")}
    return {"status":rows[0]["status"], "description":rows[0].get("description",""),
            "pocket":rows[0].get("pocket"), "priority":priority, "upstream_levels":sorted(impact)}


def collect(targets, net):
    inputs, packages = sources(targets)
    origins = official_sources({(n,v) for _,n,v in packages}, net)
    gaps = [f"{n}={v}: Noble archive origin " + ("provider unavailable" if ok is None else "unverified")
            for (n,v),ok in origins.items() if not ok]
    keys = {k for k in packages if origins[k[1:]]}
    hits = osv_matches(keys, net) if keys else {}
    missing = {}
    records = osv_records(hits, net, missing)
    gaps.extend(identifier+": OSV record unavailable" for identifier in missing)
    withdrawn=sorted(k for k,r in records.items() if r.get("withdrawn"))
    cves = sorted(set().union(*(upstream(r) for r in records.values() if not r.get("withdrawn"))))
    def fetch(cve):
        try:
            return cve, net.fetch("https://ubuntu.com/security/cves/" + cve + ".json")
        except (OSError, ValueError) as exc:
            return cve, {"unavailable":str(exc)}
    with ThreadPoolExecutor(max_workers=6) as pool:
        tracker = dict(pool.map(fetch, cves))
    findings = []
    for key, found in hits.items():
        eco,name,version = key
        grouped = {}
        for identifier in sorted(found):
            if identifier in missing or identifier in withdrawn:
                continue
            rec = records[identifier]
            ids = upstream(rec)
            if not ids:
                gaps.append(f"{identifier} {name}: no upstream CVE identity")
            for cve in sorted(ids):
                grouped.setdefault(cve, []).append(identifier)
        for cve, identifiers in sorted(grouped.items()):
            obj = tracker[cve]
            if "unavailable" in obj:
                gaps.append(f"{cve} {name}: tracker unavailable")
                continue
            try:
                row = tracker_row(name,cve,obj)
            except (KeyError, ValueError, AttributeError, TypeError):
                gaps.append(f"{cve} {name}: invalid Ubuntu status or priority")
                continue
            direct = "UBUNTU-" + cve in identifiers
            if not row:
                if direct:
                    gaps.append(f"{cve} {name}: missing Noble package status")
                continue
            status = row["status"]
            fixed = sorted({event["fixed"] for identifier in identifiers
                            for a in records[identifier].get("affected",[])
                            if (a["package"]["ecosystem"],a["package"]["name"])==(eco,name)
                            for rng in a.get("ranges",[]) for event in rng["events"] if "fixed" in event})
            # A USN can fix many packages and CVEs at once. Only a direct
            # package match or a tracker-confirmed identical fix relates it to this version.
            if not direct and (status != "released" or row["description"] not in fixed):
                continue
            applicable = status in {"needed","ignored","deferred"} or (status=="released" and row["description"] in fixed)
            if not applicable:
                gaps.append(f"{cve} {name}={version}: Ubuntu {status} requires reconciliation")
            if applicable and row["priority"] not in {"HIGH","CRITICAL"} and set(row["upstream_levels"]) & {"HIGH","CRITICAL"}:
                gaps.append(f"{cve} {name}: Ubuntu priority and upstream severity cross the blocking threshold")
            findings.append({"ecosystem":eco,"package":name,"version":version,
                "identifiers":[cve,*identifiers],"fixed":fixed, "ubuntu":row,
                "levels":[row["priority"]] if applicable else [],
                "locations":sorted(packages[key])})
    return {"inputs":inputs,"coverage":len(packages),"withdrawn":withdrawn,
            "findings":findings,"gaps":sorted(set(gaps))}


def capture(targets, folder):
    folder.mkdir(exist_ok=False)
    started = time.time()
    net = Transport(folder/"provider")
    try:
        result = collect(targets,net)
    except Exception as exc:
        (folder/"partial.json").write_text(json.dumps({"started":started,"responses":net.seen,"error":str(exc)}))
        raise
    (folder/"ubuntu.json").write_text(json.dumps({"schema":1,"started":started,"responses":net.seen,
                                      "result":result},sort_keys=True) + "\n")
    return result
