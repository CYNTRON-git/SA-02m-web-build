"""Smart-home device-type catalog.

One home for the Alice picker, ``validate_device``, and discovery.
``DEVICE_TYPE_GROUPS`` is the Yandex official set (53 ids, Russian labels)
from https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/device-types
(retrieved 2026-09-04). Those ids and labels are unchanged.

``EXTENDED_DEVICE_TYPE_GROUPS`` adds types the official page does not list.
Metadata here is ours (id, Russian label, capability tags, the Yandex id
Alice discovery must send). Upstream lists were used as vocabularies only —
no source files or icons are vendored:

- HomeTiles tile types, MIT, Copyright (c) 2025 Sebastian.
  https://github.com/GalusPeres/HomeTiles
  https://galusperes.github.io/tiles/ (retrieved 2026-10-08)
- Matter device types, Apache-2.0 (project-chip/connectedhomeip).
  https://project-chip.github.io/connectedhomeip-doc/ids_and_codes/spec_device_types.html
- Home Assistant device classes and domains, Apache-2.0.
  https://www.home-assistant.io/integrations/cover/
  https://www.home-assistant.io/integrations/binary_sensor/
  https://www.home-assistant.io/integrations/sensor/
  https://www.home-assistant.io/integrations/siren/
  https://www.home-assistant.io/integrations/remote/
- Zigbee2MQTT exposes, GPL-3.0 (Koenkk/zigbee2mqtt). Names only.
  https://www.zigbee2mqtt.io/guide/usage/exposes.html
  https://github.com/Koenkk/zigbee2mqtt

Admitting a type does not implement it. A missing tile icon falls back to
``generic`` and does not block save. Yandex discovery sends
``yandex_type_for``; the cloud profile keeps the stored catalog id.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# (optgroup RU label, ((type_id, RU picker label), ...))
# Existing UI labels that already named a type are kept (Освещение, Розетка,
# Выключатель, Другое, Датчик, Климат-датчик, Датчик движения, Счётчик,
# Счётчик электроэнергии, Термостат, Вентустановка, Вентилятор). New types
# use the short official name from that page.
DEVICE_TYPE_GROUPS: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "Датчики",
        (
            ("devices.types.sensor", "Датчик"),
            ("devices.types.sensor.button", "Умная кнопка"),
            ("devices.types.sensor.climate", "Климат-датчик"),
            ("devices.types.sensor.gas", "Датчик газа"),
            ("devices.types.sensor.illumination", "Датчик освещённости"),
            ("devices.types.sensor.motion", "Датчик движения"),
            ("devices.types.sensor.open", "Датчик открытия двери"),
            ("devices.types.sensor.smoke", "Датчик дыма"),
            ("devices.types.sensor.vibration", "Датчик вибрации"),
            ("devices.types.sensor.water_leak", "Датчик протечки воды"),
        ),
    ),
    (
        "Счётчики",
        (
            ("devices.types.smart_meter", "Счётчик"),
            ("devices.types.smart_meter.cold_water", "Счётчик холодной воды"),
            ("devices.types.smart_meter.electricity", "Счётчик электроэнергии"),
            ("devices.types.smart_meter.gas", "Счётчик газа"),
            ("devices.types.smart_meter.heat", "Счётчик тепла"),
            ("devices.types.smart_meter.hot_water", "Счётчик горячей воды"),
        ),
    ),
    (
        "Медиаустройства",
        (
            ("devices.types.camera", "Видеокамера"),
            ("devices.types.media_device", "Медиаустройство"),
            ("devices.types.media_device.receiver", "Ресивер"),
            ("devices.types.media_device.tv", "Телевизор"),
            ("devices.types.media_device.tv_box", "ТВ-приставка"),
        ),
    ),
    (
        "Кухонная техника",
        (
            ("devices.types.cooking", "Кухонная техника"),
            ("devices.types.cooking.coffee_maker", "Кофеварка"),
            ("devices.types.cooking.kettle", "Чайник"),
            ("devices.types.cooking.multicooker", "Мультиварка"),
            ("devices.types.dishwasher", "Посудомоечная машина"),
        ),
    ),
    (
        "Бытовая техника",
        (
            ("devices.types.iron", "Утюг"),
            ("devices.types.vacuum_cleaner", "Робот-пылесос"),
            ("devices.types.washing_machine", "Стиральная машина"),
        ),
    ),
    (
        "Устройства для животных",
        (
            ("devices.types.pet_drinking_fountain", "Поилка"),
            ("devices.types.pet_feeder", "Кормушка"),
        ),
    ),
    (
        "Климатическая техника",
        (
            ("devices.types.humidifier", "Увлажнитель воздуха"),
            ("devices.types.purifier", "Очиститель воздуха"),
            ("devices.types.thermostat", "Термостат"),
            ("devices.types.thermostat.ac", "Кондиционер"),
            ("devices.types.ventilation", "Вентустановка"),
            ("devices.types.ventilation.fan", "Вентилятор"),
        ),
    ),
    (
        "Электрооборудование",
        (
            ("devices.types.light", "Освещение"),
            ("devices.types.light.ceiling", "Люстра"),
            ("devices.types.light.dimmable", "Диммер"),
            ("devices.types.light.garland", "Гирлянда"),
            ("devices.types.light.lamp", "Настольная лампа"),
            ("devices.types.light.sconce", "Бра"),
            ("devices.types.light.strip", "Диодная лента"),
            ("devices.types.light.torchere", "Торшер"),
            ("devices.types.socket", "Розетка"),
            ("devices.types.switch", "Выключатель"),
            ("devices.types.switch.relay", "Реле"),
        ),
    ),
    (
        "Открытие/закрытие",
        (
            ("devices.types.openable", "Открываемое"),
            ("devices.types.openable.curtain", "Шторы"),
            ("devices.types.openable.valve", "Шаровой кран"),
            ("devices.types.openable.door_lock", "Замок"),
        ),
    ),
    (
        "Остальные устройства",
        (("devices.types.other", "Другое"),),
    ),
)

OFFICIAL_DEVICE_TYPES = frozenset(
    type_id for _label, rows in DEVICE_TYPE_GROUPS for type_id, _ru in rows
)

# Capability tags later features read. Not Yandex capability ids.
CAPABILITY_TAGS = frozenset({
    "on_off", "brightness", "color", "climate", "cover",
    "sensor", "energy", "media", "security", "action",
})

# (id, RU label, capability tags, Yandex wire id, source)
# source: hometiles | matter | homeassistant | zigbee2mqtt
# Rows of a group are appended after that group's official rows.
EXTENDED_DEVICE_TYPE_GROUPS: Tuple[
    Tuple[str, Tuple[Tuple[str, str, Tuple[str, ...], str, str], ...]], ...
] = (
    (
        "Датчики",
        (
            ("devices.types.sensor.air_quality", "Датчик качества воздуха", ("sensor",), "devices.types.sensor", "matter"),
            ("devices.types.sensor.smoke_co", "Датчик дыма и CO", ("sensor", "security"), "devices.types.sensor.smoke", "matter"),
            ("devices.types.sensor.occupancy", "Датчик присутствия", ("sensor",), "devices.types.sensor.motion", "matter"),
            ("devices.types.sensor.rain", "Датчик дождя", ("sensor",), "devices.types.sensor", "matter"),
            ("devices.types.sensor.freeze", "Датчик замерзания", ("sensor",), "devices.types.sensor", "matter"),
            ("devices.types.sensor.soil", "Датчик почвы", ("sensor",), "devices.types.sensor", "matter"),
            ("devices.types.sensor.flow", "Датчик расхода", ("sensor",), "devices.types.sensor", "matter"),
            ("devices.types.sensor.window", "Датчик окна", ("sensor",), "devices.types.sensor.open", "homeassistant"),
            ("devices.types.sensor.carbon_monoxide", "Датчик угарного газа", ("sensor", "security"), "devices.types.sensor.gas", "homeassistant"),
            ("devices.types.sensor.pm25", "Датчик PM2.5", ("sensor",), "devices.types.sensor", "zigbee2mqtt"),
            ("devices.types.sensor.voc", "Датчик ЛОВ", ("sensor",), "devices.types.sensor", "zigbee2mqtt"),
            ("devices.types.weather", "Погода", ("sensor",), "devices.types.other", "hometiles"),
        ),
    ),
    (
        "Медиаустройства",
        (
            ("devices.types.media_device.speaker", "Колонка", ("media", "on_off"), "devices.types.media_device", "matter"),
            ("devices.types.remote", "Пульт", ("action",), "devices.types.other", "homeassistant"),
        ),
    ),
    (
        "Кухонная техника",
        (
            ("devices.types.cooking.oven", "Духовой шкаф", ("on_off", "climate"), "devices.types.cooking", "matter"),
            ("devices.types.cooking.microwave", "Микроволновка", ("on_off",), "devices.types.cooking", "matter"),
            ("devices.types.cooking.hood", "Кухонная вытяжка", ("on_off",), "devices.types.ventilation.fan", "matter"),
            ("devices.types.cooking.cooktop", "Варочная панель", ("on_off", "climate"), "devices.types.cooking", "matter"),
        ),
    ),
    (
        "Бытовая техника",
        (
            ("devices.types.refrigerator", "Холодильник", ("on_off", "climate"), "devices.types.other", "matter"),
            ("devices.types.dryer", "Сушильная машина", ("on_off",), "devices.types.other", "matter"),
        ),
    ),
    (
        "Климатическая техника",
        (
            ("devices.types.water_heater", "Водонагреватель", ("on_off", "climate"), "devices.types.thermostat", "matter"),
            ("devices.types.heat_pump", "Тепловой насос", ("on_off", "climate"), "devices.types.thermostat", "matter"),
        ),
    ),
    (
        "Электрооборудование",
        (
            ("devices.types.pump", "Насос", ("on_off",), "devices.types.switch", "matter"),
            ("devices.types.ev_charger", "Зарядная станция", ("on_off", "energy"), "devices.types.socket", "matter"),
        ),
    ),
    (
        "Открытие/закрытие",
        (
            ("devices.types.openable.blind", "Жалюзи", ("cover",), "devices.types.openable.curtain", "homeassistant"),
            ("devices.types.openable.shutter", "Рольставни", ("cover",), "devices.types.openable.curtain", "homeassistant"),
            ("devices.types.openable.garage", "Гаражные ворота", ("cover",), "devices.types.openable", "homeassistant"),
            ("devices.types.openable.gate", "Ворота", ("cover",), "devices.types.openable", "homeassistant"),
            ("devices.types.openable.damper", "Заслонка", ("cover",), "devices.types.openable", "homeassistant"),
            ("devices.types.openable.door", "Дверь", ("cover",), "devices.types.openable", "homeassistant"),
            ("devices.types.openable.window", "Окно", ("cover",), "devices.types.openable", "homeassistant"),
            ("devices.types.openable.awning", "Маркиза", ("cover",), "devices.types.openable.curtain", "homeassistant"),
            ("devices.types.irrigation", "Полив", ("on_off",), "devices.types.openable.valve", "zigbee2mqtt"),
        ),
    ),
    (
        "Охрана",
        (
            ("devices.types.alarm_panel", "Охранная панель", ("security",), "devices.types.other", "hometiles"),
            ("devices.types.siren", "Сирена", ("on_off", "security"), "devices.types.switch", "homeassistant"),
        ),
    ),
    (
        "Остальные устройства",
        (
            # Virtual Alice scenes stay devices.types.switch (scene_devices.py).
            ("devices.types.scene", "Сцена", ("action",), "devices.types.switch", "hometiles"),
        ),
    ),
)

_EXTENDED_BY_ID: Dict[str, Tuple[str, Tuple[str, ...], str, str]] = {
    type_id: (ru, caps, yandex, source)
    for _label, rows in EXTENDED_DEVICE_TYPE_GROUPS
    for type_id, ru, caps, yandex, source in rows
}

CATALOG_DEVICE_TYPES = OFFICIAL_DEVICE_TYPES | frozenset(_EXTENDED_BY_ID)


def _official_caps(type_id: str) -> Tuple[str, ...]:
    """Capability tags for a shipped Yandex id. Empty if the id is unknown."""
    special = {
        "devices.types.sensor.button": ("action", "sensor"),
        "devices.types.sensor.climate": ("sensor", "climate"),
        "devices.types.sensor.gas": ("sensor", "security"),
        "devices.types.sensor.smoke": ("sensor", "security"),
        "devices.types.sensor.motion": ("sensor", "security"),
        "devices.types.sensor.open": ("sensor", "security"),
        "devices.types.sensor.water_leak": ("sensor", "security"),
        "devices.types.camera": ("media", "sensor"),
        "devices.types.light.strip": ("on_off", "brightness", "color"),
        "devices.types.light.garland": ("on_off", "brightness", "color"),
        "devices.types.ventilation.fan": ("on_off",),
        "devices.types.openable.door_lock": ("security",),
    }
    if type_id in special:
        return special[type_id]
    if type_id == "devices.types.sensor" or type_id.startswith("devices.types.sensor."):
        return ("sensor",)
    if type_id.startswith("devices.types.smart_meter"):
        return ("energy", "sensor")
    if type_id.startswith("devices.types.light"):
        return ("on_off", "brightness")
    if type_id.startswith("devices.types.media_device"):
        return ("media", "on_off")
    if type_id.startswith("devices.types.cooking") or type_id in (
        "devices.types.dishwasher",
        "devices.types.iron",
        "devices.types.vacuum_cleaner",
        "devices.types.washing_machine",
        "devices.types.pet_drinking_fountain",
        "devices.types.pet_feeder",
        "devices.types.socket",
        "devices.types.switch",
        "devices.types.switch.relay",
        "devices.types.other",
    ):
        return ("on_off",)
    if type_id in (
        "devices.types.ventilation",
        "devices.types.humidifier",
        "devices.types.purifier",
    ) or type_id.startswith("devices.types.thermostat"):
        return ("on_off", "climate")
    if type_id.startswith("devices.types.openable"):
        return ("cover",)
    return ()


def picker_groups() -> Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]:
    """Picker order. Official rows of a group stay ahead of extended rows."""
    labels: List[str] = [label for label, _rows in DEVICE_TYPE_GROUPS]
    extra: List[str] = []
    for label, _rows in EXTENDED_DEVICE_TYPE_GROUPS:
        if label not in labels and label not in extra:
            extra.append(label)
    if "Остальные устройства" in labels:
        at = labels.index("Остальные устройства")
        labels = labels[:at] + extra + labels[at:]
    else:
        labels.extend(extra)
    bucket: Dict[str, List[Tuple[str, str]]] = {label: [] for label in labels}
    for label, rows in DEVICE_TYPE_GROUPS:
        bucket[label].extend(rows)
    for label, rows in EXTENDED_DEVICE_TYPE_GROUPS:
        bucket[label].extend((type_id, ru) for type_id, ru, _c, _y, _s in rows)
    return tuple((label, tuple(bucket[label])) for label in labels)


DEVICE_TYPE_LABELS: Dict[str, str] = {
    type_id: ru for _label, rows in picker_groups() for type_id, ru in rows
}


def official_type_ids() -> List[str]:
    """Yandex official ids, in the original group order."""
    return [type_id for _label, rows in DEVICE_TYPE_GROUPS for type_id, _ru in rows]


def catalog_type_ids() -> List[str]:
    """Every catalog id in picker order."""
    return [type_id for _label, rows in picker_groups() for type_id, _ru in rows]


def capabilities_of(type_id: str) -> frozenset:
    """Capability tags for a catalog id. Empty when the id is not in the catalog."""
    row = _EXTENDED_BY_ID.get(type_id)
    if row is not None:
        return frozenset(row[1])
    if type_id in OFFICIAL_DEVICE_TYPES:
        return frozenset(_official_caps(type_id))
    return frozenset()


def yandex_type_for(type_id: str) -> str:
    """Id to send on the Yandex discovery profile.

    Official ids pass through. An extended id maps to one official id.
    Anything else is returned unchanged (a hand-edited document).
    """
    if type_id in OFFICIAL_DEVICE_TYPES:
        return type_id
    row = _EXTENDED_BY_ID.get(type_id)
    if row is not None:
        return row[2]
    return type_id


def source_of(type_id: str) -> str:
    """``yandex`` for the shipped set, else the upstream token, else ``\"\"``."""
    if type_id in OFFICIAL_DEVICE_TYPES:
        return "yandex"
    row = _EXTENDED_BY_ID.get(type_id)
    return row[3] if row is not None else ""
