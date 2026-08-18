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

## Where real templates come from — and the license restriction

This project ships **no Wiren Board template files.** The `wb-mqtt-serial`
template collection is published under a **hardware-restricted MIT variant**:
its LICENSE limits use of the software "to Wiren Board controllers … hardware
manufactured by Contactless Devices, LLC or its affiliates." SA-02m runs on an
Allwinner A40i SoM — **not** Wiren Board hardware — so those template files are
**not vendored into this repo** (Operator decision, 2026-08-17: ship the
license-independent mechanism only).

The **integrator/operator supplies templates** into this dir at their own legal
discretion — a written WB permission, a differently-licensed template source, or
a clean-room template re-derived from a device's public Modbus datasheet.

## File naming

The `template:` value in the YAML device entry is a **bare name**; the resolver
looks for `config-<name>.json` then `<name>.json` in this dir. Example:

```yaml
- id: example-COM5-30
  type: template
  template: example        # → templates/config-example.json
  port: /dev/COM5
  baudrate: 9600
  address: 30
  name: "Example meter (COM5 addr=30)"
  poll_s: 2
```

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
