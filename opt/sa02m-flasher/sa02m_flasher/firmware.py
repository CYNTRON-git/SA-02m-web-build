# -*- coding: utf-8 -*-
"""
Load firmware from .fw or .bin.
.fw: bootloader format — info 32 B (sig 12 + size 4 LE + pad) + data blocks (246 B each as 123 big-endian words).
.bin: raw image for 0x08000800 (FLASH_APP_START).
"""
import hashlib
import re
import struct
import zlib
from pathlib import Path
from typing import Optional, Tuple, List

# Адрес начала приложения (2 КБ от начала Flash). В Python обязательно 8 hex-цифр: 0x08000800 (иначе 0x0800800 = 8390656).
FLASH_APP_START = 0x08000800
# STM32F030: Flash 0x08000000..0x0803FFFF (256 КБ)
FLASH_END = 0x08040000
RAM_START = 0x20000000
RAM_END = 0x20008000  # STM32F030CC 32 KB
# Конец области приложения (до staging 0x0802F800), согласовано с STM32F030CCXX_FLASH_boot.ld
FLASH_APP_END = 0x0802F800
# Область приложения: 0x08000800..0x0802F800. Лимит = 192512 байт (188 КБ).
MAX_FIRMWARE_SIZE = (FLASH_APP_END - FLASH_APP_START)  # 0x2F000 = 192512

# Образ бутлоадера для прошивальщика: 2 КБ векторы + 32 КБ код = 34 КБ.
BL_VECTORS_ORIGIN = 0x08000000
BL_VECTORS_SIZE = 2048
BL_CODE_ORIGIN = 0x08038000
BL_CODE_SIZE = 32768
BL_IMAGE_TOTAL_BYTES = BL_VECTORS_SIZE + BL_CODE_SIZE  # 34816


def _effective_size(image: bytes) -> int:
    """
    Размер образа без хвоста из незапрограммированного Flash (только 0xFF).

    Не обрезать по 0x00/0xA5/0xCD: в конце AppBoot .bin это могут быть валидные байты
    машинного кода или данных. Размер выравнивается по 4 байта для записи во Flash.
    """
    if not image:
        return 0
    last = len(image) - 1
    while last >= 0 and image[last] == 0xFF:
        last -= 1
    used = last + 1
    if used == 0:
        return len(image)
    return (used + 3) & ~3


def parse_known_firmware_filename(filename: str) -> Optional[Tuple[str, str, str]]:
    """Return (device, kind, version) for an exact known release basename."""
    match = re.fullmatch(
        r"(MR-02m|RTU-Sensor|CE-02m-3|RGBW_WS2812)_(bootloader_)?([0-9]+(?:\.[0-9]+){3})\.(?:fw|bin)",
        filename,
        re.I | re.ASCII,
    )
    if match is None:
        return None
    if match[1].upper() == "RGBW_WS2812":
        return None if match[2] else ("LED", "app", match[3])
    family = {"mr-02m": "MR-02m", "rtu-sensor": "RTU-Sensor", "ce-02m-3": "CE-02m-3"}
    return family[match[1].lower()], "bootloader" if match[2] else "app", match[3]


