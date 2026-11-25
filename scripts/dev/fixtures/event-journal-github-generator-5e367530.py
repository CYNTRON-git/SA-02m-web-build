import json, os, re
from pathlib import Path

overlay = Path(os.environ["OVERLAY"])
meta = Path(os.environ["META"])
meta.mkdir(parents=True, exist_ok=True)

# One of four copies of the destination allow-list (runner x2, packer, validator);
# the ota-dst-allowlist-parity row keeps them equal - change all four together.
DST_RE = re.compile(
    r"^/(var/www/network_config/|usr/local/(sbin|lib|libexec)/|"
    r"opt/sa02m-[a-z0-9-]+/|opt/mplc4/(mplc_cyntron|mplc_protocol_fast_modbus)\.so$|"
    r"etc/systemd/system/sa02m-|"
    r"etc/nginx/|etc/tmpfiles\.d/|etc/sudoers\.d/|"
    r"etc/default/sa02m-|"
    r"etc/sa02m-update/trusted-keys/|"
    r"usr/local/bin/fix-eth\.sh$|"
    r"etc/dhcp/dhclient-exit-hooks\.d/eth1-default-route$)"
)
# Git-tracked MPLC plugins (firmware/mplc4/) → /opt/mplc4/<name>. Closed set —
# never map arbitrary firmware/* into the live RT tree.
MPLC_OTA_PLUGINS = frozenset({
    "mplc_cyntron.so",
    "mplc_protocol_fast_modbus.so",
})

def map_dst(rel: str):
    rel = rel.replace("\\", "/").lstrip("./")
    if rel == "etc/sa02m-update-state-migrate.sh":
        return None  # Checkout-only installer preflight; not a runtime helper.
    if rel.startswith("www/network_config/"):
        # rel is www/network_config/... → /var/www/network_config/...
        return "/var/" + rel
    if rel.startswith("opt/sa02m-"):
        return "/" + rel
    if rel.startswith("usr/local/"):
        return "/" + rel
    if rel.startswith("etc/sa02m-update/trusted-keys/"):
        return "/" + rel
    if rel.startswith("etc/tmpfiles.d/"):
        return "/" + rel
    if rel.startswith("etc/sudoers.d/"):
        return "/" + rel
    if rel.startswith("etc/systemd/system/") and "sa02m-" in rel:
        return "/" + rel
    if rel.startswith("etc/systemd/") and "sa02m-" in Path(rel).name:
        return "/etc/systemd/system/" + Path(rel).name
    name = Path(rel).name
    # MPLC RT plugins (licence publisher + Fast Modbus) — otherwise web OTA
    # never refreshes them and the licence card falls back to a log scrape.
    if rel.startswith("firmware/mplc4/") and name in MPLC_OTA_PLUGINS:
        return "/opt/mplc4/" + name
    # The static-IP watchdog (02-network.sh installs it to /usr/local/bin) —
    # the one exact /usr/local/bin path the allow-list admits (1.0.7.19); the
    # 1.0.7.12 late-cable fix reached boards only by install.sh without it.
    # Gate: fix-eth-ota-delivery.
    if rel == "etc/fix-eth.sh":
        return "/usr/local/bin/fix-eth.sh"
    # Helpers etc/sa02m-*.sh → /usr/local/sbin or lib
    if rel.startswith("etc/") and name.startswith("sa02m-"):
        if name.endswith("-lib.sh") or name in ("sa02m-web-build-lib.sh", "sa02m-web-auth-lib.sh", "sa02m-stacks-policy.sh"):
            return "/usr/local/lib/" + name
        # Privileged helpers sudo grants by an explicit path must land at THAT
        # path — a stripped extension deploys a twin nobody calls and leaves the
        # granted file stale (audit B1 deploy gap). Gate: sudoers-pin-contract.
        if name == "sa02m-mqtt-external-info.py":
            return "/usr/local/sbin/" + name
        if name.endswith(".sh") or name in (
            "sa02m-web-update-check", "sa02m-web-update-apply",
            "sa02m-set-cpu-profile", "sa02m-set-storage-auto-format",
        ):
            base = name[:-3] if name.endswith(".sh") and name not in (
                "sa02m-web-reboot.sh", "sa02m-web-restart-services.sh",
                "sa02m-web-service-ctl.sh", "sa02m-web-root-cmd.sh",
                "sa02m-rs485-stats.sh", "sa02m-kernel-select.sh",
                "sa02m-cpu-profile.sh", "sa02m-web-backup.sh",
                "sa02m-restore-backup.sh",
            ) else name
            # Keep .sh for scripts that install with extension
            if name in (
                "sa02m-web-reboot.sh", "sa02m-web-restart-services.sh",
                "sa02m-web-service-ctl.sh", "sa02m-web-root-cmd.sh",
                "sa02m-rs485-stats.sh", "sa02m-kernel-select.sh",
                "sa02m-cpu-profile.sh", "sa02m-web-backup.sh",
                "sa02m-restore-backup.sh", "sa02m-update-runner.sh",
                "sa02m-update-inspect.sh", "sa02m-update-control.sh", "sa02m-factory-reset-runner.sh",
                "sa02m-iface-conf-write.sh", "sa02m-usb-power.sh",
                "sa02m-ensure-eth1-dhcp-hook.sh",
                "sa02m-conf-rm.sh", "sa02m-mplc-project-deploy.sh",
            ):
                if "runner" in name or "inspect" in name or "factory-reset" in name or name == "sa02m-update-control.sh":
                    return "/usr/local/libexec/" + name.replace(".sh", "")
                return "/usr/local/sbin/" + name
            return "/usr/local/sbin/" + base
    # eth1 DHCP default-route exit-hook (panel/installer parity on OTA boards)
    if rel == "etc/dhcp/dhclient-exit-hooks.d/eth1-default-route":
        return "/etc/dhcp/dhclient-exit-hooks.d/eth1-default-route"
    # Template, not a live site. render_nginx_template() substitutes __PORT__
    # and __WEB_ROOT__ in the staged copy before this path is packed.
    if rel == "etc/nginx/network_config.conf":
        return "/etc/nginx/sites-available/network_config"
    return None

