"""Evaluate the exact upstream Node.js core catalogue with upstream semver."""
import re
from .providers import require


def on_linux(record):
    environments=record.get("affectedEnvironments")
    known={"aix","all","android","darwin","freebsd","linux","openbsd","sunos","win32"}
    require(isinstance(environments,list) and environments and set(environments)<=known,"unknown Node advisory platform")
    return bool({"linux","all"}.intersection(environments))


def check(version, artifact, tool_dir, work, node, policy, transport):
    base="https://api.github.com/repos/nodejs/security-wg"
    head=transport.fetch(base+"/commits?path=vuln/core&per_page=1")
    commit=head[0]["sha"]
    require(re.fullmatch("[0-9a-f]{40}",commit),"invalid Node security catalogue commit")
    listing=transport.fetch(base+"/contents/vuln/core?ref="+commit)
    ids={row["name"].removesuffix(".json") for row in listing if re.fullmatch(r"[0-9]+\.json",row["name"])}
    index=transport.fetch("https://raw.githubusercontent.com/nodejs/security-wg/"+commit+"/vuln/core/index.json")
    require(ids==set(index) and ids,"incomplete Node security catalogue")
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
    return {"version":version,"catalogue_commit":commit,"evaluated":len(ids),"findings":findings,
            "other_platforms":other_platforms}