def parse_version_from_filename(filename: str) -> Optional[str]:
    """
    Извлекает версию из имени файла MR-02m_<version>.fw / .bin.
    Поддерживаются 1–4 компонента: 1, 1.0, 1.0.0, 1.0.0.0 (недостающие дополняются нулями до X.Y.Z.W).
    """
    known = parse_known_firmware_filename(filename)
    if known is not None:
        return known[2]
    m = re.match(r"ce-02m-3_(\d+\.\d+\.\d+\.\d+)\.(?:fw|bin)$", filename, re.I)
    if m:
        return m.group(1)
    m = re.match(r"ce-02m-3_(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?\.(?:fw|bin)$", filename, re.I)
    if m:
        parts = [m.group(1), m.group(2) or "0", m.group(3) or "0", m.group(4) or "0"]
        return ".".join(parts)
    m = re.match(r"ce-02m-3_(.+)\.(?:fw|bin)$", filename, re.I)
    if m:
        ver = m.group(1).strip()
        return ver if re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", ver) else "?"
    # Бутлоадер: MR-02m_bootloader_X.Y.Z.W.fw (до общего MR-02m_<ver>, иначе ver = «bootloader_0…»)
    m = re.match(r"MR-02m_bootloader_(\d+\.\d+\.\d+\.\d+)\.(?:fw|bin)$", filename, re.I)
    if m:
        return m.group(1)
    # Строго: четыре числа (X.Y.Z.W)
    m = re.match(r"MR-02m_(\d+\.\d+\.\d+\.\d+)\.(?:fw|bin)$", filename, re.I)
    if m:
        return m.group(1)
    # 1–3 компонента: дополняем до четырёх
    m = re.match(r"MR-02m_(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?\.(?:fw|bin)$", filename, re.I)
    if m:
        parts = [m.group(1), m.group(2) or "0", m.group(3) or "0", m.group(4) or "0"]
        return ".".join(parts)
    # Любой MR-02m_<что-то>.fw/.bin — версия «как есть» или «?»
    m = re.match(r"MR-02m_(.+)\.(?:fw|bin)$", filename, re.I)
    if m:
        ver = m.group(1).strip()
        return ver if re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", ver) else "?"
    # .fw: mp02m_1.0.0.fw, MR-02m_1.0.0.0.fw
    m = re.fullmatch(r"(?:MR-02m|mp02m|mp-02m)_([0-9]+(?:\.[0-9]+){0,3})\.fw", filename, re.I | re.ASCII)
    if m:
        return m.group(1)
    m = re.match(r"(?:MR-02m|mp02m|mp-02m)_(.+)\.fw$", filename, re.I)
    if m:
        ver = m.group(1).strip()
        return ver if re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", ver) else "?"
    return None


# Manufacturer application markers are NUL-terminated C strings in flash.
_MARKER = re.compile(rb"(?<![A-Za-z0-9_])(MR02M|DTV|LED|CE02M3)_VER:([0-9]{1,5}(?:\.[0-9]{1,5}){3})(?=\x00|\Z)")
_MARKER_FAMILIES = {b"MR02M": "MR-02m", b"DTV": "RTU-Sensor", b"LED": "LED", b"CE02M3": "CE-02m-3"}


class FirmwareIdentityError(ValueError):
    """Two explicit identifiers describe different firmware."""


def filename_family(filename: str) -> Optional[str]:
    known = parse_known_firmware_filename(filename)
    if known:
        return known[0]
    legacy = re.fullmatch(r"(MR-02m|mp-?02m|ce-02m-3)_([0-9]+(?:\.[0-9]+){0,3})\.(fw|bin)",
                          filename, re.I | re.ASCII)
    if legacy:
        return "CE-02m-3" if legacy[1].lower() == "ce-02m-3" else "MR-02m"
    return None


def versions_match(left: str, right: str) -> bool:
    """Compare a legacy one-to-four-component release name to a marker."""
    def components(value):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value):
            return None
        parts = [part.lstrip("0") or "0" for part in value.split(".")]
        return parts + ["0"] * (4 - len(parts))

    expected = components(left)
    return expected is not None and expected == components(right)


def _application_marker(raw: bytes) -> Optional[Tuple[str, str]]:
    identity = None
    for match in _MARKER.finditer(raw):
        candidate = _MARKER_FAMILIES[match[1]], match[2].decode("ascii")
        if identity is not None and identity != candidate:
            raise FirmwareIdentityError("В образе найдены противоречивые маркеры приложения.")
        identity = candidate
    return identity


def parse_version_from_image(image: bytes, *, search_u32: bool = True) -> Optional[str]:
    """Read an explicit version marker. The legacy search_u32 keyword is accepted."""
    marker = _application_marker(image)
    return marker[1] if marker else None


def _fw_payload(image: bytes, size: int) -> bytes:
    count = (size + 1) & ~1
    encoded = image[FW_INFO_SIZE:FW_INFO_SIZE + count]
    if len(encoded) != count:
        raise ValueError("Контейнер .fw короче указанного размера приложения.")
    raw = bytearray(count)
    raw[::2], raw[1::2] = encoded[1::2], encoded[::2]
    return bytes(raw[:size])


def _fw_signature(data: bytes) -> str:
    name, _, padding = data[:12].partition(b"\x00")
    if padding.strip(b"\x00\xff") or any(c < 32 or c > 126 for c in name):
        raise FirmwareIdentityError("Некорректная сигнатура заголовка .fw.")
    return name.decode("ascii").strip() or "NONE"


