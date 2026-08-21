# MPLC4 plugins shipped by this repo

Two prebuilt ARM shared objects the installer drops into `/opt/mplc4`:

| file | size / md5 | what it is |
|---|---|---|
| `mplc_cyntron.so` | 186356 B · `f6ae6026` | CYNTRON addin — Linux diagnostics as MPLC channels, PCA9536 outputs, i2c lock, beeper override, and (since 1.0.6.5) the runtime licence published to `/run/sa02m-mplc-license.json` |
| `mplc_protocol_fast_modbus.so` | 226276 B · `9eba65e3` | Fast Modbus protocol plugin |

**This directory is authoritative.** `scripts/09-mplc.sh` installs these over
`/opt/mplc4/<name>.so` whenever the bytes differ, preferring them to the vendor
payload — so a stale binary here silently downgrades every device the installer
touches, and the licence card falls back to «—». Update the file here in the
same change that deploys a new plugin — and with it EVERY place that records a
fingerprint, or the acceptance check starts passing stale drivers:

1. the table above;
2. `docs/deployment.md` — the post-install acceptance check;
3. `docs/vendor-integrations.md` — the "not in the vendor drop" note;
4. `MPLC4/README.md` — the staging table;
5. `docs/mplc-driver-build.md` §8 — the shipped-build fingerprint.

## Building these

**Do not improvise a build from this file.** The one home for the recipe — the
vendor toolchain, the device-sourced SDK `.so` prerequisites, the mandatory ABI
gate (a generic-toolchain build looks fine and bricks the RT), the known
`makedrv.sh` traps, and what each featured layer contains — is
**`docs/mplc-driver-build.md`**; §8 describes exactly what the shipped
`mplc_cyntron.so` carries, including the licence publishing that a naive
vendor-baseline rebuild would drop.

Sources live in separate repositories, one per plugin — `mplc_cyntron.so` in
`PCA9536-driver-for-MasterPLC`, `mplc_protocol_fast_modbus.so` in
`fast_modbus_MasterSCADA4D_driver` (both declared in `.ai-dev/components.json`).
Only the built binaries are vendored here.

Consumers and the fail-safe order for the licence file:
`docs/contracts/mplc-project-deploy.md`. Runtime API background:
`docs/agent-rules/mplc4-api.md`.
