# MPLC4 activation on bench 1.136 — «Wrong SystemKey / Not activated» (2026-08-17)

Durable diagnosis (moved out of the volatile resume pointer). This is a
vendor-runtime / licensing matter, **not** a SA-02m-web-build code bug.

## Symptom
On 1.136 the MPLC4 runtime logs (`/var/log/mplc4/0/<date>.txt`) at each start:
`Wrong SystemKey` ×3 → `<Protect> Not activated (4)! Request key : 000C00000253F942BC1F`.
The web «Обновление проекта MPLC» license line therefore shows «не активирована».

## What is actually true
- The installed key `/opt/mplc4/server/mplc.key` (content starts `70152fd6…`) **IS
  the correct API-issued license 410293** — it matches the stand's activation
  journal entry (`SA02M-136`, controllerID `009300008A332421D8EC0E4D`, SystemKey
  `8A332421D8EC0E4D`, licenseNumber 410293, installed:true). The key is right and
  in place.
- BUT the runtime now computes request key **`000C00000253F942BC1F`** =
  `000C0000` + MAC `02:53:f9:42:bc:1f` — a **MAC-based fallback** — instead of
  reading the hardware SystemKey `8A332421D8EC0E4D` it read at activation time.
  So it presents the wrong identity and rejects the valid key.

## Root cause (hypothesis, high-confidence)
The runtime stopped reading the hardware SystemKey and fell back to MAC. Coincides
with: MPLC version now `1.3.10.34421 RC_20260813` (vs activation-time `…34027…
+cyntron`) AND this session's `/opt/mplc4` driver `.so` swap (09-mplc.sh crash
fix). One of these broke SystemKey reading.

## Fix direction (vendor-runtime, Operator/4D-dev)
Restore the MPLC version/driver state that reads hardware SystemKey
`8A332421D8EC0E4D` — then the installed key 410293 validates. NOT a web-code fix.

## Reference facts
- Request key = `000C0000` + MAC eth0. The A40i board has NO hardware MAC; the
  image derives a stable MAC from SID (`/usr/local/sbin/sa02m-pre-start.sh`); a
  MAC override sits in `/etc/sa02m-mac.conf`. **If the MAC is not pinned, the
  license dies on reboot.** 1.136's current MAC (02:53:f9:42:bc:1f) is pinned in
  `/etc/sa02m-mac.conf` but is NOT the SID-stable value and NOT a licensed one.
- Stand 1.135 key store: `/var/lib/sa02m-stand/license_keys/` (files named by
  request key), journal `/var/lib/sa02m-stand/license_keys.jsonl`. Activation
  project: `C:\Users\admin\Downloads\hardpy_tests` (`lib/license_activation.py`,
  MasterActivator API). A key for the SID-stable request key
  `000C00000253D20A1564` also exists in the store.
