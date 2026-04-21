# -*- coding: utf-8 -*-
"""
Load firmware from .fw or .bin.
.fw: bootloader format — info 32 B (sig 12 + size 4 LE + pad) + data blocks (246 B each as 123 big-endian words).
.bin: raw image for 0x08000800 (FLASH_APP_START).
"""
import re
import struct
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


# Байты заполнения линкера (хвост образа отбрасываем при расчёте эффективного размера)
_FILL_BYTES = frozenset((0xFF, 0x00, 0xA5, 0xCD))


def _effective_size(image: bytes) -> int:
    """
    Размер образа без хвоста из типичных заполнителей (0xFF, 0x00, 0xA5, 0xCD).
    Выравнивание по 4 байта для записи в Flash.
    """
    if not image:
        return 0
    last = len(image) - 1
    while last >= 0 and image[last] in _FILL_BYTES:
        last -= 1
    used = last + 1
    if used == 0:
        return len(image)  # не обрезать до 0
    return (used + 3) & ~3


def parse_version_from_filename(filename: str) -> Optional[str]:
    """
    Извлекает версию из имени файла MR-02m_<version>.fw / .bin.
    Поддерживаются 1–4 компонента: 1, 1.0, 1.0.0, 1.0.0.0 (недостающие дополняются нулями до X.Y.Z.W).
    """
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
        return ver if re.match(r"^[\d.]+$", ver) else "?"
    # .fw: mp02m_1.0.0.fw, MR-02m_1.0.0.0.fw
    m = re.match(r"(?:MR-02m|mp02m|mp-02m)_([\d.]+)\.fw$", filename, re.I)
    if m:
        return m.group(1)
    m = re.match(r"(?:MR-02m|mp02m|mp-02m)_(.+)\.fw$", filename, re.I)
    if m:
        ver = m.group(1).strip()
        return ver if re.match(r"^[\d.]+$", ver) else "?"
    return None


ELF_MAGIC_LE = 0x7F454C46  # b'\x7fELF' как uint32 LE — не считать версией

# Строка версии в прошивке AppBoot:
#   Файл: Core/Src/version_string.c (const firmware_version_elf[]).
#   В образе: ASCII "MR02M_VER:X.Y.Z.W\0" в секции .rodata (попадает в ELF и во Flash-образ).
#   Поиск: байты MR02M_VER: (0x4D 0x52 0x30 0x32 0x4D 0x5F 0x56 0x45 0x52 0x3A), затем версия до '\0'.
FW_VERSION_PREFIX = b"MR02M_VER:"


def _parse_version_mr02m_prefix(image: bytes) -> Optional[str]:
    """
    Ищет в образе/ELF строку версии прошивки AppBoot: префикс MR02M_VER:, затем X.Y.Z.W.
    Место хранения: const firmware_version_elf[] в Core/Src/version_string.c, секция .rodata.
    """
    idx = image.find(FW_VERSION_PREFIX)
    if idx < 0:
        return None
    start = idx + len(FW_VERSION_PREFIX)
    if start >= len(image):
        return None
    end = start
    while end < len(image) and image[end] in b"0123456789.-":
        end += 1
    if end == start:
        return None
    try:
        return image[start:end].decode("ascii")
    except Exception:
        return None


def _parse_version_from_u32_le(image: bytes) -> Optional[str]:
    """
    Ищет в образе 32-битное значение (LE) версии MAJOR.MINOR.PATCH.SUFFIX.
    Принимаем только однозначные компоненты (0–9), чтобы не брать посторонние константы
    (типа 8.0.80.9 из кода). Пропускаем первые 64 байта (векторная таблица).
    """
    if len(image) < 4:
        return None
    search_len = min(len(image), 64 * 1024)
    start = 64  # пропуск векторной таблицы и начала кода

    def check(off: int) -> Optional[str]:
        if off + 4 > len(image):
            return None
        v = struct.unpack_from("<I", image, off)[0]
        if v == ELF_MAGIC_LE:
            return None
        major = (v >> 24) & 0xFF
        minor = (v >> 16) & 0xFF
        patch = (v >> 8) & 0xFF
        suffix = v & 0xFF
        if 1 <= major <= 9 and minor <= 9 and patch <= 9 and suffix <= 9:
            return f"{major}.{minor}.{patch}.{suffix}"
        return None

    for off in range(start, search_len - 3, 4):
        r = check(off)
        if r:
            return r
    for off in range(max(start, 2), min(search_len - 3, start + 1024), 2):
        r = check(off)
        if r:
            return r
    return None


