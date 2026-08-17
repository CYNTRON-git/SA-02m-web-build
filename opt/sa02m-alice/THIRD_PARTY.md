# Third-party notices — sa02m-alice

## yandex_smart_home (MIT)

- Project: https://github.com/dext0r/yandex_smart_home
- Copyright 2021–2026 Artem Sorokin
- License: MIT (`LICENSE.txt` upstream)
- Use in SA-02m: Yandex Smart Home **error codes**, response envelope shape, and
  (in the companion gateway repo) pydantic schemas / contract fixtures.
- This controller tree does **not** vendor HA runtime code.

## wb-mqtt-alice (MIT-WB — reference only)

- Project: https://github.com/wirenboard/wb-mqtt-alice
- License: The WB License (MIT-WB) — **hardware-locked to Wiren Board controllers**
- Use in SA-02m: **protocol/behavior reference only**. No source files were
  copied. Socket.IO event names, MQTT `/on` write convention, two-stage state
  rate limiting, and `client_enabled=false` standby exit are reimplemented
  clean-room in `sa02m_alice/`.

## python-socketio / python-engineio

- Installed at deploy time by `scripts/06-alice.sh` when absent.
- Used only by `sa02m-alice-client` when `client_enabled=true`.
