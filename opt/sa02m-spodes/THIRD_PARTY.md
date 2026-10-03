# Third-party notices — sa02m-spodes

The `sa02m_spodes` package is clean-room SA-02m code. It vendors no
third-party source. On the board it runs on system `python3` plus the
packages the Modbus→MQTT bridge already uses (`pyserial` for the HDLC
UART, `cryptography` for AES-GCM suite 0). No GPL-licensed source was
consulted or copied for this package.

## Specifications implemented

- IEC 62056-46 — HDLC type 3 (flags, address extension, FCS-16, LLC).
- IEC 62056-47 — TCP/UDP wrapper header.
- IEC 62056-5-3 — ACSE AARQ/AARE and xDLMS GET/SET/ACTION, LN only.
- IEC 62056-6-2 — A-XDR COSEM data types and the unit enum.
- СТО 34.01-5.1-006-2023 / Incotex SPODES object list (client SAPs 16,
  32, 48; AES-GCM-128 suite 0). Contract: `docs/contracts/spodes-mercury.md`.

## Reference only — no code copied

Behaviour was taken from those standards and from the Incotex object
catalogue (https://doc.incotexcom.ru/protocol/spodes/). The public
repositories Gurux DLMS (GPL-2.0) and gvtret/spodes-rs (GPL-3.0) were
not used as source and are not imported.
