# Bench SA02M-136: untracked bridge patch (2026-07-24 → closed by 1.0.5.50)

- Bench **192.168.10.136** ran a hand-applied SSH patch of
  `opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py` from **2026-07-24**: the WB
  form of `configure_events` (0x18), the two event-record grammars, and a
  local `MR02M_AI_READ_CHUNK_REGS` 42 → 21. It existed **only on that device**
  — no branch, no commit, and the next routine `scripts/update-www-only.sh`
  deploy would have overwritten it without a trace.
- **Closed by release 1.0.5.50**: the wire work landed in-repo (grammar home
  `docs/contracts/fmb-event-wire.md`), and the chunk size became the
  per-device key `ai_read_chunk_regs` instead of a global default change — so
  bench 136 is tuned in its own `/etc/sa02m-modbus-mqtt.yaml`
  (`ai_read_chunk_regs: 21`), not in code.
- The bench is a declared sibling component (`.ai-dev/components.json`,
  entry `hardpy_tests`) and reads its RS-485 verifiers on COM4 **through this
  bridge** — a deploy to 136 restarts the service and stalls the port, so it
  is agreed with the bench owner, never done mid-run.
- **On the next divergence** (bench behaviour that the repo cannot explain):
  check the deployed web build against `docs/SA02M_WEB_BUILD_PIN.md` in the
  `hardpy_tests` sibling FIRST — that pin is where the bench records which
  build it expects. A device-only patch is the failure mode to look for.