def application_metadata(filename: str, image: bytes, *, decoded: Optional[bytes] = None,
                         packed_header: bool = True) -> Tuple[str, Optional[str]]:
    """Return the reported version and an independently marked family, if present."""
    name_version = parse_version_from_filename(filename)
    suffix = Path(filename).suffix.lower()
    if decoded is not None:
        marker = _application_marker(decoded)
    elif suffix == ".fw" and len(image) >= FW_INFO_SIZE:
        size = struct.unpack_from("<I", image, 12)[0]
        if not 0 < size <= MAX_FIRMWARE_SIZE:
            return name_version or "?", None
        # make_fw stores little-endian flash bytes as big-endian 16-bit words.
        marker = _application_marker(_fw_payload(image, size))
    elif suffix == ".bin" and len(image) <= MAX_FIRMWARE_SIZE:
        marker = _application_marker(image)
    else:
        marker = None
    if marker is None:
        return name_version or "?", None
    family, version = marker
    named_family = filename_family(filename)
    if named_family and named_family != family:
        raise FirmwareIdentityError(f"Семейство в имени {named_family} не совпадает с маркером {family}.")
    if name_version and name_version != "?" and not versions_match(name_version, version):
        raise FirmwareIdentityError(f"Версия в имени {name_version} не совпадает с маркером {version}.")
    if suffix == ".fw" and packed_header:
        from . import module_profiles as mp
        signature = _fw_signature(image)
        header_family = mp.firmware_header_family(signature)
        if header_family and header_family != family:
            raise FirmwareIdentityError(f"Семейство заголовка {header_family} не совпадает с маркером {family}.")
    return version, family


def check_app_vector_table(payload: bytes) -> Tuple[bool, Optional[str]]:
    """
    Проверить, что начало образа похоже на таблицу векторов (SP, Reset_Handler).
    Если образ собран для 0x08000000 (Debug/Release), а не для 0x08000800 (AppBoot),
    бутлоадер не запустит приложение. Возвращает (True, None) если OK.
    """
    if len(payload) < 8:
        return False, "образ меньше 8 байт"
    sp, reset_handler = struct.unpack_from("<II", payload, 0)
    if sp < RAM_START or sp > RAM_END:
        return False, (
            f"SP=0x{sp:08X} не в RAM (0x2000xxxx). "
            "Соберите прошивку конфигурацией AppBoot (STM32F030CCXX_FLASH_boot.ld)."
        )
    if (reset_handler & 0xFF000000) != 0x08000000:
        return False, (
            f"Reset_Handler=0x{reset_handler:08X} не в Flash. "
            "Используйте образ AppBoot."
        )
    if reset_handler < FLASH_APP_START or reset_handler >= FLASH_APP_END:
        return False, (
            f"Reset_Handler=0x{reset_handler:08X} вне 0x{FLASH_APP_START:x}..0x{FLASH_APP_END:x}. "
            "Используйте образ AppBoot."
        )
    if (reset_handler & 1) == 0:
        return False, f"Reset_Handler=0x{reset_handler:08X} без бита Thumb."
    if sp == 0xFFFFFFFF or reset_handler == 0xFFFFFFFF:
        return False, "образ пустой (0xFFFFFFFF)"
    return True, None


def load_bin(path: Path) -> Tuple[bytes, int]:
    """Load raw .bin. Returns (image_bytes, effective_size without trailing 0xFF)."""
    return _bin_image(path.read_bytes())


def _bin_image(data: bytes) -> Tuple[bytes, int]:
    if len(data) > MAX_FIRMWARE_SIZE:
        raise ValueError(
            f"Размер файла {len(data)} байт превышает допустимый для области приложения ({MAX_FIRMWARE_SIZE} байт). "
            "Используйте образ AppBoot (0x08000800..0x08038000)."
        )
    size = _effective_size(data)
    if size == 0:
        size = len(data)
    if size > MAX_FIRMWARE_SIZE:
        raise ValueError(
            f"Эффективный размер образа {size} байт превышает допустимый {MAX_FIRMWARE_SIZE} байт."
        )
    return data[:size], size


FW_INFO_SIZE = 32
FW_BLOCK_BYTES = 246  # Modbus 0x10 max (123 reg = 246 B per block), согласовано с бутлоадером


