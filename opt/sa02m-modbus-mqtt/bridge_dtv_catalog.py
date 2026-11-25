"""DTV register metadata shared by the poller and configuration UI capability."""

# Sensor MQTT channels and MCU diagnostics. ZMOD scales below predate 1.0.2.4.
DTV_REGS: dict[int, tuple[str, float, str, str]] = {
    1:   ("temp_ds18b20",        0.1,   "temperature",  "°C"),
    2:   ("temp_mcp9808",        0.1,   "temperature",  "°C"),
    3:   ("temp_hdc1080",        0.1,   "temperature",  "°C"),
    4:   ("temp_bme280",         0.1,   "temperature",  "°C"),
    5:   ("temp_bme680",         0.1,   "temperature",  "°C"),
    6:   ("temp_ext",            0.1,   "temperature",  "°C"),
    7:   ("humidity_hdc1080",    0.1,   "rel_humidity", "%"),
    8:   ("humidity_bme280",     0.1,   "rel_humidity", "%"),
    9:   ("humidity_bme680",     0.1,   "rel_humidity", "%"),
    10:  ("pressure_bme280_mmhg",1.0,   "pressure",     "mmHg"),
    11:  ("pressure_bme680_mmhg",1.0,   "pressure",     "mmHg"),
    12:  ("pressure_bme280_kpa", 0.01,  "pressure",     "kPa"),
    13:  ("pressure_bme680_kpa", 0.01,  "pressure",     "kPa"),
    14:  ("altitude_bme280",     1.0,   "value",        "m"),
    15:  ("altitude_bme680",     1.0,   "value",        "m"),
    16:  ("gas_resist_bme680",   1.0,   "value",        "kΩ"),
    17:  ("iaq_bme680",          1.0,   "value",        "IAQ"),
    18:  ("eco2_bme680",         1.0,   "value",        "ppm"),
    19:  ("tvoc_zmod",           0.01,  "value",        "mg/m³"),
    20:  ("iaq_zmod",            1.0,   "value",        "IAQ"),
    21:  ("eco2_zmod",           1.0,   "value",        "ppm"),
    22:  ("etoh_zmod",           0.01,  "value",        "ppm"),
    25:  ("light_pct",           1.0,   "value",        "%"),
    26:  ("input_pb2",           1.0,   "switch",       ""),
    27:  ("presence",            1.0,   "switch",       ""),
    28:  ("moving_distance",     1.0,   "value",        "cm"),
    29:  ("still_distance",      1.0,   "value",        "cm"),
    30:  ("detect_distance",     1.0,   "value",        "cm"),
    123: ("mcu_vdd",             0.01,  "voltage",      "V"),
    # Reg 124 — int16 in tenths of °C (373 → 37.3), same as MR-02m/hardpy.
    124: ("mcu_temp",            0.1,   "temperature",  "°C"),
}
DTV_COILS = {1: ("buzzer", True), 2: ("leds", True)}

POLL_DEFAULTS = {"poll_sensors_s": 10, "poll_presence_s": 2,
                 "poll_diag_s": 60, "poll_uptime_s": 5}


def configuration_capability():
    """Supported controls, never a report of physically detected sensors."""
    return {"sensor_keys": [value[0] for reg, value in DTV_REGS.items() if 1 <= reg <= 30],
            "always_polled_keys": [value[0] for value in DTV_COILS.values()],
            "poll_defaults": dict(POLL_DEFAULTS)}
