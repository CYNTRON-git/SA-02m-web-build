# MR-02m holding 134 — a module that clears its own outputs (2026-08-27)

**Symptom.** A DO switched on over MQTT goes back to 0 by itself after
10–25 s. No command on the bus, no bridge writeback, no second poll value —
the output simply falls. An identical module at another address holds
forever. Reflashing the application does NOT cure it.

**Cause.** Holding register **134 — «время без опроса, с»** (Modbus
inactivity timeout). When it is non-zero the module clears **all** of its
outputs after that many seconds without a frame that resets its inactivity
counter. The per-second tick calls `clear_all_output()`. The value lives in an
external EEPROM (CAT24C02), so it survives both a power cycle and an
application reflash — which is exactly why a firmware update looks like it
"did nothing".

**The write is deferred, not immediate.** `eeprom_update()` only arms a
countdown (`eeprom_must_write = EEPROM_MUST_WRITE_TIMER`,
`Core/storage/src/config_accessors.c:347-353`) that a later tick flushes; the
firmware names the contrast itself at `Core/storage/src/eeprom_config.c:686`.
So after changing register 134, give the module a few seconds before cutting
its power, or the new value is lost and the fault appears to come back.

Firmware homes, all in the sibling repo `CYNTRON-git/MR-02m`:
`Core/system/src/system_timer.c:172-178` (the tick and the clear),
`shared/modbus/src/modbus_rtu_write.c:423-427` (register 134 → `set_inactivity()`
→ `eeprom_update()`), `Core/storage/src/config_accessors.c:195-211` (accessors).

**What resets the counter — five sites, and only one of them is
address-matched:**

| Site | Gate |
| --- | --- |
| `modbus_rtu_hw.c:1757-1765` | classic RTU frame whose first byte **is this module's address** |
| `modbus_rtu_hw.c:1699-1703` | emulated `0x08`/`0x60` frames (DMA path) — no address check |
| `modbus_rtu_hw.c:1681-1685` | Fast-Modbus `0x46` frames — no address check |
| `fast_mb_isr.c:708-714` | the same `0x08`/`0x60` frames on the IRQ path — no address check |
| `config_accessors.c:205-207` | writing register 134 itself |

The table is exhaustive — `grep -rn set_counter_inactivity --include=*.c
Core/ shared/` in that repo returns exactly these call sites. Note that a
`0xFF` broadcast is **not** among them: the broadcast branch
(`modbus_rtu_hw.c:1733-1756`) resets nothing, and it fails the
`buf[0] == get_modbusaddress()` gate. Do not confuse this counter with the
RED_LED one — `inactivity_modbus` (`uint16_t`, `system_timer.c:88-89`) is a
different variable from the `uint8_t counter_inactivity` that drives
`clear_all_output()`, and the firmware comment at `modbus_rtu_hw.c:1679-1680`
talks about the former.

**Why 1 s was fatal on this bench — and what was NOT established.** The bridge
runs Fast Modbus against these modules: it is the default for `mr02m` devices
regardless of configuration (`opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py:241-243`),
and the bench sets it explicitly as well (`fast_modbus: true` per COM4 device in
`/etc/sa02m-modbus-mqtt.yaml` on 192.168.1.135). So several of the un-gated
reset sites are in play, not only the address-matched one. Even so the 1 s
timeout fired repeatedly during ordinary operation: whatever frames the bridge
actually emits, the gap between two that reset address 11's counter exceeded
one second. Which frame type carries the resets on this line was **not**
traced — the A/B/A experiment below is the evidence, not a frame-level
account. The practical rule is simply that 1 s is shorter than the real gap on
a shared, round-robin line; the drop looks non-deterministic (10–25 s) only
because it lands on whichever sweep first stretches past the limit.

**Confirmed on bench 192.168.1.135, module `mr02m-COM4-11` (16DO), A/B/A:**
134=0 → held 60 s · 134=1 → dropped after 20 s · 134=0 → held 60 s. Left at 0.
The whole COM4 line was audited the same day: every other module (10, 12, 13,
14) already had 0.

**Where it comes from.** The value is user-editable — «Время без опроса, с»
in the module-config window (`www/network_config/static/js/flasher.js`,
`saveMrGlobalInactivity`, and the bulk template-apply path that copies one
module's config onto others). Nothing on the SA-02m side writes it
automatically.

**Next time an output falls on its own:** read holding 134 on that module
BEFORE suspecting the bus, the bridge, or the firmware version. Compare
against a module that holds — the register diff answers it in one read
(`/api/flasher` → `device_config/snapshot`, field `mr.inactivity_s`).