def _fw_container(data: bytes) -> Tuple[bytes, int, str]:
    """
    Загрузка .fw (формат бутлоадера). Возвращаем файл как есть — первые 32 байта пойдут в info-блок (BE), далее блоки по 246 B.
    Format: 32 B info (sig 12 ASCII + size 4 LE + pad) + data as 123 big-endian 16-bit words per block (246 B).
    Returns (full_file_bytes, size, signature_from_file).
    """
    if len(data) < FW_INFO_SIZE:
        raise ValueError("Файл .fw слишком короткий (нет info-блока 32 байт)")
    signature = _fw_signature(data)
    size = struct.unpack_from("<I", data, 12)[0]
    if size <= 0 or size > MAX_FIRMWARE_SIZE:
        hint = ""
        if len(data) == BL_IMAGE_TOTAL_BYTES:
            hint = " Размер файла 34 КБ — похоже на образ бутлоадера: выберите режим «Прошивка бутлоадера» и укажите этот файл там."
        raise ValueError(
            f"В .fw указан размер {size} байт — недопустимо (ожидается 1..{MAX_FIRMWARE_SIZE}). "
            "Возможно, выбран не тот файл (не .fw приложения) или файл повреждён. "
            "Используйте .fw, собранный make_fw.py / скриптом прошивки приложения."
            + hint
        )
    return data, size, signature


def load_fw(path: Path) -> Tuple[bytes, int, str, str]:
    data, size, signature = _fw_container(path.read_bytes())
    version, _ = application_metadata(path.name, data)
    return data, size, version, signature


def _is_printable_ascii_signature(s: str) -> bool:
    """Проверка: строка сигнатуры — печатные ASCII (пробел 0x20..0x7E), без управляющих символов."""
    if not s:
        return False
    return all(0x20 <= ord(c) <= 0x7E for c in s)


def _decode_signature_candidate(raw: bytes) -> Optional[str]:
    """Декодировать байты в строку-сигнатуру: убрать ведущие/замыкающие null, оставить только печатные ASCII."""
    if not raw:
        return None
    raw = raw.strip(b"\x00")
    if not raw:
        return None
    try:
        s = raw.decode("ascii", errors="strict")
    except Exception:
        return None
    s = "".join(c for c in s if 0x20 <= ord(c) <= 0x7E)
    return s if s and len(s) >= 2 else None


def _find_longest_printable_run(data: bytes, max_len: int = 24) -> Optional[str]:
    """Найти самую длинную подстроку из печатных ASCII в data[:max_len] (длина >= 2)."""
    if len(data) < 2:
        return None
    data = data[:max_len]
    best = ""
    cur = []
    for b in data:
        if 0x20 <= b <= 0x7E:
            cur.append(chr(b))
        else:
            if len(cur) >= 2:
                s = "".join(cur)
                if len(s) > len(best):
                    best = s
            cur = []
    if len(cur) >= 2:
        s = "".join(cur)
        if len(s) > len(best):
            best = s
    return best if best else None


def _extract_wbfw_signature(data: bytes) -> Optional[str]:
    """
    Извлечь сигнатуру из первых 32 байт .wbfw. Поддерживаются варианты:
    - 12 байт подряд (ASCII + null-pad), в т.ч. с ведущими нулями;
    - 16 слов LE (wb-mcu-fw-flasher): символ в младших байтах [0,2,4,6,8,10] или в старших [1,3,5,7,9,11];
    - 4 байта размер (LE) + 12 байт сигнатура (смещение 4..16);
    - запасной вариант: самая длинная подстрока из печатных ASCII в первых 24 байтах.
    """
    if len(data) < 12:
        return None
    candidates: List[bytes] = []
    # Сначала смещение 4 (формат WB: 4 байта размер LE + 12 байт сигнатура), иначе data[:12] может дать "Fk" из размера
    if len(data) >= 16:
        candidates.append(data[4:16])
    candidates.append(data[:12])
    # Слова LE (формат .wbfw WB): младшие байты [0,2,4,6,8,10]
    candidates.append(bytes(data[i] for i in range(0, min(12, len(data)), 2)))
    # Слова LE: старшие байты [1,3,5,7,9,11]
    if len(data) >= 12:
        candidates.append(bytes(data[i] for i in range(1, min(13, len(data)), 2)))
    for raw in candidates:
        s = _decode_signature_candidate(raw)
        if s and _is_printable_ascii_signature(s):
            return s
    # Запасной вариант: любая подстрока из 2+ печатных ASCII (разные форматы/смещения)
    fallback = _find_longest_printable_run(data, 24)
    if fallback and len(fallback) >= 2 and _is_printable_ascii_signature(fallback):
        return fallback
    return None


