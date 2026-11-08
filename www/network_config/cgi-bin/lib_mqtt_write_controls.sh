#!/bin/bash
# Shared by mqtt_set.cgi and the template catalog's web-write capability.
MQTT_WEB_WRITE_PATTERN='^(do_([1-9]|1[0-6])|ao_([1-9]|1[0-2])|ai_type_([1-9]|1[0-2])|buzzer|leds|unit_on|setpoint|setpoint_summer|fan_supply|fan_step|alarm_reset|detection_distance|detection_shielding_distance|admission_confirmation_delay|departure_disappearance_delay|trigger_sensitivity|maintain_sensitivity|entrance_distance_reduction|load_disconnect)$'
readonly MQTT_WEB_WRITE_PATTERN
