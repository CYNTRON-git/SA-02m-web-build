#!/usr/bin/env bash
# web-bus-mode-bacnet — the web surface that writes the field-bus selector
# (register 122) honours docs/contracts/web-bus-mode-bacnet.md at the WRITE
# PATH, not only in the pure value logic.
#
# WHY THIS EXISTS. Register 122 switches a module between Modbus and BACnet
# MS/TP; a wrong write strands the module off the bus until an in-band or a
# physical recovery. `opt/sa02m-flasher/tests/test_bus_mode.py` pins the value
# space, sanitiser, labels and recovery matrix of `bus_mode.py` — the pure
# layer. Nothing exercised `device_config.write_bus_mode`, the function that
# actually opens the transport and issues FC06, nor the daemon route that
# reaches it (audit 2026-09-08 F17: the contract was "validated" by syntax
# rows and a test of the wrong layer). This gate RUNS the shipped write path
# with a recording transport double and pins the route wiring statically.
#
# GUARANTEES CHECKED (each from the contract's own text):
#   1. mode ∉ {0,1,2} (an int out of range, a non-number) is refused with
#      ValueError BEFORE any transport is opened — no serial traffic on junk.
#   2. a family without the selector (ce / wb / bootloader) is refused with
#      ValueError after the identity read; NO write is issued; the transport
#      is closed (the lease is not leaked).
#   3. mode 2 on MR: exactly one FC06 write to register 122 with value 2;
#      `reboot_pending` true and NO fresh snapshot (the device is rebooting —
#      §5.3 is the verify job's business, not this call's).
#   4. mode 0 on DTV: one FC06 to 122 with 0; `reboot_pending` false; a fresh
#      snapshot is returned; `prior_mode` is the sanitised pre-write read.
#   5. a transport error string from the write surfaces as RuntimeError (the
#      route maps it to 409, ValueError to 400 — mqtt-set-endpoint idiom).
#   6. the two register homes agree: device_config.REG_FAST_MODBUS ==
#      bus_mode.REG_BUS_MODE == 122.
#   7. static (lib_check.sh, comment-stripped): service.py dispatches
#      POST /bus_mode to _handle_bus_mode, and that handler calls
#      device_config.write_bus_mode — the route cannot be silently unwired.
#
# METHOD. python3 imports the SHIPPED sa02m_flasher.device_config and swaps
# its module-level collaborators (_open_transport, _device_slave,
# _read_live_identity, _resolve_kind, family_from_kind, _read_u16,
# write_single, snapshot_for_device) for recording doubles; the function under
# test is the real one. Same interpreter picker as py-unit-flasher. The static
# half reads service.py through lib_check.sh so a commented-out dispatch line
# cannot satisfy the pin; that case is registered in comment-mutation-proof.
#
# NON-VACUOUS: an import failure, a missing service.py, or a case that ran
# zero assertions FAILS.
#
# Proven RED (1.0.6.40) by mutation:
#   * `#` before `return self._handle_bus_mode(ctx)` in service.py → case 7 FAIL
#   * the `if not bus_mode.bus_mode_valid(mode)` guard in write_bus_mode
#     replaced by `if False` → case 1 FAIL (transport opened, junk written)
#   * `REG_FAST_MODBUS = 122` → `= 123` in device_config.py → cases 3, 4, 6 FAIL
#
# Run: bash .ai-dev/quality/checks/web-bus-mode-bacnet.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT" || exit 1

# shellcheck source=.ai-dev/quality/checks/lib_check.sh
. "$ROOT/.ai-dev/quality/checks/lib_check.sh"

SVC=opt/sa02m-flasher/sa02m_flasher/service.py
DC=opt/sa02m-flasher/sa02m_flasher/device_config.py
fails=0
ok()  { printf 'web-bus-mode-bacnet: ok    %s\n' "$1"; }
bad() { printf 'web-bus-mode-bacnet: FAIL  %s\n' "$1"; fails=$((fails + 1)); }