def signature_from_wb_filename(path: Optional[Path]) -> str:
    """
    Сигнатура для прошивки Wiren Board из имени файла: в начале до «__» или до первого «_».
    Примеры: ledGe__3.6.1_master.wbfw → ledGe; ledGE_3.6.1_master.wbfw → ledGE (до 12 символов).
    """
    if not path or path.suffix.lower() != ".wbfw":
        return "NONE"
    stem = path.stem.strip()
    if "__" in stem:
        sig = stem.split("__", 1)[0].strip()
    elif "_" in stem:
        sig = stem.split("_", 1)[0].strip()
    else:
        sig = stem
    sig = (sig or "NONE")[:12]
    return sig or "NONE"


def load_firmware_wbfw(path: Path) -> Tuple[bytes, int, str, Optional[str]]:
    """
    Загрузка прошивки Wiren Board (.wbfw): сырой файл (32 B info + блоки по 136 B, слова LE).
    Сигнатура извлекается из первых 32 байт при возможности; при открытии файла ошибку не выдаём.
    Проверка сигнатуры и несовпадение с устройством — только при прошивке выбранного устройства.
    Returns (image, size, version_string, signature_from_info_block or None).
    """
    data = path.read_bytes()
    if len(data) < 32:
        raise ValueError(f"Файл .wbfw слишком короткий: {len(data)} байт (минимум 32 для info-блока).")
    signature = signature_from_wb_filename(path)
    if not signature or signature == "NONE":
        signature = _extract_wbfw_signature(data[:32]) or "NONE"
    version = parse_version_from_filename(path.name) or "?"
    return data, len(data), version, signature


def validate_native_payload(raw: bytes, kind: str) -> None:
    boot = kind == "bootloader"
    valid, reason = check_bootloader_vector_table(raw) if boot else check_app_vector_table(raw)
    if not valid:
        raise ValueError(f"Неверная таблица векторов {kind}: {reason}")
    reset = struct.unpack_from("<I", raw, 4)[0] & ~1
    offset = (BL_VECTORS_SIZE + reset - BL_CODE_ORIGIN) if boot else reset - FLASH_APP_START
    if offset < 8 or offset + 2 > len(raw) or raw[offset:offset + 2] == b"\xff\xff":
        raise ValueError("Reset_Handler вне переданного кода образа.")
    first = struct.unpack_from("<H", raw, offset)[0]
    if first >> 11 in (0b11101, 0b11110, 0b11111) and offset + 4 > len(raw):
        raise ValueError("Reset_Handler указывает на неполную инструкцию Thumb.")


