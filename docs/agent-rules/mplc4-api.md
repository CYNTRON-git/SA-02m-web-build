# MPLC4 runtime API — what is reachable, how, and the traps

Project-local map of the MPLC4 runtime's own interfaces as they exist on an
SA-02m board: the HTTP/FastCGI method API, the license API, and the two ways
the web layer can actually read a license. POINTERS ONLY — the web-side contract
stays in `docs/contracts/mplc-project-deploy.md`, never restated here.
Machine-facing: English (`PROTOCOL.md` invariant 5).

**Not `@`-imported by `CLAUDE.md`** — read on demand via the pointer row in
`agent-tooling-map.md`. Every fact below was measured on a real board
(runtime **1.3.10.34421**, bench 192.168.1.136, license 413850 · 100 points ·
1 client · 1 instance) during the 1.0.6.4 session — except the `FeatureParameter`
ids below, which come from the SDK header `core/main_imp.h` and carry that source
where they are stated. The two encoded fields we did NOT decode are named as such.

---

## The HTTP method API — and the standing trap

```
http://<ip>/Methods/<Method>        →  MPLC4's OWN nginx (port 80)  →  FastCGI  →  runtime
```

- Port **80 belongs to MPLC4's nginx**, not to the SA-02m panel (the panel is on
  **9999**). The two web stacks coexist on the board.
- The runtime speaks **FastCGI, not HTTP**, on `Main` (30750+) and
  `fcgi_backend_N` (30751…). **The trap:** a plain HTTP GET straight at
  30550/30750 answers *nothing* — it is not a dead port and not a firewall
  problem, it is the wrong protocol. Always go through `/Methods/` on port 80.
- **With no project deployed every method returns**
  `{"code":2149908480,"hex":"0x80250000"}`. That code means **«no project»**, NOT
  «unknown method» — do not use it to conclude a method does not exist.

Method names are not documented; they live as strings in the runtime's addin
`.so` files:

```sh
strings <addin>.so | grep -E '^RT[A-Z]'
```

Known set: `RTVersion`, `RTCPULoad`, `RTThreads`, `RTConfigID`, `RTDescriptors`,
plus ~45 `RTUsers*` methods.

## The license API (SDK `core/main_imp.h`)

Exported by `masterplc.so` and callable **from an addin**:

| Call | Returns |
|---|---|
| `GetFeatureParameter(fp)` | one `FeatureParameter` value (table below) |
| `GetFeaturesJSONData()` | the whole feature set as JSON |
| `GetProtectInfo()` | the protection/licence block |
| `GetRTVersion()` | runtime version |

`FeatureParameter` ids that matter:

| id | name | meaning |
|---|---|---|
| 1 | `fpSessionsLimit` | clients (клиенты) |
| 2 | `fpPLCConnectionsLimit` | **points (точки)** |
| 3 | `fpLicNumber` | licence number |
| 4 | `fpInstancesLimit` | instances (экземпляры) |
| 6 | `fpAllowedVersionDate` | encoded — see the honesty note |
| 8 | `fpBasePlatformType` | encoded — see the honesty note |

**точки = `fpPLCConnectionsLimit` (2), never `fpInstancesLimit` (4).** Mapping
точки ← `InstancesLimit` shipped through 1.0.6.3 and is FIXED in 1.0.6.4: on the
bench licence the old mapping printed 1 instead of 100.

**Where this runs:** the CYNTRON addin `mplc_cyntron.so` is loaded by the runtime
as **addin #35 at startup, with NO project deployed** — so an addin can publish
the licence at init, which is exactly what the log-free source below relies on.

## Two sources the web layer can read

| # | Source | State |
|---|---|---|
| 1 | `/run/sa02m-mplc-license.json` (tmpfs, written by the addin at runtime start) | primary; the addin ships from the **driver repo**, separately from this repo |
| 2 | `<Protect>` block in `/var/log/mplc4/0/<YYYY_MM_DD>.txt` | fallback; gated by `WriteLogsToHost` |

One home each: the enum ids above are SDK truth and live here; the JSON shape,
the producer's obligations, and the fail-safe source order belong to the web
contract — `docs/contracts/mplc-project-deploy.md ## Лицензия рантайма MPLC4`.

**Host logs are a WORKING option, currently OFF by Operator decision.** Measured,
not assumed: enabling `WriteLogsToHost` in
`/opt/mplc4/default_monitor_config.json` makes the `<Protect>` block appear at
**~1 KB/min**, bounded by `HostLogFilesCount` / `HostLogFileSizeInMb` (verified
accepted at **2 × 5 MB**), and it yields the licence **with no project deployed**.
The Operator chose to keep logging off — which is why source 1 exists. Do not
re-litigate this; if logs are wanted again, it is a one-key config change.

## Rejected: a standalone ctypes licence reader

Tried and **rejected in 1.0.6.4** — do not re-run this experiment:

- `dlopen` works: 105/110 libs preload, `masterplc.so` loads, the calls succeed.
- But **`InitProtect()` called with no arguments leaves the key unread** —
  `LicNumber=0` and every limit at its demo default.
- The real signature is **undocumented internal**; guessing it is not a shippable
  foundation. The addin (which the runtime itself initialises) is.

## Honesty note — what is NOT decoded

`fpAllowedVersionDate` (6) and `fpBasePlatformType` (8) come back **encoded**.
This session did **not** decode them, and nothing in this repo interprets them.
Treat any claim about their meaning as unverified until someone decodes them on
hardware and records it here.

## Verification recipes

```sh
# runtime version through the method API (the ONLY correct transport)
curl -s http://<ip>/Methods/RTVersion

# the trap, for contrast: answers nothing — FastCGI, not HTTP
curl -s --max-time 3 http://<ip>:30750/

# licence in the host log (only when WriteLogsToHost is on)
grep -n 'LicNumber=\|Limit=\|Not activated' /var/log/mplc4/0/*.txt | head

# is the host-log gate on?
grep -n 'WriteLogsToHost\|HostLogFile' /opt/mplc4/default_monitor_config.json

# the log-free source, if the addin is deployed
cat /run/sa02m-mplc-license.json

# what the panel actually serves (session cookie required)
curl -s --cookie 'session_token=<tok>' \
  'http://<ip>:9999/cgi-bin/mplc_project_deploy.cgi'
```

Probing the last one: it is the **Bash CGI layer**, so it answers HTTP 200 with
`ok:false` on failure — `curl -f` cannot fail there, only a body assertion can
(the rule and its two-layer rationale: `web-code-rigor.md ## Bash CGI floors`).