for f in "$SVC" "$DC" opt/sa02m-flasher/sa02m_flasher/bus_mode.py docs/contracts/web-bus-mode-bacnet.md; do
    [ -f "$f" ] || { echo "web-bus-mode-bacnet: FAIL — missing $f"; exit 1; }
done

# ── case 7: the route is wired (static, comment-stripped) ──────────────────
if stripped_has "$SVC" 'if method == "POST" and p == "/bus_mode":' \
   && stripped_has "$SVC" 'return self._handle_bus_mode(ctx)' \
   && stripped_has "$SVC" 'return device_config.write_bus_mode(device_path, device, mode)'; then
    ok "7 POST /bus_mode dispatches to _handle_bus_mode → device_config.write_bus_mode"
else
    bad "7 POST /bus_mode is not wired to device_config.write_bus_mode — the contract's route is dead or bypasses the validated path"
fi

# ── cases 1-6: run the shipped write path with recording doubles ───────────
py=""
for p in python3 python py; do
    if "$p" -c "import sys" >/dev/null 2>&1; then py="$p"; break; fi
done
[ -n "$py" ] || { bad "no working python interpreter"; echo "web-bus-mode-bacnet: $fails FAILURE(S)"; exit 1; }

# Fresh bytecode cache per run. Python trusts a .pyc whose recorded source
# mtime AND size match; a mutation restored within the same second and of the
# same length (`122` ↔ `123`) leaves a .pyc compiled from the MUTATED source
# that the restored file still "matches" — the gate then judges bytes that are
# no longer on disk. Seen while proving this gate RED; a private cache prefix
# makes every run compile what it reads.
PYCACHE=$(mktemp -d) || exit 1
trap 'rm -rf "$PYCACHE"' EXIT

PYTHONIOENCODING=utf-8 PYTHONPYCACHEPREFIX="$PYCACHE" "$py" - <<'PY'
import sys
sys.path.insert(0, "opt/sa02m-flasher")
fails = 0
def ok(m):  print(f"web-bus-mode-bacnet: ok    {m}")
def bad(m):
    global fails; fails += 1; print(f"web-bus-mode-bacnet: FAIL  {m}")

try:
    from sa02m_flasher import device_config as dc, bus_mode as bm
except Exception as exc:  # noqa: BLE001
    bad(f"cannot import the shipped flasher modules: {exc!r}")
    print(f"web-bus-mode-bacnet: {fails} FAILURE(S)"); sys.exit(1)

class Rec:
    def __init__(self, family, write_err=""):
        self.family, self.write_err = family, write_err
        self.opened = 0; self.closed = 0; self.writes = []; self.snapshots = 0
    def install(self):
        rec = self
        def _open(path, dev):
            rec.opened += 1
            return (lambda *a, **k: None), (lambda: setattr(rec, "closed", rec.closed + 1))
        dc._open_transport = _open
        dc._device_slave = lambda dev: 7
        dc._read_live_identity = lambda send, slave, dev: {"kind": "x"}
        dc._resolve_kind = lambda ident, dev: ("kind-" + rec.family, ident)
        dc.family_from_kind = lambda kind: (None if rec.family in ("ce", "wb", "bootloader") else rec.family)
        dc._read_u16 = lambda send, slave, reg, t=700: 1   # prior = Fast
        def _write(send, slave, reg, value, t=800):
            rec.writes.append((slave, reg, value)); return rec.write_err
        dc.write_single = _write
        def _snap(*a, **k):
            rec.snapshots += 1; return {"snapshot": True}
        dc.snapshot_for_device = _snap
        return rec