def render_nginx_template(overlay: Path) -> None:
    """Substitute the nginx template in the staged overlay.

    The repo file is a template. A raw copy would put __PORT__ into the live
    site and nginx -t would reject it. Port and root come from the site already
    on the board; a board with no site yet gets the installer defaults.
    """
    src = overlay / "etc/nginx/network_config.conf"
    if not src.is_file():
        return
    text = src.read_text(encoding="utf-8")
    if "__PORT__" not in text and "__WEB_ROOT__" not in text:
        return
    port, web_root = "9999", "/var/www/network_config"
    live = os.environ.get("SA02M_NGINX_SITE") or "/etc/nginx/sites-available/network_config"
    try:
        live_text = Path(live).read_text(encoding="utf-8", errors="replace")
    except OSError:
        live_text = ""
    found = re.search(r"(?m)^\s*listen\s+(\d{2,5})\s+default_server\s*;", live_text)
    if found:
        n = int(found.group(1))
        if 1 <= n <= 65535:
            port = found.group(1)
    found = re.search(r"(?m)^\s*root\s+(/[A-Za-z0-9_./-]*)\s*;", live_text)
    if found and ".." not in found.group(1).split("/"):
        web_root = found.group(1).rstrip("/") or found.group(1)
    src.write_text(
        text.replace("__PORT__", port).replace("__WEB_ROOT__", web_root),
        encoding="utf-8", newline="\n")

