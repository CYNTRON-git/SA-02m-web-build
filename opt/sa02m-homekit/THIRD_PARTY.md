# Third-party notices — sa02m-homekit

The `sa02m_homekit` package itself is clean-room SA-02m code. It vendors no
third-party source: every dependency below is installed **unmodified** from
PyPI (hash-pinned wheels, `requirements.lock`) into the separate venv
`/opt/sa02m-homekit-venv`, or comes from the board's apt packages. Each wheel's
licence files ship inside its `*.dist-info/` in that venv. The install contract
and the evidence for the versions: `docs/decisions/homekit-home-connect.md`
(G1). The HAP non-commercial position is the Operator's recorded decision
(same doc, G8) — it is not a licence of any package below.

## Venv packages (`requirements.lock`)

| Package | Version | Licence | Source | Role |
|---|---|---|---|---|
| hap-python | 5.0.0 | Apache-2.0 | https://github.com/ikalchev/HAP-python | HAP accessory server: pairing, TLV, encrypted sessions, accessory model. Used through subclasses and documented hooks only (`bridge.py`); never patched. Upstream ships no NOTICE file; its `LICENSE` and `AUTHORS` travel in the wheel. |
| zeroconf | 0.151.3 | LGPL-2.1-or-later | https://github.com/python-zeroconf/python-zeroconf | mDNS advertisement of `_hap._tcp` (HAP-python's own dependency). Installed unmodified as its own distribution in the venv and dynamically imported, so it can be replaced by any compatible build (`pip install` over it); source at the link above. |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography | Ed25519 / X25519 / HKDF for HAP. Venv-only on purpose: the system copy verifies OTA signatures and is never upgraded by this module. |
| chacha20poly1305-reuseable | 0.13.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/bdraco/chacha20poly1305-reuseable | HAP session encryption (HAP-python dependency). |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | https://github.com/ijl/orjson | JSON encoding inside HAP-python. Unmodified binary wheel; source at the link. |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 | HTTP/1.1 framing of the HAP server (HAP-python dependency). |
| ifaddr | 0.2.0 | MIT | https://github.com/pydron/ifaddr | Interface enumeration (zeroconf dependency). |
| segno | 1.6.6 | BSD-3-Clause | https://github.com/heuer/segno | Server-side QR matrix of the setup URI (`setup_payload.py`); the served page carries no QR library. |

## System packages (apt, not in the lock)

| Package | Licence | Role |
|---|---|---|
| python3-paho-mqtt | EPL-2.0 OR EDL-1.0 | MQTT client to the local broker (`mqtt_link.py`); reused through `--system-site-packages`. |
| python3-cffi-backend | MIT | `_cffi_backend` C module `cryptography` loads (why the lock installs with `--no-deps`: see the lock header). |

## Reference only — no code copied

- **HAP-NodeJS** (Apache-2.0) — the bridged-accessory ceiling `MAX_ACCESSORIES = 149`
  is taken as a number (`constants.MAX_BRIDGED`); no source used.
- **Wiren Board** (`wb-ext-conventions`; MIT-WB, hardware-locked) and
  **Sprut.hub** (proprietary) — studied for behaviour only. The type-derived
  mapping in `projection.py` is our own design over the SA-02m device
  document; nothing was copied.
- The `X-HM://` setup-URI layout in `setup_payload.py` follows the HAP
  specification (the same layout HAP-python's `Accessory.xhm_uri` builds);
  reimplemented because that method needs the optional `base36` package.
