# Third-party notices — sa02m-homeconnect

The `sa02m_homeconnect` package is clean-room SA-02m code. It vendors no
third-party source and installs nothing from PyPI: it runs on the board's
system `python3` with the standard library plus one apt package
(`tests/test_stdlib_only.py` fails on any other import). The design and the
evidence behind the API facts: `docs/decisions/homekit-home-connect.md`
(G6/G7) and `docs/contracts/home-connect.md`.

## System packages (apt)

| Package | Licence | Role |
|---|---|---|
| python3-paho-mqtt | EPL-2.0 OR EDL-1.0 | MQTT client to the local broker (`mqtt_link.py`), publish only. Imported only by the daemon, after `main.missing_dependencies()` found it; the CGI dispatch never imports it. |

## Specifications implemented

- OAuth 2.0 Device Authorization Grant — RFC 8628 (`oauth.py`; `slow_down`
  adds 5 s per §3.5); refresh grant — RFC 6749 §6.
- Server-Sent Events — WHATWG HTML, "Server-sent events" (`sse.py` line
  parser).
- The Wiren Board MQTT topic convention as this repo documents it
  (`docs/MQTT_TOPICS.md`) — no Wiren Board code involved.

## Reference only — no code copied

Read for API facts (endpoint paths, key names, rate-limit and retry
behaviour, the event list and the missing-`id` quirk), cited by commit in
`.ai-dev/plans/homekit-home-connect-research.md` §C; every line here was
written for this package:

- **homebridge-homeconnect** (ISC) — @ `f568d07`.
- **aiohomeconnect** (Apache-2.0) — @ `e8ff3e7`.

No GPL-licensed source was consulted for this package.