def parse_version_from_image(
    image: bytes, *, search_u32: bool = True
) -> Optional[str]:
    """
    Ищет в образе прошивки версию: MR02M_VER:X.Y.Z.W (AppBoot), затем строку N.N.N.N или 32-битную константу.
    search_u32: искать ли 32-битную константу (для сырого ELF не используем — много ложных срабатываний).
    """
    if len(image) < len(FW_VERSION_PREFIX):
        return None
    # 0) Строка версии AppBoot из version_string.c (префикс MR02M_VER: в .rodata)
    v = _parse_version_mr02m_prefix(image)
    if v:
        return v
    # 1) Строка вида N.N.N.N (1–3 цифры на компонент)
    pattern = re.compile(rb"(?<!\d)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?!\d)")
    search_len = min(len(image), 32 * 1024)
    m = pattern.search(image[:search_len])
    if m:
        try:
            return m.group(1).decode("ascii")
        except Exception:
            pass
    if len(image) > 512:
        m = pattern.search(image[-512:])
        if m:
            try:
                return m.group(1).decode("ascii")
            except Exception:
                pass
    if search_u32:
        return _parse_version_from_u32_le(image)
    return None


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
    data = path.read_bytes()
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


def load_fw(path: Path) -> Tuple[bytes, int, str, str]:
    """
    Загрузка .fw (формат бутлоадера). Возвращаем файл как есть — первые 32 байта пойдут в info-блок (BE), далее блоки по 246 B.
    Format: 32 B info (sig 12 ASCII + size 4 LE + pad) + data as 123 big-endian 16-bit words per block (246 B).
    Returns (full_file_bytes, size, version_from_filename, signature_from_file).
    """
    data = path.read_bytes()
    if len(data) < FW_INFO_SIZE:
        raise ValueError("Файл .fw слишком короткий (нет info-блока 32 байт)")
    sig_raw = data[0:12]
    signature = sig_raw.rstrip(b"\x00").decode("ascii", errors="replace")
    if not signature or any(ord(c) < 32 for c in signature):
        signature = "NONE"
    size = struct.unpack_from("<I", data, 12)[0]
    if size <= 0 or size > MAX_FIRMWARE_SIZE:
        hint = ""
        try:
            if path.stat().st_size == BL_IMAGE_TOTAL_BYTES:
                hint = " Размер файла 34 КБ — похоже на образ бутлоадера: выберите режим «Прошивка бутлоадера» и укажите этот файл там."
        except OSError:
            pass
        raise ValueError(
            f"В .fw указан размер {size} байт — недопустимо (ожидается 1..{MAX_FIRMWARE_SIZE}). "
            "Возможно, выбран не тот файл (не .fw приложения) или файл повреждён. "
            "Используйте .fw, собранный make_fw.py / скриптом прошивки приложения."
            + hint
        )
    version = parse_version_from_filename(path.name)
    if not version or version == "?":
        payload_part = data[FW_INFO_SIZE : FW_INFO_SIZE + min(size, len(data) - FW_INFO_SIZE)]
        version = _parse_version_mr02m_prefix(payload_part) or parse_version_from_image(
            payload_part, search_u32=False
        ) or "?"
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


def load_firmware(path: Path) -> Tuple[bytes, int, str, Optional[str]]:
    """
    Load firmware from .fw, .bin or .wbfw (Wiren Board).
    Returns (image, size, version_string, signature_override).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    suf = path.suffix.lower()
    if suf == ".bin":
        image, size = load_bin(path)
        v_fn = parse_version_from_filename(path.name)
        v_img = parse_version_from_image(image)
        version = v_fn or v_img or "?"
        return image, size, version, None
    if suf == ".fw":
        image, size, version, signature = load_fw(path)
        return image, size, version, signature
    if suf == ".wbfw":
        return load_firmware_wbfw(path)
    raise ValueError("Поддерживаются файлы .fw, .bin и .wbfw (Wiren Board)")


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
    data = path.read_bytes()
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
    Загрузить bootloader.fw (сырой образ 34 КБ: 2 КБ векторы + 32 КБ код).
    Формат: как make_bootloader_fw.py — без заголовка, ровно BL_IMAGE_TOTAL_BYTES.
    """
    data = path.read_bytes()
    if len(data) < BL_IMAGE_TOTAL_BYTES:
        raise ValueError(
            f"Файл bootloader.fw слишком короткий: {len(data)} байт, требуется {BL_IMAGE_TOTAL_BYTES}."
        )
    image = data[:BL_IMAGE_TOTAL_BYTES]
    ok, err = check_bootloader_vector_table(image)
    if not ok:
        raise ValueError(f"Неверная таблица векторов в bootloader.fw: {err}")
    return image


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
