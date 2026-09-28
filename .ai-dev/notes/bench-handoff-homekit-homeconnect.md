# Bench hand-off — HomeKit + Home Connect (1.0.6.57)

For the local-network agent that deploys to the bench (192.168.1.135 — the
board actually used; the G procedures also name 192.168.1.136 and an SA-02m-2). The
cloud session built and reviewed everything that can run without hardware;
what follows needs a board, an iPhone or a BSH account.

## What is on the branch

`claude/home-connect-homekit-integration-4llog3` (version 1.0.6.57, stacked on
1.0.6.56): HomeKit bridge, Home Connect read-only client, and Phase 3
cross-feeding (Home Connect controls in «Умный дом», scenario recipes, CO₂
alarm threshold, buttons, thermostat setpoint, scenes in Apple Home). The
CHANGELOG 1.0.6.57 section is the user-facing summary.

## Deploy

Per `docs/deployment.md` (the `06c-homekit.sh` / `06d-homeconnect.sh`
modules) — never improvised. After deploy: `scripts/dev/verify-release-on-board.sh`
(recipe in `docs/agent-rules/web-diagnostic-tools.md`).

## Owed on the bench

Hardware gates — procedures and the result table live in
`docs/decisions/homekit-home-connect.md` (`## Сводка G1–G9`); record results there:

Done on 1.135 (2026-09-28, recorded there): G1, G2 (RSS/CPU), G3 on 1eth,
G4 pairing + control on the same Wi-Fi; G6 criterion 1 = BSH cloud silent from
an RU IP. Still owed:

- Re-deploy this branch with plain `06c-homekit.sh` on a board with an older
  Alice package: expect the peer refresh or the one-line «Обновите пакет
  Алисы» state, never a crash-loop.
- Bulk «Показывать в HomeKit» on the card: tick, save, devices appear in Home.
- Least privilege (never run on a board yet): after 06c/06d, neither
  daemon is in www-data (`id -nG`), the ACLs and setgid `/run` are in place
  (`getfacl`, recipe in `docs/deployment.md`), the HomeKit bridge still reads
  its conf and the Alice device document, the Home Connect client reads its
  conf only (a «Permission denied» on the Alice document for
  `sa02m-homeconnect` is correct), and the cards show state (not «Нет
  доступа к настройкам»).
- G2 pair-setup time at 149 accessories; G3 on SA-02m-2 (eth1); G4 via a home
  hub and item 8 (reset); G6 criterion 2 (SingleKey ID from RU — Operator).

Phase 3 items not verifiable off-board:

- iOS Home thermostat dial with a setpoint below 10 °C (HAP's default `minValue`;
  the bridge overrides it from the item range).
- BSH event order for recipe 2 (door open while running → alarm) —
  `docs/HOME_CONNECT_INTEGRATION.md` recipes.
- `sa02m-homekit` memory under `MemoryMax=64M` now that it imports
  `sa02m_rules.store` (desktop x86: maxrss 38.7 → 40.6 MiB).
- Alice device freshness while the Home Connect stream is up (heartbeat
  republish, contract `docs/contracts/home-connect.md`).
- Deploy-loop time and `/var/www` ownership on the hardpy stands
  (root-owned web root, side-fixes branch `claude/side-findings-fixes-4llog3`).

## For the next release (HomeKit v1.1: Carel Thermostat + Fanv2, extra temps, meter)

Build on this branch's final push — Phase 3 already moved the ground below.
Pointers into `opt/sa02m-homekit/sa02m_homekit/projection.py` (contract:
`docs/contracts/homekit-bridge.md` §3–§6):

- **Thermostat already exists — extend it, do not add a parallel row.** M17
  (`_thermostat_parts` → `_thermostat_service`) builds `Thermostat` from a
  `devices.types.thermostat` document: range `temperature` setpoint + measured
  temperature + optional heating `on_off`. Items it consumes produce no row of
  their own. A Carel Thermostat = teach `_thermostat_parts` the Carel document
  shape (or have the Alice document declare it); keep the setpoint clamp and the
  "out-of-range value not shown" rule.
- **Fan speed:** `on_off` on `devices.types.ventilation.fan` already yields
  `Fanv2`/`Active` (M05, `_on_off_service`); Carel `mode:fan_speed` is skipped
  today as `capability_unsupported`. Attach `RotationSpeed` to that Fanv2 the
  way `Brightness` is merged into a Lightbulb (`bulb_index` in
  `device_services`) — one accessory, no new aid.
- **Do not reorder services on an existing accessory.** Services are added in
  `device_services` order and pyhap numbers characteristics by add order;
  new services go AFTER the current ones (the `LATE_ROWS` stable sort; pinned
  by "порядок сервисов M15–M18 после v1" in §16). Add new row ids to the late
  group, never in front.
- **Primary service** is not set by the bridge today — the tile shows what iOS
  picks. That is the extension point for "tile shows temperature/speed"
  (a flag on `ServiceSpec`, applied in `bridge.py` when services are built).
- **Extra accessories cost the 149 cap and aids.** Prefer extra services on the
  same accessory; aids are keyed by device id (`aid_store.py`, never 1/7,
  never reissued, `retire_absent(keep=…)`) — do not change the key scheme.
- **Meter (V/A/W):** HAP has no types; Eve characteristics are custom UUIDs the
  pyhap loader does not know — they need a loader extension in `bridge.py`,
  and `hap_value` / `_float_service` rows for them. Today such floats skip as
  `no_homekit_type` (honest; keep it for anything not mapped).
- **Leave alone:** `engine.py` `note_mqtt` counter routing (buttons + the
  dual-bound case, §6), the scene M18 momentary reset, and the unreadable-store
  aid keep (`scene_devices.read_rules_doc`) — each is pinned by review-found
  tests.
- `MAPPING` is contract-pinned (HK §3 table, test counts it): a new row = a
  new `Mxx` + its §3 line + `test_projection` fixtures.

Delete this note once every item above is recorded in
`docs/decisions/homekit-home-connect.md` (its one durable home).
