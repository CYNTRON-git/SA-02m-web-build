# Device templates — drop-in dir for `type: template`

This directory holds JSON device templates the Modbus→MQTT bridge reads for a
device configured with `type: template` in `/etc/sa02m-modbus-mqtt.yaml`. A
template describes a device's registers (address, format, scale, units) so the
bridge polls and publishes it on Wiren-Board MQTT conventions with no hand-coded
family class. Contract + supported schema: `docs/contracts/template-device.md`.

## What ships here

- `config-example.json` — a **self-authored** fictional demo meter documenting
  the v1 supported schema. Used by the runtime tests and the web add-by-template
  picker. It is not a real device.
- `config-mp02-ahu.json` (`template: mp02-ahu`) — the CYNTRON **MP-02** PLC
  running its air-handling-unit (AHU / ПВУ) program, 24 controls. Authored by
  the MP-02 firmware team from their own register map, not a Wiren Board file
  (source: MP-02 firmware repo `CYNTRON-git/PLC_STM32F427` — a **private**
  repository, readable by CYNTRON staff only —
  `integrations/sa02m/config-mp02-ahu.json` (their PR #33)); the register
  map's one home is that repo's `docs/MODBUS_MAP.md` — not restated here.
  What bites an operator:
  - Line: RS-485 **19200 8N1, address 1** (MP-02 defaults), or Ethernet —
    Modbus TCP, unit 1 (`transport: tcp`; grammar and limits:
    `docs/contracts/bridge-modbus-tcp.md`).
  - The setpoints `temp_setpoint` / `humidity_setpoint` / `fan_speed_manual`
    (holding 190…192, int16 = value ×10) and `run` (coil 16) are the MP-02
    **operator window** — they need MP-02 firmware with the operator window
    (HR 190…193, coil 16; 2026-09-27) or newer. Out-of-range setpoints are clamped
    by the PLC; the read-back shows the accepted value.
  - `fan_mode` (holding 193, same ×10 window: `0` = auto, `1` = manual, on the
    wire 0/10; the PLC clamps anything else — < 5 → auto, else manual) needs
    MP-02 firmware of 2026-09-28 or newer; on older firmware it reads 0 and a
    write is refused by the PLC (exception 02 → `/meta/error = w`). In manual
    mode the fan runs at `fan_speed_manual` regardless of the unit's own mode,
    within the PLC's min/max limits.
  - `run` reads back the **operator start latch**, not the unit's state: a
    unit started by its schedule reads `run = 0` while running. Use
    `status_code` / `sequencer_state` for the real state.
  - Every write lands in MP-02's event log as an operator command. The bridge
    writes only on an explicit `/on` command (a retained `/on` is not
    replayed), never on poll.
  - Holding **129** is MP-02's bootloader entry; the template does not
    include it — never write it outside a flashing scenario.

## Where real templates come from — and the license restriction

This project ships **no Wiren Board template files.** The `wb-mqtt-serial`
template collection is published under a **hardware-restricted MIT variant**:
its LICENSE limits use of the software "to Wiren Board controllers … hardware
manufactured by Contactless Devices, LLC or its affiliates." SA-02m runs on an
Allwinner A40i SoM — **not** Wiren Board hardware — so those template files are
**not vendored into this repo** (Operator decision, 2026-08-17: ship the
license-independent mechanism only).

For third-party devices the **integrator/operator supplies templates** into
this dir at their own legal discretion — a written WB permission, a
differently-licensed template source, or a clean-room template re-derived from
a device's public Modbus datasheet.

## File naming

The `template:` value in the YAML device entry is a **bare name**; the resolver
looks for `config-<name>.json` then `<name>.json` in this dir. Example:

```yaml
- id: example-COM5-30
  type: template
  template: example        # → templates/config-example.json
  port: /dev/COM5
  baudrate: 9600           # only baudrate is configurable; the line is 8N1-only
  address: 30
  name: "Example meter (COM5 addr=30)"
  poll_s: 2

# Serial framing: the bridge opens the port **8N1** (parity=NONE, stopbits=ONE),
# hard-coded — there is no parity/stopbits field. A Wiren Board device on its
# common 9600 **8N2** factory line is NOT supported in v1 and will not answer
# (see docs/contracts/template-device.md §8).
```

A template device can also be polled over Ethernet (Modbus TCP): the entry
carries `transport: tcp`, `host`, `tcp_port` instead of `port`/`baudrate` —
`docs/contracts/bridge-modbus-tcp.md` is the one home of that grammar.

Only a `[A-Za-z0-9._-]+` name is accepted (no path separators, no traversal); a
name that resolves to no file leaves that one device idle and logs an error —
the rest of the fleet keeps polling.

## Supported vs deferred (v1)

Supported: `reg_type` coil/discrete/input/holding; `format`
u16/s16/u32/s32/float (32-bit honours `word_order`); `scale`/`offset`/`units`/
`type`/`readonly`/`enabled`; `device.setup` holding writes; writable
holding/coil channels.

Deferred — **skipped loudly** (a WARN per channel, never mis-polled): bitfield
addresses (`reg:shift:width`), `bcd`/`string`/`u64`/`s64`/`u8`/`s8`,
`byte_order` in-register swaps, `consists_of` composites, `condition`
expressions, sub-devices, and Jinja templates. A template whose every channel is
unsupported logs an ERROR and publishes nothing (a mis-import is surfaced, not
silently half-working). Register maps are **unverified against hardware** until
bench-confirmed against the real device.