def deploy_mode(rel: str, dst: str) -> str:
    # Mode follows DESTINATION role, not source extension. Extension-less helpers
    # (sa02m-set-storage-auto-format, sa02m-set-cpu-profile) land in
    # /usr/local/sbin and must stay executable — see private/BUG-ota-mode-0644.md.
    if dst.startswith("/usr/local/sbin/") or dst.startswith("/usr/local/libexec/"):
        return "0755"
    if dst.startswith("/usr/local/bin/"):
        return "0755"
    if dst.startswith("/etc/dhcp/dhclient-exit-hooks.d/"):
        return "0755"
    if rel.endswith(".cgi") or rel.endswith(".sh"):
        return "0755"
    if dst.startswith("/opt/mplc4/") and rel.endswith(".so"):
        return "0755"
    return "0644"

deploy = []
# After `deploy = []`: ota-deploy-mode-contract extracts deploy_mode up to that
# line and execs the slice. A call above it runs during the extract.
render_nginx_template(overlay)
for p in overlay.rglob("*"):
    if not p.is_file():
        continue
    rel = p.relative_to(overlay).as_posix()
    if "__pycache__" in rel or rel.endswith(".pyc"):
        continue
    dst = map_dst(rel)
    if not dst or not DST_RE.match(dst):
        continue
    mode = deploy_mode(rel, dst)
    owner = "www-data:www-data" if dst.startswith("/var/www/") else "root:root"
    deploy.append({"src": rel, "dst": dst, "mode": mode, "owner": owner})

ver = os.environ.get("TARGET_VER") or ""
if not ver:
    vf = overlay / "www/network_config/VERSION"
    if vf.is_file():
        for line in vf.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and re.match(r"^\d+(\.\d+){2,3}$", s):
                ver = s
                break
