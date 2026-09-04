"""Official Yandex Smart Home device-type catalog.

One home for the Alice picker, ``validate_device``, and discovery passthrough.
Ids and Russian labels are taken from
https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/device-types
(retrieved 2026-09-04). Do not invent types that page does not list.

This is the type enum only — admitting a type does not implement its
capabilities. A missing tile icon falls back to ``generic`` and does not
block save.
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

DEVICE_TYPE_LABELS: Dict[str, str] = {
    type_id: ru for _label, rows in DEVICE_TYPE_GROUPS for type_id, ru in rows
}


def official_type_ids() -> List[str]:
    """Stable picker order (group order, then row order)."""
    return [type_id for _label, rows in DEVICE_TYPE_GROUPS for type_id, _ru in rows]