def load_identified_firmware(path: Path, *, expected_sha256: str = "", kind: Optional[str] = None) -> Tuple[bytes, int, str, Optional[str], Optional[str]]:
    """
    Load firmware from .fw, .bin or .wbfw (Wiren Board).
    Returns the wire image, size, version, signature and marked application family.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    suf = path.suffix.lower()
    if suf == ".wbfw":
        return (*load_firmware_wbfw(path), None)
    data = path.read_bytes()
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256.lower():
        raise FirmwareIdentityError("SHA-256 выбранного образа не совпадает с каталогом.")
    release = parse_known_firmware_filename(path.name)
    boot = kind == "bootloader" or (kind is None and release and release[1] == "bootloader")
    packed = suf == ".fw" and not (
        boot and len(data) == BL_IMAGE_TOTAL_BYTES and check_bootloader_vector_table(data)[0])
    if boot and not packed and suf in (".fw", ".bin"):
        if len(data) != BL_IMAGE_TOTAL_BYTES and len(data) < FLASH_END - 0x08000000:
            raise ValueError("Неверный размер образа загрузчика.")
        image = _bootloader_bin_image(data)
        size, signature, raw = len(image), None, image
    elif suf == ".bin":
        image, size = _bin_image(data)
        signature, raw = None, image
    elif suf == ".fw":
        image, size, signature = _fw_container(data)
        raw = _fw_payload(image, size)
    else:
        raise ValueError("Поддерживаются файлы .fw, .bin и .wbfw")
    if kind is not None:
        if packed:
            padding = image[FW_INFO_SIZE + ((size + 1) & ~1):]
            if len(padding) >= FW_BLOCK_BYTES or padding.strip(b"\xff"):
                raise ValueError("Недопустимое заполнение после содержимого .fw.")
        validate_native_payload(raw, kind)
        if packed and image[16:20] == b"CRC2" and zlib.crc32(raw) != struct.unpack_from("<I", image, 20)[0]:
            raise ValueError("CRC32 содержимого прошивки не совпадает с заголовком.")
        if boot and packed and size != BL_IMAGE_TOTAL_BYTES:
            if not BL_VECTORS_SIZE < size < BL_IMAGE_TOTAL_BYTES:
                raise ValueError("Неверный размер области кода загрузчика.")
            image = raw.ljust(BL_IMAGE_TOTAL_BYTES, b"\xff")
    version, family = application_metadata(path.name, data, decoded=raw, packed_header=packed)
    return image, size, version, signature, family


def load_firmware(path: Path) -> Tuple[bytes, int, str, Optional[str]]:
    image, size, version, signature, _ = load_identified_firmware(path)
    return image, size, version, signature


# В полном дампе Flash (objcopy -O binary без -j) код бутлоадера 0x08038000 идёт по смещению 0x38000.
BL_CODE_OFFSET_IN_FULL_BIN = 0x38000


def load_bootloader_bin(path: Path) -> bytes:
    """
    Загрузить образ бутлоадера из .bin.
    Два формата:
    1) 34 КБ (34816): [2 КБ векторы][32 КБ код] — как make_bootloader_fw.py / bootloader.bin.
    2) Полный дамп ≥256 КБ: векторы в 0..2047, код в 0x38000..0x38000+32767 (как objcopy -O binary из ELF с двумя регионами).
    Итог всегда ровно BL_IMAGE_TOTAL_BYTES (34816).
    """
    return _bootloader_bin_image(path.read_bytes())


def _bootloader_bin_image(data: bytes) -> bytes:
    if len(data) >= 0x40000:
        # Полный дамп: код по смещению 0x38000
        if len(data) < BL_CODE_OFFSET_IN_FULL_BIN + BL_CODE_SIZE:
            raise ValueError(
                f"Файл бутлоадера (полный дамп): требуется не менее {BL_CODE_OFFSET_IN_FULL_BIN + BL_CODE_SIZE} байт, получено {len(data)}."
            )
        vectors = data[0:BL_VECTORS_SIZE]
        code = data[BL_CODE_OFFSET_IN_FULL_BIN : BL_CODE_OFFSET_IN_FULL_BIN + BL_CODE_SIZE]
        image = vectors + code
        ok, err = check_bootloader_vector_table(image)
        if not ok:
            raise ValueError(f"Полный дамп: неверная таблица векторов: {err}")
        return image
    if len(data) < BL_IMAGE_TOTAL_BYTES:
        raise ValueError(
            f"Файл бутлоадера слишком короткий: {len(data)} байт, требуется не менее {BL_IMAGE_TOTAL_BYTES} "
            "(или полный дамп ≥256 КБ для авто-извлечения векторов и кода)."
        )
    image = data[:BL_IMAGE_TOTAL_BYTES]
    # Проверка: код (байты 2048..34815) не должен быть сплошь 0xFF (признак того, что взяли «дыру» полного дампа как 34 КБ).
    code_part = image[BL_VECTORS_SIZE:BL_IMAGE_TOTAL_BYTES]
    if code_part == b"\xff" * len(code_part):
        raise ValueError(
            "Образ бутлоадера: секция кода (32 КБ) пустая (0xFF). "
            "Используйте bootloader.fw / bootloader.bin из make (make_bootloader_fw.py) или полный .bin ≥256 КБ."
        )
    ok, err = check_bootloader_vector_table(image)
    if not ok:
        raise ValueError(f"Неверная таблица векторов в образе бутлоадера: {err}")
    return image


def check_bootloader_vector_table(image: bytes) -> Tuple[bool, Optional[str]]:
    """
    Проверить таблицу векторов образа бутлоадера (первые 8 байт: SP, Reset_Handler).
    Reset_Handler должен указывать в 0x08038000..0x0803FFFF (код бутлоадера), Thumb (LSB=1).
    """
    if len(image) < 8:
        return False, "образ короче 8 байт"
    sp, reset_handler = struct.unpack_from("<II", image, 0)
    if sp < RAM_START or sp > RAM_END:
        return False, f"SP=0x{sp:08X} не в RAM (0x20000000..0x20008000)"
    if (reset_handler & 0xFF000000) != 0x08000000:
        return False, f"Reset_Handler=0x{reset_handler:08X} не в Flash"
    if reset_handler < BL_CODE_ORIGIN or reset_handler >= (BL_CODE_ORIGIN + BL_CODE_SIZE):
        return False, (
            f"Reset_Handler=0x{reset_handler:08X} не в области кода бутлоадера 0x08038000..0x0803FFFF. "
            "Соберите образ через make (bootloader.fw / make_bootloader_fw.py)."
        )
    if (reset_handler & 1) == 0:
        return False, f"Reset_Handler=0x{reset_handler:08X} без бита Thumb (LSB=1)"
    if sp == 0xFFFFFFFF or reset_handler == 0xFFFFFFFF:
        return False, "таблица векторов пустая (0xFFFFFFFF)"
    return True, None


def load_bootloader_fw(path: Path) -> bytes:
    """
    Загрузить bootloader.fw.

    Поддерживаются два формата:
    1) сырой образ 34 КБ: 2 КБ векторы + 32 КБ код;
    2) контейнер make_fw.py: 32 B info + блоки по 246 B, где каждое 16-битное слово
       записано big-endian для Modbus-регистров.
    """
    data = path.read_bytes()
    if len(data) >= BL_IMAGE_TOTAL_BYTES:
        image = data[:BL_IMAGE_TOTAL_BYTES]
        ok, err = check_bootloader_vector_table(image)
        if ok:
            return image

    image = _load_bootloader_fw_container(data)
    ok, err = check_bootloader_vector_table(image)
    if not ok:
        raise ValueError(f"Неверная таблица векторов в bootloader.fw: {err}")
    return image


def _load_bootloader_fw_container(data: bytes) -> bytes:
    """Decode make_fw.py bootloader .fw into raw [vectors + code] image."""
    if len(data) < FW_INFO_SIZE:
        raise ValueError(
            f"Файл bootloader.fw слишком короткий: {len(data)} байт "
            f"(нужен сырой образ {BL_IMAGE_TOTAL_BYTES} байт или .fw с info-блоком)."
        )
    size = struct.unpack_from("<I", data, 12)[0]
    if size <= 0 or size > BL_IMAGE_TOTAL_BYTES:
        raise ValueError(
            f"В bootloader.fw указан размер {size} байт — ожидается 1..{BL_IMAGE_TOTAL_BYTES}."
        )
    encoded = data[FW_INFO_SIZE:]
    needed_encoded = ((size + 1) // 2) * 2
    if len(encoded) < needed_encoded:
        raise ValueError(
            f"bootloader.fw повреждён: данных {len(encoded)} байт, требуется не менее {needed_encoded}."
        )
    raw = bytearray()
    for i in range(0, needed_encoded, 2):
        w = (encoded[i] << 8) | encoded[i + 1]
        raw.append(w & 0xFF)
        raw.append((w >> 8) & 0xFF)
    return bytes(raw[:size]).ljust(BL_IMAGE_TOTAL_BYTES, b"\xff")


def load_bootloader_image(path: Path) -> bytes:
    """
    Загрузить образ бутлоадера из .fw или .bin (34 КБ: 2 КБ векторы + 32 КБ код).
    Returns image ровно BL_IMAGE_TOTAL_BYTES.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    suf = path.suffix.lower()
    if suf == ".bin":
        return load_bootloader_bin(path)
    if suf == ".fw":
        return load_bootloader_fw(path)
    raise ValueError("Образ бутлоадера: поддерживаются только .fw и .bin")


def find_firmware_files(app_dir: Path) -> List[Tuple[Path, str]]:
    """
    Ищет в app_dir файлы прошивки: .fw и .bin (версия из имени или «?»).
    Returns list of (path, version).
    """
    found: List[Tuple[Path, str]] = []
    for p in app_dir.iterdir():
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf not in (".fw", ".bin"):
            continue
        v = parse_version_from_filename(p.name)
        if v is not None:
            found.append((p, v))
        else:
            found.append((p, "?"))
    return found