commit = os.environ.get("TARGET_COMMIT") or ("0" * 40)
manifest = {
    "schema_version": 1,
    "product": "SA-02m",
    "model": "A40i",
    "arch": "armv7l",
    "version": ver or "0.0.0.0",
    "repo_commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ("0" * 40),
    "built_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    "signing_key_id": "github-unsigned",
    "min_updater": "1.0.7.5",
    "min_version": "1.0.5.60",
    "payload": {"size": 0, "sha256": "0" * 64, "uncompressed_size_max": 1},
    "preflight": {
        "commands": ["/bin/bash", "/usr/bin/python3", "/usr/bin/install", "/usr/bin/systemctl"],
        "free_bytes_min": 67108864,
        "free_bytes_multiplier": 3,
    },
    "deploy": deploy,
    "services": {
        "daemon_reload": True,
        "stop_before_apply": ["sa02m-flasher"],
        "enable": [
            "sa02m-devices-api.service",
            "sa02m-devices-logger.service",
            # Boot-time DNS belt (docs/contracts/boot-network-dns.md). Deploying
            # the unit file is NOT enough: without an entry here no
            # multi-user.target.wants symlink is ever created and the unit stays
            # inert forever. Enable only, never restart[]: it is a Type=oneshot
            # whose job is the NEXT boot, and restart[] feeds the health check.
            "sa02m-dns-ensure.service",
        ],
        "restart": [
            "fcgiwrap",
            "nginx",
            "sa02m-flasher",
            "sa02m-devices-api",
            "sa02m-devices-logger",
            # Reload MPLC plugins after OTA (mplc_cyntron.so publishes licence).
            # Masked/disabled units are skipped by the health loop; restart uses
            # the same soft || true path as other optional services.
            "mplc4",
            # Telemetry owns its own device id and its legacy-retained clear
            # (1.0.6.22), so the new .py on disk changes nothing until the
            # process restarts. Safe here: it touches /proc, /sys and i2c bus 2
            # only, never an RS-485 port — unlike sa02m-modbus-mqtt, which
            # stays OUT of this list under the port-lease invariant.
            # HONEST LIMIT: this manifest is built by the runner ALREADY on the
            # board, so the entry first bites on the update AFTER the one that
            # delivers it. Landing 1.0.6.22 over the web leaves the old process
            # running until a reboot or `systemctl restart sa02m-telemetry`.
            # Must stay in step with scripts/pack-offline-update.py.
            "sa02m-telemetry",
            # Scenario engine holds /opt/sa02m-rules in memory: an OTA that
            # delivers new rules code must bounce it, or scenarios keep running
            # the stale engine until reboot (1.0.6.37 bench incident class:
            # cloud scenario push silently stripped trigger/end on stale code).
            # Core unit — enabled+started by 06b-rules.sh on every full
            # install, so the restart||start semantics of this list fits (same
            # as sa02m-telemetry). Must stay in step with
            # scripts/pack-offline-update.py.
            "sa02m-rules",
        ],
        # Conditional restarts (never-widen). `systemctl restart` on an INACTIVE
        # unit STARTS it, so opt-in units must never go into restart[]: the
        # Alice family ships disabled by default (scripts/06-alice.sh `app off`)
        # and an OTA must not turn the board's Alice/cloud profile on. All three
        # hold the scenario channel code in memory (sa02m_alice +
        # sa02m_rules.store from /opt/sa02m-rules) — without the restart a
        # deploy of new /opt code leaves them stale (the 1.0.6.37 incident).
        # HONEST LIMIT (same as sa02m-telemetry above): built by the on-board
        # runner, so these entries first bite on the update AFTER the one that
        # delivers this runner. Must stay in step with
        # scripts/pack-offline-update.py.
        "restart_if_active": [
            "sa02m-alice-client",
            "sa02m-alice-config",
            "sa02m-cloud-control",
            # HomeKit bridge: optional module, installed stopped/disabled
            # (scripts/06c-homekit.sh `app off`); it holds /opt/sa02m-homekit
            # and the Alice registry code in memory.
            "sa02m-homekit",
            # Home Connect client: optional module, installed stopped/disabled
            # (scripts/06d-homeconnect.sh `app off`); it holds
            # /opt/sa02m-homeconnect in memory.
            "sa02m-homeconnect",
            # Audio recorder: on every board, installed stopped/disabled
            # (scripts/15-audio.sh `app off`); it holds /opt/sa02m-audio in
            # memory.
            "sa02m-audio",
            # Agent API is off until the panel card enables it (13-agent-api.sh
            # `app off`). Bounce only a running daemon so new /opt code is not
            # left in memory, and never start one the operator left off.
            "sa02m-agent-api",
            # The two health daemons are long-lived bash loops: a replaced
            # script changes nothing until the process restarts (the
            # 1.0.7.36 heal-only watchdog stayed old on an upgraded board,
            # D36-02). Always on (infra units), so restart-if-active is the
            # right rule; restart_if_changed cannot name them, its validator
            # takes /opt-style directory prefixes only. A bounce just resets
            # their failure counters. Must stay in step with
            # scripts/pack-offline-update.py.
            "sa02m-userspace-watchdog",
            "sa02m-failure-monitor",
        ],
        # Change-gated conditional restart: unit -> /opt prefix watched in the
        # apply journal. sa02m-modbus-mqtt owns the RS-485 port lease: restart
        # it only when the bridge code actually changed (a www-only patch must
        # not bounce industrial polling) and only when it is already running —
        # a stopped bridge may be stopped FOR a flasher lease, never start it
        # (the port-lease invariant that keeps it out of restart[] above).
        # Must stay in step with scripts/pack-offline-update.py.
        "restart_if_changed": {
            "sa02m-modbus-mqtt": "/opt/sa02m-modbus-mqtt/",
        },
        "health": {
            "http_url": "http://127.0.0.1:9999/login.html",
            "units_active": ["nginx", "fcgiwrap", "sa02m-devices-api"],
            "version_file": "/var/www/network_config/VERSION",
        },
    },
    "delete": [],
    "migrations": [],
}
(meta / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
(meta / "signature_ok").write_text("0\n", encoding="utf-8")
print("OK github overlay deploy=%d version=%s" % (len(deploy), ver))