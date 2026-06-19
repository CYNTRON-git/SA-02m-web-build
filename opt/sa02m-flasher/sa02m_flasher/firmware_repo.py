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
from typing import Any, Dict, List, Optional, Set, Tuple

from . import firmware as fw_parser

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 15.0
USER_AGENT = "sa02m-flasher/1.0"
VALID_EXTENSIONS = {".fw", ".bin", ".elf"}
INDEX_CACHE_NAME = ".index.json"


# Полный дамп Flash / ELF / слишком большой .bin — не для Modbus-прошивальщика (см. firmware.load_firmware).
_FULL_BIN_MIN_BYTES = 0x40000


def is_flasher_supported_file(file_name: str, *, kind: str = "app", size: int = 0) -> bool:
    """
    Можно ли использовать файл в задаче прошивки (runner → load_firmware / load_bootloader).

    Отсекаются полные образы (*_full_*.bin), ELF, .wbfw и слишком большие артефакты приложения.
    """
    name = (file_name or "").strip()
    if not name:
        return False
    low = name.lower()
    suf = Path(name).suffix.lower()
    if suf == ".elf":
        return False
    if "_full_" in low or low.endswith("_full.bin"):
        return False
    k = (kind or "app").strip().lower()
    if k == "bootloader":
        return suf in (".fw", ".bin")
    if suf not in (".fw", ".bin"):
        return False
    sz = int(size or 0)
    if suf == ".bin" and sz >= _FULL_BIN_MIN_BYTES:
        return False
    if k == "app" and sz > fw_parser.MAX_FIRMWARE_SIZE:
        return False
    return True


def is_flasher_supported_entry(entry: FirmwareEntry) -> bool:
    return is_flasher_supported_file(
        entry.file,
        kind=entry.kind,
        size=entry.size,
    )


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


NO_INTERNET_USER_MSG = "Нет доступа к интернету"


def _network_exc_root(exc: Exception) -> Exception:
    """Развернуть цепочку __cause__ / __context__ до исходной сетевой ошибки."""
    seen: set[int] = set()
    cur: BaseException = exc
    while id(cur) not in seen:
        seen.add(id(cur))
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or not isinstance(nxt, Exception):
            break
        cur = nxt
    return cur  # type: ignore[return-value]


def _is_offline_network_error(exc: Exception) -> bool:
    """True, если ошибка типична для отсутствия интернета / DNS / маршрута."""
    root = _network_exc_root(exc)
    msg = str(root).strip().lower()
    if isinstance(root, urllib.error.HTTPError) and root.code in (502, 503, 504):
        return True
    if isinstance(root, urllib.error.URLError):
        reason = root.reason
        if isinstance(reason, OSError):
            if reason.errno in (-2, -3, -5, 101, 111, 113):
                return True
            rmsg = str(reason).lower()
            if any(
                token in rmsg
                for token in (
                    "temporary failure in name resolution",
                    "name or service not known",
                    "network is unreachable",
                    "no route to host",
                    "connection refused",
                    "connection timed out",
                )
            ):
                return True
        if isinstance(reason, str) and any(
            token in reason.lower() for token in ("timed out", "timeout")
        ):
            return True
    if any(
        token in msg
        for token in (
            "temporary failure in name resolution",
            "name or service not known",
            "network is unreachable",
            "no route to host",
            "connection refused",
            "connection timed out",
            "timed out",
            "timeout",
        )
    ):
        return True
    return False


def _format_network_error(exc: Exception, *, url: str = "") -> str:
    """Понятное сообщение об ошибке скачивания (RU) для UI и логов."""
    if _is_offline_network_error(exc):
        return NO_INTERNET_USER_MSG

    root = _network_exc_root(exc)
    msg = str(root).strip()
    low = msg.lower()

    if isinstance(root, urllib.error.HTTPError):
        code = int(getattr(root, "code", 0) or 0)
        if code == 404:
            return "Файл прошивки не найден на сервере"
        if code == 403:
            return "Доступ к серверу прошивок запрещён"
        if 400 <= code < 500:
            return f"Сервер прошивок отклонил запрос ({code})"
        if code >= 500:
            return f"Сервер прошивок временно недоступен ({code})"

    if isinstance(root, urllib.error.URLError):
        reason = root.reason
        if isinstance(reason, OSError) and reason.errno in (-2, -3):
            return NO_INTERNET_USER_MSG
        if "timed out" in low or "timeout" in low:
            return NO_INTERNET_USER_MSG

    if "temporary failure in name resolution" in low or "name or service not known" in low:
        return NO_INTERNET_USER_MSG

    if url:
        return f"Не удалось скачать прошивку с сервера"
    return msg or "Не удалось скачать прошивку"