# 1. junk mode: refused before any transport. The shipped rule is int(v) then
#    range (bus_mode._coerce, pinned by test_bus_mode.py): 3.5 truncates OUT of
#    range and is refused; a float truncating INTO range (2.5 → 2) is accepted
#    by design, and the daemon route int()s the JSON value before this call.
for junk in (3, -1, "x", None, 3.5):
    r = Rec("mr").install()
    try:
        dc.write_bus_mode("/dev/ttyS1", {"slave": 7}, junk)
        bad(f"1 mode={junk!r} accepted — the {{0,1,2}} allow-list is gone")
    except ValueError:
        if r.opened == 0 and not r.writes:
            ok(f"1 mode={junk!r} refused (ValueError) before any transport")
        else:
            bad(f"1 mode={junk!r} refused only AFTER opening the transport (opened={r.opened}, writes={r.writes})")
    except Exception as exc:  # noqa: BLE001
        bad(f"1 mode={junk!r}: wrong exception {exc!r}")

# 2. unsupported family: refused after identity, no write, transport closed
for fam in ("ce", "wb", "bootloader"):
    r = Rec(fam).install()
    try:
        dc.write_bus_mode("/dev/ttyS1", {"slave": 7}, 2)
        bad(f"2 family {fam}: write accepted — CE/WB/bootloader must not get a selector write")
    except ValueError:
        if not r.writes and r.closed == 1:
            ok(f"2 family {fam}: refused, no FC06 issued, transport closed")
        else:
            bad(f"2 family {fam}: writes={r.writes} closed={r.closed}")
    except Exception as exc:  # noqa: BLE001
        bad(f"2 family {fam}: wrong exception {exc!r}")

# 3. MR → BACnet (2): one write to 122 = 2, reboot pending, no snapshot
r = Rec("mr").install()
res = dc.write_bus_mode("/dev/ttyS1", {"slave": 7}, 2)
if r.writes == [(7, 122, 2)] and res.get("reboot_pending") is True and "snapshot" not in res and r.closed == 1:
    ok("3 MR mode 2: exactly one FC06 → reg 122 = 2, reboot_pending, no snapshot, transport closed")
else:
    bad(f"3 MR mode 2: writes={r.writes} result={res} closed={r.closed}")

# 4. DTV → classic (0): one write 122 = 0, live switch, snapshot, prior sanitised
r = Rec("dtv").install()
res = dc.write_bus_mode("/dev/ttyS1", {"slave": 7}, 0)
if (r.writes == [(7, 122, 0)] and res.get("reboot_pending") is False and res.get("snapshot") == {"snapshot": True}
        and res.get("prior_mode") == 1 and res.get("requested_mode") == 0):
    ok("4 DTV mode 0: one FC06 → reg 122 = 0, live switch with fresh snapshot, prior_mode 1")
else:
    bad(f"4 DTV mode 0: writes={r.writes} result={res}")

# 5. transport error on the write → RuntimeError (→ 409 at the route)
r = Rec("mr", write_err="timeout").install()
try:
    dc.write_bus_mode("/dev/ttyS1", {"slave": 7}, 1)
    bad("5 write error swallowed — the UI would show success on a failed switch")
except RuntimeError:
    ok("5 write error surfaces as RuntimeError (route → 409), transport closed" if r.closed == 1 else "5 RuntimeError but transport not closed")
    if r.closed != 1: fails += 1
except Exception as exc:  # noqa: BLE001
    bad(f"5 wrong exception {exc!r}")

# 6. one register, two homes, both 122
if dc.REG_FAST_MODBUS == bm.REG_BUS_MODE == 122:
    ok("6 REG_FAST_MODBUS == bus_mode.REG_BUS_MODE == 122")
else:
    bad(f"6 register homes disagree: device_config {dc.REG_FAST_MODBUS} vs bus_mode {bm.REG_BUS_MODE}")

print(f"web-bus-mode-bacnet: python half {'ok' if not fails else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
PY
[ $? -eq 0 ] || fails=$((fails + 1))

if [ "$fails" -eq 0 ]; then
    echo "web-bus-mode-bacnet: ALL OK"
    exit 0
fi
echo "web-bus-mode-bacnet: $fails FAILURE(S)"
exit 1
