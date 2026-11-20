"""Opaque Home document extensions; known fields always come from validators."""
ROOT_FIELDS = set("id name enabled type order trigger condition action code summary last_run last_error template template_id template_version source params end alice_expose captured_from".split())
TARGET = {"device", "cap", "instance"}
STATE = TARGET | {"op", "value"}
TRIGGER = {
    "state": STATE, "time": {"at", "days"}, "sun": {"event", "offset", "lat", "lon", "days"},
    "boot": set(), "every": {"minutes"}, "button": {"device", "gesture", "input"}, "presence": {"event"},
}
ACTION = {
    "set": TARGET | {"value", "transition_s"}, "toggle": TARGET,
    "ramp": TARGET | {"to", "seconds"}, "delay": {"seconds"}, "scenario": {"id"}, "scene": {"id"},
    "mode": {"value"}, "notify": {"text"}, "http": {"url", "method", "body"},
}
CONDITION = {
    "state": STATE | {"for_s"}, "time_window": {"preset", "from", "to"},
    "weekday": {"days", "preset"}, "mode": {"value"},
}


def validation_input(body):
    """The old condition validator rejects metadata on the list container."""
    result = dict(body)
    if isinstance(body.get("condition"), dict):
        result["condition"] = {k: v for k, v in body["condition"].items() if k in ("all", "any")}
    return result


def _node(raw, clean, known):
    return {**{k: v for k, v in raw.items() if k not in known}, **clean}


def _items(raw, clean, schema):
    # Home refuses dropped elements before this helper: indices identify the
    # same accepted item; an invalid known value cannot be restored as opaque.
    result = []
    for original, accepted in zip(raw, clean):
        known = schema.get(accepted["kind"], set()) | {"kind"}
        row = _node(original, accepted, known)
        if accepted["kind"] == "state" and isinstance(original.get("value"), dict) and isinstance(accepted.get("value"), dict):
            row["value"] = _node(original["value"], accepted["value"], {"min", "max"})
        result.append(row)
    return result


def restore_extensions(body, row):
    row = _node(body, row, ROOT_FIELDS)
    for part, schema in (("trigger", TRIGGER), ("action", ACTION)):
        if isinstance(body.get(part), list) and len(body[part]) == len(row[part]):
            row[part] = _items(body[part], row[part], schema)
    raw_condition = body.get("condition")
    if isinstance(raw_condition, dict):
        condition = _node(raw_condition, row["condition"], {"all", "any"})
        for key in ("all", "any"):
            raw, clean = raw_condition.get(key), row["condition"].get(key)
            if isinstance(raw, list) and isinstance(clean, list) and len(raw) == len(clean):
                condition[key] = _items(raw, clean, CONDITION)
        row["condition"] = condition
    for part, known in (("end", {"after_s", "mode"}), ("captured_from", {"room_id", "group_id"})):
        if isinstance(body.get(part), dict) and isinstance(row.get(part), dict):
            row[part] = _node(body[part], row[part], known)
    return row
