"""Evaluate the exact upstream Node.js core catalogue with upstream semver."""
from concurrent.futures import ThreadPoolExecutor
import re
from .providers import require


def on_linux(record):
    environments=record.get("affectedEnvironments")
    known={"aix","all","android","darwin","freebsd","linux","openbsd","sunos","win32"}
    require(isinstance(environments,list) and environments and set(environments)<=known,"unknown Node advisory platform")
    return bool({"linux","all"}.intersection(environments))


def catalogue(transport):
    base = "https://api.github.com/repos/nodejs/security-wg"
    head = transport.fetch(base + "/commits?path=vuln/core&per_page=1")
    require(isinstance(head, list) and len(head) == 1, "invalid Node catalogue head")
    commit = head[0]["sha"]
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "invalid Node catalogue commit")
    listing = transport.fetch(base + "/contents/vuln/core?ref=" + commit)
    # GitHub's Contents API caps directory responses at 1,000 entries.
    require(isinstance(listing, list) and 0 < len(listing) < 1000, "incomplete Node advisory directory")
    members = {}
    for row in listing:
        name = row["name"]
        require(isinstance(name, str) and name not in members and row["type"] == "file"
                and row["path"] == "vuln/core/" + name
                and (name == "index.json" or re.fullmatch(r"[1-9][0-9]*\.json", name))
                and type(row["size"]) is int and 0 < row["size"] <= 8 * 1024 * 1024
                and re.fullmatch(r"[0-9a-f]{40}", row["sha"]), "invalid Node advisory directory member")
        members[name] = (row["size"], row["sha"])
    # The generated index can lag behind numbered upstream advisories.
    members.pop("index.json", None)
    require(members, "empty Node advisory directory")
    prefix = f"https://raw.githubusercontent.com/nodejs/security-wg/{commit}/vuln/core/"

    def fetch(name):
        record = transport.fetch(prefix + name, git_blob=members[name])
        require(isinstance(record, dict) and isinstance(record.get("cve"), list)
                and all(isinstance(alias, str) and re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", alias)
                        for alias in record["cve"])
                and record.get("severity") in ("unknown", "low", "medium", "high", "critical"),
                "invalid Node advisory " + name)
        on_linux(record)
        return name.removesuffix(".json"), record

    with ThreadPoolExecutor(max_workers=6) as pool:
        requests = [pool.submit(fetch, name) for name in sorted(members)]
    return commit, dict(request.result() for request in requests)


def check(version, artifact, tool_dir, work, node, policy, transport):
    commit, index = catalogue(transport)
    rows=[]
    for identifier,record in sorted(index.items()):
        for key in ("vulnerable","patched"):
            require(isinstance(record[key],str) and record[key],"missing Node core range")
            rows.append({"id":identifier+"-"+key,"vulnerable_versions":record[key]})
    node.tools_verified(policy,tool_dir)
    module=node.semver_module(artifact,node.REPO,work,policy)
    matches=node.range_matches({"node":rows},{"node":[version]},tool_dir/"node",module,work)
    findings=[]
    other_platforms=[]
    for identifier,record in index.items():
        if matches["node",version,identifier+"-vulnerable"] and not matches["node",version,identifier+"-patched"]:
            if not on_linux(record):
                other_platforms.append({"id":"NSWG-COR-"+identifier,"environments":record["affectedEnvironments"]})
                continue
            sev=str(record.get("severity","unknown")).upper()
            findings.append({"id":"NSWG-COR-"+identifier, "aliases":record.get("cve",[]),
                             "severity":{"MEDIUM":"MODERATE"}.get(sev,sev),
                             "package":"node", "version":version, "patched":record["patched"]})
    return {"version":version,"catalogue_commit":commit,"evaluated":len(index),"findings":findings,
            "other_platforms":other_platforms}
