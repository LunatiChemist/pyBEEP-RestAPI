import json
import pathlib
import re
import threading
from typing import Dict, NamedTuple, Optional

from fastapi import HTTPException

PATH_SEGMENT_RE = re.compile(r"[^0-9A-Za-z_-]+")
CLIENT_DATETIME_RE = re.compile(r"[^0-9A-Za-zT_-]+")


class RunStorageInfo(NamedTuple):
    experiment: str
    subdir: Optional[str]
    timestamp_dir: str
    timestamp_name: str
    filename_prefix: str


RUN_DIRECTORY_LOCK = threading.Lock()
RUN_DIRECTORIES: Dict[str, pathlib.Path] = {}
_RUNS_ROOT: Optional[pathlib.Path] = None


def configure_runs_root(root: pathlib.Path) -> None:
    """Configure the base directory used for run storage helpers."""
    global _RUNS_ROOT
    _RUNS_ROOT = root
    with RUN_DIRECTORY_LOCK:
        RUN_DIRECTORIES.clear()
        stored = _load_run_index_unlocked()
        for rid, rel in stored.items():
            candidate = root / pathlib.Path(rel)
            if candidate.is_dir():
                RUN_DIRECTORIES[rid] = candidate


def run_index_path() -> pathlib.Path:
    """Return the path of the on-disk run directory index."""
    root = _require_root()
    return root / "_run_paths.json"


def value_or_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed if trimmed else None


def sanitize_path_segment(raw: str, field_name: str) -> str:
    trimmed = (raw or "").strip()
    if not trimmed:
        raise HTTPException(400, f"{field_name} darf nicht leer sein")
    sanitized = PATH_SEGMENT_RE.sub("_", trimmed)
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)
    sanitized = sanitized.strip("_-")
    if not sanitized:
        raise HTTPException(400, f"{field_name} ist ungueltig")
    return sanitized


def sanitize_optional_segment(value: Optional[str]) -> Optional[str]:
    candidate = value_or_none(value)
    if candidate is None:
        return None
    return sanitize_path_segment(candidate, "subdir")


def sanitize_client_datetime(raw: str) -> str:
    trimmed = (raw or "").strip()
    if not trimmed:
        raise HTTPException(400, "client_datetime darf nicht leer sein")
    normalized = (
        trimmed.replace(":", "-")
        .replace(" ", "_")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(".", "-")
    )
    sanitized = CLIENT_DATETIME_RE.sub("-", normalized)
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    sanitized = re.sub(r"_{2,}", "_", sanitized)
    sanitized = sanitized.strip("_-")
    if not sanitized:
        raise HTTPException(400, "client_datetime ist ungueltig")
    return sanitized


def record_run_directory(run_id: str, run_dir: pathlib.Path) -> None:
    """Persist the mapping between run_id and its output directory."""
    try:
        rel = run_dir.relative_to(_require_root())
    except ValueError:
        rel = run_dir
    rel_str = rel.as_posix()
    with RUN_DIRECTORY_LOCK:
        RUN_DIRECTORIES[run_id] = run_dir
        data = _load_run_index_unlocked()
        data[run_id] = rel_str
        _write_run_index_unlocked(data)


def forget_run_directory(run_id: str) -> None:
    """Remove a run directory mapping from memory and disk."""
    with RUN_DIRECTORY_LOCK:
        RUN_DIRECTORIES.pop(run_id, None)
        data = _load_run_index_unlocked()
        if run_id in data:
            data.pop(run_id, None)
            if data:
                _write_run_index_unlocked(data)
            else:
                path = run_index_path()
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def resolve_run_directory(run_id: str) -> pathlib.Path:
    """Return the directory for a run or raise HTTP 404 if unknown."""
    with RUN_DIRECTORY_LOCK:
        candidate = RUN_DIRECTORIES.get(run_id)
    if candidate and candidate.is_dir():
        return candidate

    data = _load_run_index_unlocked()
    rel = data.get(run_id)
    if rel:
        run_dir = _require_root() / pathlib.Path(rel)
        if run_dir.is_dir():
            with RUN_DIRECTORY_LOCK:
                RUN_DIRECTORIES[run_id] = run_dir
            return run_dir

    fallback = _require_root() / run_id
    if fallback.is_dir():
        with RUN_DIRECTORY_LOCK:
            RUN_DIRECTORIES[run_id] = fallback
        return fallback

    raise HTTPException(404, "Run nicht gefunden")


def _require_root() -> pathlib.Path:
    if _RUNS_ROOT is None:
        raise RuntimeError("RUNS_ROOT wurde noch nicht konfiguriert")
    return _RUNS_ROOT


def _load_run_index_unlocked() -> Dict[str, str]:
    try:
        raw = run_index_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return {
        run_id: rel
        for run_id, rel in data.items()
        if isinstance(run_id, str) and isinstance(rel, str)
    }


def _write_run_index_unlocked(data: Dict[str, str]) -> None:
    path = run_index_path()
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