def _http_get(url: str, *, timeout: float = HTTP_TIMEOUT_S, retries: int = 2) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, int(retries))):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", resp.getcode())
                if code != 200:
                    raise urllib.error.HTTPError(url, code, "non-200", resp.headers, None)
                return resp.read()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(_format_network_error(exc, url=url)) from exc
    assert last_exc is not None
    raise RuntimeError(_format_network_error(last_exc, url=url)) from last_exc


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

    def refresh(
        self,
        *,
        download: bool = False,
        keep_current: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Скачать и применить index.json.

        При download=True — скачать отсутствующие образы «текущей» (keep_current)
        и последней stable-версии (app + bootloader), затем удалить из кеша все
        остальные .fw/.bin/.elf (кроме .index.json и *.part).
        """
        status: Dict[str, Any] = {
            "ok": False,
            "error": "",
            "updated": "",
            "entries": 0,
            "download_errors": [],
            "purged": [],
        }
        try:
            raw = _http_get(self.manifest_url)
        except Exception as exc:
            self._manifest_error = _format_network_error(exc, url=self.manifest_url)
            status["error"] = self._manifest_error
            log.warning("Манифест недоступен: %s", self._manifest_error)
            return status
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            self._manifest_error = "Некорректный ответ сервера прошивок"
            status["error"] = self._manifest_error
            log.warning("Манифест: ошибка JSON: %s", exc)
            return status
        if not self._apply_manifest(data):
            self._manifest_error = "Некорректный формат списка прошивок"
            status["error"] = self._manifest_error
            return status
        self._manifest_error = ""
        self._last_refresh_ts = time.time()
        try:
            (self.cache_dir / INDEX_CACHE_NAME).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            log.exception("Не удалось сохранить кэш манифеста")
        if download:
            keep_versions = self._resolve_keep_versions(keep_current)
            status["keep_versions"] = {
                kind: sorted(vers) for kind, vers in keep_versions.items() if vers
            }
            download_errors = self._download_keep_versions(keep_versions)
            status["download_errors"] = download_errors
            if download_errors:
                status["error"] = "; ".join(download_errors)
            purged = self._purge_cache_except_versions(keep_versions)
            status["purged"] = purged
        self._consolidate_repository()
        with self._lock:
            status["ok"] = not download or not status["download_errors"]
            status["updated"] = self._manifest_updated
            status["entries"] = len(self._entries)
        return status

    def _apply_manifest(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        schema = data.get("schema", 1)
        if schema != 1:
            log.warning("Незнакомая схема манифеста %r, попытка всё равно прочитать", schema)
        self._manifest_updated = str(data.get("updated") or "")
        channels = data.get("channels") or {}
        if not isinstance(channels, dict):
            return False
        with self._lock:
            # Удалить старые manifest-записи (локальные/upload — сохранить).
            for key in list(self._entries.keys()):
                if self._entries[key].source == "manifest":
                    del self._entries[key]
            for channel, items in channels.items():
                if not isinstance(items, list):
                    return False
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    file_name = str(raw.get("file") or "").strip()
                    if not self._valid_manifest_file_name(file_name):
                        continue
                    kind_probe = str(raw.get("kind") or "").strip().lower()
                    if kind_probe in ("app", "bootloader"):
                        kind_pre = kind_probe
                    else:
                        kind_pre = _infer_kind_from_filename(file_name)
                    if not is_flasher_supported_file(
                        file_name,
                        kind=kind_pre,
                        size=int(raw.get("size") or 0),
                    ):
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
                    local = self._cache_path_for_entry(entry)
                    if local.is_file() and self._cached_file_valid(entry, local):
                        entry.downloaded = True
                        entry.local_path = str(local)
                        if not entry.size:
                            entry.size = local.stat().st_size
                    self._entries[(entry.channel, entry.file)] = entry
            self._consolidate_repository_locked()
        return True

    def _resolve_url(self, url_or_name: str) -> str:
        if url_or_name.startswith(("http://", "https://")):
            return url_or_name
        return urllib.parse.urljoin(self.firmware_base_url, url_or_name)

    @staticmethod
    def _valid_manifest_file_name(file_name: str) -> bool:
        if not file_name:
            return False
        p = Path(file_name)
        if p.is_absolute() or p.name != file_name:
            return False
        if file_name in (".", "..") or ".." in p.parts:
            return False
        return p.suffix.lower() in VALID_EXTENSIONS

    def _cache_path_for_entry(self, entry: FirmwareEntry) -> Path:
        if not self._valid_manifest_file_name(entry.file):
            raise ValueError(f"Недопустимое имя файла прошивки в манифесте: {entry.file}")
        return self.cache_dir / entry.file

    def _cached_file_valid(self, entry: FirmwareEntry, path: Path) -> bool:
        try:
            if entry.size and path.stat().st_size != int(entry.size):
                return False
            if entry.sha256 and _sha256_of(path).lower() != entry.sha256.lower():
                return False
        except OSError:
            return False
        return True

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
                try:
                    sz = path.stat().st_size
                except OSError:
                    continue
                kind_guess = _infer_kind_from_filename(path.name)
                if not is_flasher_supported_file(path.name, kind=kind_guess, size=sz):
                    try:
                        path.unlink()
                        log.info("Удалён неподдерживаемый файл прошивки из кеша: %s", path.name)
                    except OSError:
                        log.warning("Не удалось удалить %s", path, exc_info=True)
                    continue
                entry = self._entry_from_file(path, source="upload")
                self._entries[(entry.channel, entry.file)] = entry

    def _remove_entry_and_cache_file(self, entry: FirmwareEntry) -> None:
        """Убрать запись из индекса и удалить локальный файл (если есть)."""
        with self._lock:
            self._entries.pop((entry.channel, entry.file), None)
        path = self.cache_dir / entry.file
        if path.is_file():
            try:
                path.unlink()
                log.info("Удалён файл прошивки из кеша: %s", entry.file)
            except OSError:
                log.warning("Не удалось удалить %s", path, exc_info=True)
        part = path.with_suffix(path.suffix + ".part")
        if part.is_file():
            part.unlink(missing_ok=True)

    def _consolidate_repository_locked(self) -> None:
        """Удалить из кеша артефакты, непригодные для прошивальщика."""
        to_drop: List[FirmwareEntry] = []
        for entry in list(self._entries.values()):
            if not is_flasher_supported_entry(entry):
                to_drop.append(entry)
        for entry in to_drop:
            self._remove_entry_and_cache_file(entry)

    @staticmethod
    def _normalize_keep_version(version: str) -> str:
        v = (version or "").strip()
        if not v or v == "?":
            return ""
        return v

    def _resolve_keep_versions(
        self,
        keep_current: Optional[Dict[str, str]],
    ) -> Dict[str, Set[str]]:
        """
        Версии, которые должны остаться в локальном кеше: latest stable + текущие с линии.
        """
        keep: Dict[str, Set[str]] = {"app": set(), "bootloader": set()}
        latest_app = self._normalize_keep_version(self.latest_stable_version())
        latest_bl = self._normalize_keep_version(self.latest_bootloader_version())
        if latest_app:
            keep["app"].add(latest_app)
        if latest_bl:
            keep["bootloader"].add(latest_bl)
        if keep_current:
            cur_app = self._normalize_keep_version(str(keep_current.get("app") or ""))
            cur_bl = self._normalize_keep_version(str(keep_current.get("bootloader") or ""))
            if cur_app:
                keep["app"].add(cur_app)
            if cur_bl:
                keep["bootloader"].add(cur_bl)
        return keep

    def _stable_entries_for_version(self, kind: str, version: str) -> List[FirmwareEntry]:
        """Manifest-записи stable с заданным kind и version."""
        target = self._normalize_keep_version(version)
        if not target:
            return []
        out: List[FirmwareEntry] = []
        with self._lock:
            for entry in self._entries.values():
                if (
                    entry.channel == "stable"
                    and entry.source == "manifest"
                    and entry.kind == kind
                    and self._normalize_keep_version(entry.version) == target
                ):
                    out.append(entry)
        return out

    def _download_keep_versions(self, keep_versions: Dict[str, Set[str]]) -> List[str]:
        """Скачать отсутствующие образы из keep_versions; вернуть список ошибок (RU)."""
        errors: List[str] = []
        seen: Set[Tuple[str, str]] = set()
        for kind in ("app", "bootloader"):
            for ver in sorted(keep_versions.get(kind) or ()):
                for entry in self._stable_entries_for_version(kind, ver):
                    key = (entry.channel, entry.file)
                    if key in seen:
                        continue
                    seen.add(key)
                    if entry.downloaded and self.path_for(entry):
                        continue
                    try:
                        self.download(entry)
                    except Exception as exc:
                        log.exception("Ошибка скачивания %s", entry.file)
                        label = "приложения" if kind == "app" else "бутлоадера"
                        detail = _format_network_error(exc, url=entry.url)
                        errors.append(
                            f"Не удалось скачать {label} v{ver} ({entry.file}): {detail}"
                        )
        return errors

    def _purge_cache_except_versions(self, keep_versions: Dict[str, Set[str]]) -> List[str]:
        """
        Удалить локальные .fw/.bin/.elf, версия которых не входит в keep_versions.
        Записи манифеста сохраняются (downloaded=False).
        """
        purged: List[str] = []

        def _should_keep(entry: FirmwareEntry) -> bool:
            kind = entry.kind if entry.kind in ("app", "bootloader") else "app"
            ver = self._normalize_keep_version(entry.version)
            if not ver:
                return False
            return ver in (keep_versions.get(kind) or set())

        with self._lock:
            for entry in list(self._entries.values()):
                path = self.cache_dir / entry.file
                if not path.is_file():
                    entry.downloaded = False
                    entry.local_path = None
                    continue
                if _should_keep(entry):
                    continue
                try:
                    path.unlink()
                    log.info("Удалён из кеша (не current/latest): %s", entry.file)
                    purged.append(entry.file)
                except OSError:
                    log.warning("Не удалось удалить %s", path, exc_info=True)
                part = path.with_suffix(path.suffix + ".part")
                part.unlink(missing_ok=True)
                entry.downloaded = False
                entry.local_path = None

            known = {e.file for e in self._entries.values()}
            for path in self.cache_dir.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in VALID_EXTENSIONS:
                    continue
                if path.name.endswith(".part"):
                    continue
                if path.name in known:
                    continue
                entry = self._entry_from_file(path, source="upload")
                if _should_keep(entry):
                    self._entries[(entry.channel, entry.file)] = entry
                    continue
                try:
                    path.unlink()
                    log.info("Удалён неучтённый файл из кеша: %s", path.name)
                    purged.append(path.name)
                except OSError:
                    log.warning("Не удалось удалить %s", path, exc_info=True)
        return purged

    def _consolidate_repository(self) -> None:
        with self._lock:
            self._consolidate_repository_locked()

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
        self._consolidate_repository()
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
            local_path = Path(entry.local_path)
            if self._cached_file_valid(entry, local_path):
                return local_path
            entry.local_path = None
            entry.downloaded = False
        local = self._cache_path_for_entry(entry)
        if local.is_file() and self._cached_file_valid(entry, local):
            entry.local_path = str(local)
            entry.downloaded = True
            return local
        entry.local_path = None
        entry.downloaded = False
        return None

    def ensure_local_path(self, entry: FirmwareEntry, *, allow_download: bool = True) -> Path:
        """
        Вернуть локальный путь к образу. При отсутствии файла — скачать (если allow_download).
        Исключение — понятное сообщение на русском (DNS/офлайн/ручная загрузка).
        """
        path = self.path_for(entry)
        if path is not None:
            return path
        if not allow_download:
            raise FileNotFoundError(
                f"Файл {entry.file} не скачан в кеш шлюза. "
                "Нажмите «Скачать прошивки», выберите другой образ или загрузите .fw вручную."
            )
        return self.download(entry)

    def download(self, entry: FirmwareEntry) -> Path:
        """Скачать файл прошивки в кеш с проверкой sha256 (если указана)."""
        if not entry.url:
            raise RuntimeError(f"У записи {entry.file} не указан URL")
        target = self._cache_path_for_entry(entry)
        tmp = target.with_suffix(target.suffix + ".part")
        log.info("Скачиваю %s → %s", entry.url, target)
        try:
            raw = _http_get(entry.url, timeout=HTTP_TIMEOUT_S * 4)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(_format_network_error(exc, url=entry.url)) from exc
        tmp.write_bytes(raw)
        if not self._cached_file_valid(entry, tmp):
            got = hashlib.sha256(raw).hexdigest() if entry.sha256 else f"размер {tmp.stat().st_size}"
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Sha256/размер не совпадает: ожидался {entry.sha256 or entry.size}, получено {got}")
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
        kind_guess = _infer_kind_from_filename(safe)
        if not is_flasher_supported_file(safe, kind=kind_guess, size=len(data)):
            raise ValueError(
                f"Файл {filename} не поддерживается прошивальщиком "
                "(нужен .fw/.bin приложения или бутлоадера, не полный дамп *_full_* и не .elf)."
            )
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
        self._consolidate_repository()
        log.info("Загружена прошивка %s (sig=%s, size=%d)", entry.file, entry.signatures, entry.size)
        return entry
