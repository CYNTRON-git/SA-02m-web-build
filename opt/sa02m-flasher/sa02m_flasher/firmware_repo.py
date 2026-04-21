# -*- coding: utf-8 -*-
"""
Репозиторий прошивок MR-02м.

Источники (приоритет):
  1. index.json по адресу https://cyntron.ru/upload/medialibrary/cyntron/firmware/index.json
     Схема (v1):
         {
           "schema": 1,
           "updated": "YYYY-MM-DD",
           "channels": {
             "stable": [ {file, version, signatures[], device, size, sha256, released, notes}, ... ],
             "beta":   [ ... ]
           }
         }
     Один образ прошивки на все варианты MR-02м: поле ``signatures`` опционально (метаданные);
     подбор «какая прошивка новее» — по ``version``, а не по сигнатуре модуля.
  2. Ручная загрузка через UI (POST /firmware/upload) — файл парсится (signature/версия берутся
     из info-блока .fw или имени MR-02m_<ver>.{fw,bin,elf}).

Локальный кеш:
  /var/lib/sa02m-flasher/firmware/        — файлы .fw/.bin/.elf
  /var/lib/sa02m-flasher/firmware/.index.json  — кэш последнего успешно скачанного манифеста
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import firmware as fw_parser

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 15.0
USER_AGENT = "sa02m-flasher/1.0"
VALID_EXTENSIONS = {".fw", ".bin", ".elf"}
INDEX_CACHE_NAME = ".index.json"


def _infer_kind_from_filename(file_name: str) -> str:
    """
    Классификация артефакта для сравнения версий с модулем.

    Явное поле ``kind`` в манифесте предпочтительнее; иначе — по имени файла.
    """
    n = (file_name or "").lower()
    if "mr-02m_bootloader" in n or n.endswith("_bootloader.fw") or n == "bootloader.fw":
        return "bootloader"
    return "app"


def version_tuple(version: str) -> Optional[Tuple[int, int, int, int]]:
    """
    Разбор версии X.Y.Z.W для сравнения (только цифровые компоненты, до четырёх).
    «1.2» → (1, 2, 0, 0). Некорректная строка → None.
    """
    version = (version or "").strip()
    if not version or version == "?":
        return None
    parts: List[int] = []
    for seg in version.split(".")[:4]:
        if not seg.isdigit():
            return None
        parts.append(int(seg))
    if not parts:
        return None
    while len(parts) < 4:
        parts.append(0)
    return (parts[0], parts[1], parts[2], parts[3])


@dataclass
class FirmwareEntry:
    file: str                       # имя файла
    version: str                    # X.Y.Z.W
    signatures: List[str] = field(default_factory=list)  # допустимые сигнатуры устройств
    device: str = "MR-02m"
    size: int = 0
    sha256: str = ""
    released: str = ""
    notes: str = ""
    channel: str = "stable"
    kind: str = "app"               # app | bootloader — для latest_* и подсказки в UI
    url: str = ""                   # абсолютный URL (resolved)
    downloaded: bool = False        # файл есть в локальном кеше
    local_path: Optional[str] = None
    source: str = "manifest"        # manifest | upload | unknown

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["local_path"] = str(self.local_path) if self.local_path else None
        return d


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_get(url: str, *, timeout: float = HTTP_TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = getattr(resp, "status", resp.getcode())
        if code != 200:
            raise urllib.error.HTTPError(url, code, "non-200", resp.headers, None)
        return resp.read()


class FirmwareRepo:
    """
    Репозиторий прошивок с потокобезопасным доступом.

    Методы:
        refresh(download=False) — обновить манифест (и при необходимости скачать файлы).
        list_entries()          — все известные записи (манифест + локальные).
        download(entry)         — принудительно скачать файл под запись.
        find_for_signature(sig) — устаревшее имя: возвращает все записи (образ общий для линейки).
        version_tuple / latest_stable_version / latest_bootloader_version — подсказка «есть обновление».
        add_upload(data, name)  — добавить .fw/.bin/.elf из UI (копирует в кеш).
        path_for(entry)         — локальный путь к файлу (или None).
    """

    def __init__(
        self,
        cache_dir: Path,
        manifest_url: str,
        firmware_base_url: str,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.manifest_url = manifest_url
        self.firmware_base_url = firmware_base_url.rstrip("/") + "/"
        self._lock = threading.RLock()
        self._entries: Dict[Tuple[str, str], FirmwareEntry] = {}   # (channel, file) → entry
        self._manifest_updated: str = ""
        self._manifest_error: str = ""
        self._last_refresh_ts: float = 0.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._load_cached_manifest()
        self._scan_local_files()

    # ─── Манифест ─────────────────────────────────────────────────────────────

    def _load_cached_manifest(self) -> None:
        path = self.cache_dir / INDEX_CACHE_NAME
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._apply_manifest(data)
        except Exception:
            log.exception("Не удалось прочитать закэшированный манифест %s", path)

    def refresh(self, *, download: bool = False) -> Dict[str, Any]:
        """Скачать и применить index.json. При download=True — дополнительно скачать файлы."""
        status = {"ok": False, "error": "", "updated": "", "entries": 0}
        try:
            raw = _http_get(self.manifest_url)
        except Exception as exc:
            self._manifest_error = f"{type(exc).__name__}: {exc}"
            status["error"] = self._manifest_error
            log.warning("Манифест недоступен: %s", self._manifest_error)
            return status
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            self._manifest_error = f"JSON: {exc}"
            status["error"] = self._manifest_error
            return status
        self._apply_manifest(data)
        self._manifest_error = ""
        self._last_refresh_ts = time.time()
        try:
            (self.cache_dir / INDEX_CACHE_NAME).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            log.exception("Не удалось сохранить кэш манифеста")
        if download:
            with self._lock:
                entries = list(self._entries.values())
            for e in entries:
                if e.source != "manifest":
                    continue
                if not e.downloaded:
                    try:
                        self.download(e)
                    except Exception:
                        log.exception("Ошибка скачивания %s", e.file)
        with self._lock:
            status["ok"] = True
            status["updated"] = self._manifest_updated
            status["entries"] = len(self._entries)
        return status

    def _apply_manifest(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        schema = data.get("schema", 1)
        if schema != 1:
            log.warning("Незнакомая схема манифеста %r, попытка всё равно прочитать", schema)
        self._manifest_updated = str(data.get("updated") or "")
        channels = data.get("channels") or {}
        if not isinstance(channels, dict):
            return
        with self._lock:
            # Удалить старые manifest-записи (локальные/upload — сохранить).
            for key in list(self._entries.keys()):
                if self._entries[key].source == "manifest":
                    del self._entries[key]
            for channel, items in channels.items():
                if not isinstance(items, list):
                    continue
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    file_name = str(raw.get("file") or "").strip()
                    if not file_name:
                        continue
                    signatures = raw.get("signatures") or []
                    if not isinstance(signatures, list):
                        signatures = [str(signatures)]
                    kind_raw = str(raw.get("kind") or "").strip().lower()
                    if kind_raw in ("app", "bootloader"):
                        kind = kind_raw
                    else:
                        kind = _infer_kind_from_filename(file_name)
                    entry = FirmwareEntry(
                        file=file_name,
                        version=str(raw.get("version") or "?"),
                        signatures=[str(s) for s in signatures if s],
                        device=str(raw.get("device") or "MR-02m"),
                        size=int(raw.get("size") or 0),
                        sha256=str(raw.get("sha256") or "").lower(),
                        released=str(raw.get("released") or ""),
                        notes=str(raw.get("notes") or ""),
                        channel=str(channel),
                        kind=kind,
                        url=self._resolve_url(str(raw.get("url") or file_name)),
                        source="manifest",
                    )
                    local = self.cache_dir / entry.file
                    if local.is_file():
                        entry.downloaded = True
                        entry.local_path = str(local)
                        if not entry.size:
                            entry.size = local.stat().st_size
                    self._entries[(entry.channel, entry.file)] = entry

    def _resolve_url(self, url_or_name: str) -> str:
        if url_or_name.startswith(("http://", "https://")):
            return url_or_name
        return urllib.parse.urljoin(self.firmware_base_url, url_or_name)

    # ─── Локальные файлы ──────────────────────────────────────────────────────

    def _scan_local_files(self) -> None:
        """Подхватить файлы в cache_dir, которые не описаны манифестом (ручная загрузка)."""
        with self._lock:
            known = {e.file for e in self._entries.values()}
            for path in self.cache_dir.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in VALID_EXTENSIONS:
                    continue
                if path.name in known:
                    continue
                entry = self._entry_from_file(path, source="upload")
                self._entries[(entry.channel, entry.file)] = entry

    def _entry_from_file(self, path: Path, *, source: str = "upload") -> FirmwareEntry:
        """Построить запись из локального файла. Для .fw читаем сигнатуру и версию из info-блока."""
        version = fw_parser.parse_version_from_filename(path.name) or "?"
        signatures: List[str] = []
        try:
            if path.suffix.lower() == ".fw":
                _, _, ver, sig = fw_parser.load_fw(path)
                if ver and ver != "?":
                    version = ver
                if sig and sig != "NONE":
                    signatures = [sig]
        except Exception:
            log.exception("Не удалось разобрать .fw %s", path)
        size = path.stat().st_size
        kind = _infer_kind_from_filename(path.name)
        return FirmwareEntry(
            file=path.name,
            version=version,
            signatures=signatures,
            device="MR-02m",
            size=size,
            sha256=_sha256_of(path),
            channel="local",
            kind=kind,
            source=source,
            downloaded=True,
            local_path=str(path),
            url="",
        )

    # ─── Публичные методы ─────────────────────────────────────────────────────

    def list_entries(self) -> List[FirmwareEntry]:
        self._scan_local_files()
        with self._lock:
            items = list(self._entries.values())
        items.sort(key=lambda e: (e.channel != "stable", e.file))
        return items

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "manifest_url": self.manifest_url,
                "manifest_updated": self._manifest_updated,
                "manifest_error": self._manifest_error,
                "last_refresh_ts": self._last_refresh_ts,
                "latest_stable_version": self.latest_stable_version(),
                "latest_bootloader_version": self.latest_bootloader_version(),
                "entries": [e.to_dict() for e in self.list_entries()],
            }

    def _latest_version_for_kind(self, kind: str) -> str:
        """Наибольшая ``version`` среди manifest-записей ``stable`` с заданным ``kind``."""
        best: Optional[Tuple[int, int, int, int]] = None
        best_raw = ""
        with self._lock:
            candidates = [
                e
                for e in self._entries.values()
                if e.channel == "stable" and e.source == "manifest" and e.kind == kind
            ]
        for e in candidates:
            t = version_tuple(e.version)
            if t is None:
                continue
            if best is None or t > best:
                best = t
                best_raw = str(e.version).strip()
        return best_raw

    def latest_stable_version(self) -> str:
        """Наибольшая версия приложения (``kind`` = app) в канале ``stable`` манифеста."""
        return self._latest_version_for_kind("app")

    def latest_bootloader_version(self) -> str:
        """Наибольшая версия образа бутлоадера (``kind`` = bootloader) в канале ``stable`` манифеста."""
        return self._latest_version_for_kind("bootloader")

    def find_for_signature(self, signature: str) -> List[FirmwareEntry]:
        """
        Вернуть все записи репозитория.

        Один файл прошивки на все варианты MR-02м: отбор по полю ``signatures`` в манифесте
        не выполняется (аргумент ``signature`` игнорируется — имя метода сохранено для совместимости).
        """
        return self.list_entries()

    def get(self, channel: str, file: str) -> Optional[FirmwareEntry]:
        with self._lock:
            e = self._entries.get((channel, file))
        if e is None and channel != "local":
            # Допускаем поиск по имени без указания канала.
            for key, entry in self._entries.items():
                if entry.file == file:
                    return entry
        return e

    def path_for(self, entry: FirmwareEntry) -> Optional[Path]:
        if entry.local_path and Path(entry.local_path).is_file():
            return Path(entry.local_path)
        local = self.cache_dir / entry.file
        if local.is_file():
            entry.local_path = str(local)
            entry.downloaded = True
            return local
        return None

    def download(self, entry: FirmwareEntry) -> Path:
        """Скачать файл прошивки в кеш с проверкой sha256 (если указана)."""
        if not entry.url:
            raise RuntimeError(f"У записи {entry.file} не указан URL")
        target = self.cache_dir / entry.file
        tmp = target.with_suffix(target.suffix + ".part")
        log.info("Скачиваю %s → %s", entry.url, target)
        raw = _http_get(entry.url, timeout=HTTP_TIMEOUT_S * 4)
        tmp.write_bytes(raw)
        if entry.sha256:
            got = hashlib.sha256(raw).hexdigest()
            if got.lower() != entry.sha256.lower():
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"Sha256 не совпадает: ожидался {entry.sha256}, получено {got}")
        tmp.replace(target)
        entry.downloaded = True
        entry.local_path = str(target)
        if not entry.size:
            entry.size = target.stat().st_size
        return target

    # ─── Загрузка файла через UI ──────────────────────────────────────────────

    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

    def add_upload(self, data: bytes, filename: str) -> FirmwareEntry:
        if not data:
            raise ValueError("Пустой файл прошивки")
        safe = self._SAFE_NAME_RE.sub("_", filename).strip("._-") or "upload.fw"
        if not any(safe.lower().endswith(ext) for ext in VALID_EXTENSIONS):
            raise ValueError(f"Недопустимое расширение: {filename} (допустимо: {sorted(VALID_EXTENSIONS)})")
        target = self.cache_dir / safe
        i = 1
        base = Path(safe).stem
        suffix = Path(safe).suffix
        while target.exists():
            i += 1
            target = self.cache_dir / f"{base}.{i}{suffix}"
        target.write_bytes(data)
        entry = self._entry_from_file(target, source="upload")
        with self._lock:
            self._entries[(entry.channel, entry.file)] = entry
        log.info("Загружена прошивка %s (sig=%s, size=%d)", entry.file, entry.signatures, entry.size)
        return entry
