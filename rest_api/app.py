# /opt/box/app.py
import logging, os, uuid, threading, zipfile, io, pathlib, datetime, platform, subprocess
from typing import Optional, Literal, Dict, List, Any
from datetime import timezone
import serial.tools.list_ports
from fastapi import Body, FastAPI, HTTPException, Header, Request, Response
from fastapi.responses import FileResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
import shlex  
import nas_smb as nas  

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover
    try:
        import importlib_metadata  # type: ignore
    except ImportError:  # pragma: no cover
        importlib_metadata = None  # type: ignore

from pyBEEP.controller import (
    connect_to_potentiostats,  # liefert List[PotentiostatController]
    PotentiostatController,
)
# Optional: vorhandene Plot-Funktionen nutzen
from pyBEEP.plotter import plot_cv_cycles, plot_time_series
from progress_utils import compute_progress, estimate_planned_duration, utcnow_iso
from validation import (
    ValidationResult,
    UnsupportedModeError,
    validate_mode_payload,
)
import storage

API_KEY = os.getenv("BOX_API_KEY", "")
BOX_ID = os.getenv("BOX_ID", "")
RUNS_ROOT = pathlib.Path(os.getenv("RUNS_ROOT", "/opt/box/runs"))
RUNS_ROOT.mkdir(parents=True, exist_ok=True)
storage.configure_runs_root(RUNS_ROOT)
NAS_CONFIG_PATH = pathlib.Path(os.getenv("NAS_CONFIG_PATH", "/opt/box/nas_smb.json"))
NAS = nas.NASManager(runs_root=RUNS_ROOT, config_path=NAS_CONFIG_PATH, logger=logging.getLogger("nas_smb"))

RunStorageInfo = storage.RunStorageInfo
RUN_DIRECTORY_LOCK = storage.RUN_DIRECTORY_LOCK
RUN_DIRECTORIES = storage.RUN_DIRECTORIES
_run_index_path = storage.run_index_path
_value_or_none = storage.value_or_none
_sanitize_path_segment = storage.sanitize_path_segment
_sanitize_optional_segment = storage.sanitize_optional_segment
_sanitize_client_datetime = storage.sanitize_client_datetime
_record_run_directory = storage.record_run_directory
_forget_run_directory = storage.forget_run_directory
_resolve_run_directory = storage.resolve_run_directory
configure_run_storage_root = storage.configure_runs_root

API_VERSION = "1.0"

try:
    from seva.utils.logging import configure_root as _configure_logging, level_name as _level_name
except Exception:  # pragma: no cover - fallback when GUI package unavailable
    def _configure_logging(default_level: int | str = logging.INFO) -> int:
        level = logging.INFO
        if isinstance(default_level, str):
            candidate = getattr(logging, default_level.upper(), None)
            if isinstance(candidate, int):
                level = candidate
        else:
            try:
                level = int(default_level)
            except Exception:
                level = logging.INFO
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=level,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        logging.getLogger().setLevel(level)
        return level

    def _level_name(level: int) -> str:
        return logging.getLevelName(level)

else:
    def _level_name(level: int) -> str:
        return logging.getLevelName(level)


_configure_logging()
log = logging.getLogger("rest_api")
log.debug("REST API logger initialized at %s", _level_name(logging.getLogger().level))


def _detect_pybeep_version() -> str:
    if "importlib_metadata" in globals() and importlib_metadata:
        try:
            version = importlib_metadata.version("pyBEEP")
            if version:
                return version
        except Exception:
            pass
    try:
        import pyBEEP  # type: ignore
    except Exception:
        return "unknown"
    for attr in ("__version__", "VERSION"):
        value = getattr(pyBEEP, attr, None)  # type: ignore[name-defined]
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _detect_build_identifier() -> str:
    env_build = os.getenv("BOX_BUILD") or os.getenv("BOX_BUILD_ID")
    if env_build:
        return env_build
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo_root),
        )
        commit = (result.stdout or "").strip()
        if commit:
            return commit
    except Exception:
        pass
    return datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_error_detail(code: str, message: str, hint: Optional[str] = None) -> Dict[str, str]:
    return {"code": code, "message": message, "hint": hint or ""}


def http_error(status_code: int, code: str, message: str, hint: Optional[str] = None) -> HTTPException:
    if status_code == 422:
        log.info("Validation failed [%s]: %s", code, message)
        if hint:
            log.debug("Validation hint: %s", hint)
    return HTTPException(status_code=status_code, detail=_build_error_detail(code, message, hint))


PYBEEP_VERSION = _detect_pybeep_version()
PYTHON_VERSION = platform.python_version()
BUILD_IDENTIFIER = _detect_build_identifier()

# ---------- Geräte-Registry ----------
class DeviceInfo(BaseModel):
    slot: str
    port: str  # z.B. /dev/ttyACM0 oder ttyACM0
    sn: Optional[str] = None

DEVICES: Dict[str, PotentiostatController] = {}   # slot -> controller
DEV_META: Dict[str, DeviceInfo] = {}              # slot -> info
DEVICE_SCAN_LOCK = threading.Lock()

def discover_devices():
    with DEVICE_SCAN_LOCK:
        DEVICES.clear()
        DEV_META.clear()
        controllers = connect_to_potentiostats()
        ports = {p.device: p for p in serial.tools.list_ports.comports()}

        for i, ctrl in enumerate(controllers, start=1):
            slot = f"slot{i:02d}"
            DEVICES[slot] = ctrl
            try:
                port_name = ctrl.device.device.serial.port
                serial_info = ports.get(port_name)
                serial_number = serial_info.serial_number if serial_info else None
            except Exception:
                port_name, serial_number = "<unknown>", None

            DEV_META[slot] = DeviceInfo(slot=slot, port=str(port_name), sn=serial_number)

# ---------- Job-Modelle ----------
class JobRequest (BaseModel):
    devices: List[str] | Literal["all"] = Field(..., description='z.B. ["slot01","slot02"] oder "all"')
    modes: List[str] = Field(..., min_length=1, description="z.B. ['CV','EIS']")
    params_by_mode: Dict[str, Dict] = Field(default_factory=dict, description="pro Modus Parameterschema")
    tia_gain: Optional[int] = 0
    sampling_interval: Optional[float] = None
    experiment_name: str = Field(..., description="Experimentname fuer die Ablage")
    subdir: Optional[str] = Field(default=None, description="Optionaler Unterordner fuer die Ablage")
    client_datetime: str = Field(..., description="Zeitstempel des Clients fuer Verzeichnis- und Dateinamen")
    run_name: Optional[str] = None
    folder_name: Optional[str] = None
    make_plot: bool = True


def _build_run_storage_info(req: JobRequest) -> RunStorageInfo:
    subdir_source = req.subdir
    if _value_or_none(subdir_source) is None:
        subdir_source = req.folder_name

    experiment_segment = _sanitize_path_segment(req.experiment_name, "experiment_name")
    subdir_segment = _sanitize_optional_segment(subdir_source)
    timestamp_segment = _sanitize_client_datetime(req.client_datetime)
    timestamp_name = timestamp_segment.replace("T", "_")

    filename_parts = [experiment_segment]
    if subdir_segment:
        filename_parts.append(subdir_segment)
    filename_parts.append(timestamp_name)
    filename_prefix = "_".join(filename_parts)

    return RunStorageInfo(
        experiment=experiment_segment,
        subdir=subdir_segment,
        timestamp_dir=timestamp_segment,
        timestamp_name=timestamp_name,
        filename_prefix=filename_prefix,
    )


class SlotStatus(BaseModel):
    slot: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    message: Optional[str] = None
    files: List[str] = Field(default_factory=list)  # relative Pfade

class JobStatus(BaseModel):
    run_id: str
    # Für Abwärtskompatibilität nutzen wir 'mode' als *aktuellen* Modus
    mode: str
    started_at: str
    status: Literal["running", "done", "failed", "cancelled"]
    ended_at: Optional[str] = None
    slots: List[SlotStatus]
    progress_pct: int = 0
    remaining_s: Optional[int] = None
    modes: List[str] = Field(default_factory=list)
    current_mode: Optional[str] = None
    remaining_modes: List[str] = Field(default_factory=list)


class JobOverview(BaseModel):
    run_id: str
    mode: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    devices: List[str]


class JobStatusBulkRequest(BaseModel):
    run_ids: List[str] = Field(..., min_length=1, description="run_id list for bulk status lookup")

JOBS: Dict[str, JobStatus] = {}            # run_id -> status
JOB_LOCK = threading.Lock()
SLOT_STATE_LOCK = threading.Lock()
SLOT_RUNS: Dict[str, str] = {}             # slot -> run_id
JOB_META: Dict[str, Dict[str, Any]] = {}   # run_id -> metadata bag
JOB_GROUP_IDS: Dict[str, str] = {}         # run_id -> provided group identifier (raw)
JOB_GROUP_FOLDERS: Dict[str, str] = {}     # run_id -> sanitized storage folder name
CANCEL_FLAGS: Dict[str, threading.Event] = {}  # run_id -> cancel flag


def record_job_meta(run_id: str, mode: str, params: Dict[str, Any]) -> None:
    """Persist the original request parameters and derived duration estimate."""
    JOB_META[run_id] = {
        "mode": mode,
        "params": dict(params or {}),
        "planned_duration_s": estimate_planned_duration(mode, params or {}),
    }


def _normalize_group_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    trimmed = value.strip()
    return trimmed or None


def _derive_group_folder(run_id: str) -> Optional[str]:
    try:
        run_dir = _resolve_run_directory(run_id)
    except HTTPException:
        return None
    except Exception:
        return None
    try:
        relative = run_dir.relative_to(RUNS_ROOT)
    except Exception:
        return None
    parts = relative.parts
    if len(parts) >= 3:
        return parts[-2]
    return None


def _job_overview_status(job: JobStatus) -> Literal["queued", "running", "done", "failed", "cancelled"]:
    slot_states = [slot.status for slot in job.slots]
    if slot_states and all(state == "queued" for state in slot_states):
        return "queued"
    return job.status


def job_snapshot(job: JobStatus) -> JobStatus:
    copy = job.model_copy(deep=True)

    # only create/retain meta while the job is running
    meta = JOB_META.get(copy.run_id)
    if meta is None and copy.status == "running":
        meta = JOB_META[copy.run_id] = {"mode": copy.mode, "params": {}}
    elif meta is None:
        meta = {"mode": copy.mode, "params": {}}

    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    planned = meta.get("planned_duration_s")
    if planned is None:
        planned = estimate_planned_duration(meta.get("mode") or copy.mode, params)
        # store planned only for running jobs to avoid re-populating after cleanup
        if copy.status == "running":
            meta["planned_duration_s"] = planned

    slot_payload = [slot.model_dump() for slot in copy.slots]
    metrics = compute_progress(
        status=copy.status,
        slots=slot_payload,
        started_at=copy.started_at,
        planned_duration_s=planned,
    )
    copy.progress_pct = metrics.get("progress_pct") or 0
    copy.remaining_s = metrics.get("remaining_s")
    return copy


# ---------- Startup ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    discover_devices()
    try:
        NAS.start_background()
    except Exception:
        log.exception("Failed to start NAS background tasks")
    try:
        yield
    finally:
        for ctrl in DEVICES.values():
            try:
                ctrl.device.device.serial.close()
            except Exception:
                pass

app = FastAPI(title="Potentiostat Box API", version=API_VERSION, lifespan=lifespan)
# TODO(metrics): optional Prometheus /metrics exporter (future)


@app.exception_handler(RequestValidationError)
async def handle_request_validation(
    request: Request, exc: RequestValidationError
):
    errors = exc.errors()
    log.info("Validation 422 path=%s issues=%d", request.url.path, len(errors))
    log.debug("Validation detail: %s", errors)
    return await request_validation_exception_handler(request, exc)

# ---------- Auth Helper ----------
def require_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise http_error(
            status_code=401,
            code="auth.invalid_api_key",
            message="Unauthorized",
            hint="X-API-Key Header fehlt oder ist falsch.",
        )


@app.get("/version")
def version_info() -> Dict[str, str]:
    return {
        "api": API_VERSION,
        "pybeep": PYBEEP_VERSION,
        "python": PYTHON_VERSION,
        "build": BUILD_IDENTIFIER,
    }

# ---------- Health / Geräte / Modi ----------
@app.get("/health")
def health(x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    with DEVICE_SCAN_LOCK:
        device_count = len(DEVICES)
    return {"ok": True, "devices": device_count, "box_id": BOX_ID}

@app.get("/devices")
def list_devices(x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    with DEVICE_SCAN_LOCK:
        return [DEV_META[s].model_dump() for s in sorted(DEV_META.keys())]

@app.get("/modes")
def list_modes(x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    # Nimm die Modi vom ersten Gerät (alle sind identisch konfiguriert)
    with DEVICE_SCAN_LOCK:
        try:
            first = next(iter(DEVICES.values()))
        except StopIteration:
            raise http_error(
                status_code=503,
                code="devices.unavailable",
                message="Keine Geraete registriert",
                hint="Mit /admin/rescan nach neuen Geraeten suchen.",
            )
    return first.get_available_modes()

@app.get("/modes/{mode}/params")
def mode_params(mode: str, x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    with DEVICE_SCAN_LOCK:
        try:
            first = next(iter(DEVICES.values()))
        except StopIteration:
            raise http_error(
                status_code=503,
                code="devices.unavailable",
                message="Keine Geraete registriert",
                hint="Mit /admin/rescan nach neuen Geraeten suchen.",
            )
    try:
        return {k: str(v) for k, v in first.get_mode_params(mode).items()}
    except Exception as e:
        raise http_error(
            status_code=400,
            code="modes.parameter_error",
            message=str(e),
            hint="Parameter entsprechend der Modus-Spezifikation pruefen.",
        )


@app.post("/modes/{mode}/validate")
def validate_mode_params(
    mode: str,
    params: Dict[str, Any] = Body(...),
    x_api_key: Optional[str] = Header(None),
) -> ValidationResult:
    """Validate mode parameter payloads without contacting any hardware."""

    require_key(x_api_key)

    try:
        return validate_mode_payload(mode, params or {})
    except UnsupportedModeError as exc:
        raise http_error(
            status_code=404,
            code="modes.not_found",
            message=str(exc),
            hint="Verfuegbare Modi ueber /modes abrufen.",
        )

# ---------- Job Worker ----------
def _update_job_status_locked(job: Optional[JobStatus]) -> None:
    if not job:
        return

    statuses = [slot.status for slot in job.slots]
    if any(state in ("queued", "running") for state in statuses):
        job.status = "running"
        job.ended_at = None
        return

    if any(state == "failed" for state in statuses):
        job.status = "failed"
    elif any(state == "cancelled" for state in statuses):
        job.status = "cancelled"
    else:
        job.status = "done"
    job.ended_at = utcnow_iso()
    #drop transient meta once job is terminal
    JOB_META.pop(job.run_id, None)
    CANCEL_FLAGS.pop(job.run_id, None)
    # NEU: Upload nur bei 'done' enqueuen (nicht bei failed/cancelled)
    if job.status == "done":
        try:
            NAS.enqueue_upload(job.run_id)
        except Exception:
            log.exception("Failed to enqueue NAS upload for run_id=%s", job.run_id)


def _request_controller_abort(ctrl: PotentiostatController) -> None:
    """Best effort attempt to stop a running measurement on the controller."""
    for attr in (
        "abort_measurement",
        "cancel_measurement",
        "stop_measurement",
        "abort",
        "cancel",
        "stop",
    ):
        method = getattr(ctrl, attr, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            else:
                return

    try:
        serial = getattr(ctrl, "device", None)
        serial = getattr(serial, "device", serial)
        serial = getattr(serial, "serial", serial)
        close_method = getattr(serial, "close", None)
        if callable(close_method):
            close_method()
    except Exception:
        pass

def _run_slot_sequence(
    run_id: str,
    run_dir: pathlib.Path,
    slot: str,
    req: JobRequest,
    slot_status: SlotStatus,
    storage: RunStorageInfo,
):
    """Führt die Liste 'modes' nacheinander aus. Jede Messung schreibt in eigenen Mode-Unterordner."""
    ctrl = DEVICES[slot]
    slot_segment = _sanitize_path_segment(slot, "slot")
    cancel_event = CANCEL_FLAGS.setdefault(run_id, threading.Event())

    def _eval_plot(csv_path: pathlib.Path, mode: str, params: Dict[str, Any]) -> List[str]:
        files: List[str] = []
        try:
            if req.make_plot:
                png_path = csv_path.with_suffix(".png")
                if (mode or "").upper() == "CV":
                    plot_cv_cycles(str(csv_path), figpath=str(png_path), show=False, cycles=params.get("cycles"))
                else:
                    plot_time_series(str(csv_path), figpath=str(png_path), show=False)
            folder = csv_path.parent
            files = [str(p.relative_to(run_dir)) for p in folder.iterdir() if p.is_file()]
        except Exception:
            try:
                folder = csv_path.parent
                files = [str(p.relative_to(run_dir)) for p in folder.iterdir() if p.is_file()]
            except Exception:
                files = []
        return sorted(files)

    # Slot initial auf running setzen (einheitlich)
    with JOB_LOCK:
        slot_status.status = "running"
        slot_status.started_at = slot_status.started_at or utcnow_iso()
        slot_status.message = None
        job = JOBS.get(run_id)
        if job:
            job.status = "running"
            job.ended_at = None

    files_collected: List[str] = []
    error: Optional[str] = None

    try:
        for idx, mode in enumerate(req.modes or []):
            if cancel_event.is_set():
                error = "cancelled"
                break

            # Status: aktuellen/Rest-Modus setzen (auch 'mode' für Kompatibilität)
            with JOB_LOCK:
                job = JOBS.get(run_id)
                if job:
                    job.mode = mode
                    job.current_mode = mode
                    job.modes = list(req.modes or [])
                    job.remaining_modes = list(req.modes[idx + 1:])

            # Per-Mode Ordner/Dateinamen
            mode_segment = _sanitize_path_segment(mode, "mode")
            mode_dir = run_dir / "Wells" / slot_segment / mode_segment
            mode_dir.mkdir(parents=True, exist_ok=True)
            filename_base = f"{storage.filename_prefix}_{slot_segment}_{mode_segment}"
            filename = f"{filename_base}.csv"

            params = dict(req.params_by_mode.get(mode, {}) or {})

            # Messung mit Abbruchfenster in Neben-Thread
            measurement_error: Optional[Exception] = None
            def _runner():
                nonlocal measurement_error
                try:
                    ctrl.apply_measurement(
                        mode=mode,
                        params=params,
                        tia_gain=req.tia_gain,
                        sampling_interval=req.sampling_interval,
                        filename=filename,
                        folder=str(mode_dir),
                    )
                except Exception as exc:
                    measurement_error = exc

            t = threading.Thread(target=_runner, name=f"{run_id}-{slot}-{mode}", daemon=True)
            t.start()
            abort_requested = False
            while t.is_alive():
                t.join(timeout=0.2)
                if cancel_event.is_set() and not abort_requested:
                    _request_controller_abort(ctrl)
                    abort_requested = True
            t.join()

            if cancel_event.is_set():
                error = "cancelled"
                break

            if measurement_error:
                error = str(measurement_error)
                break

            # Dateien einsammeln, Status fortschreiben
            csv_path = mode_dir / filename
            files_collected.extend(_eval_plot(csv_path, mode, params))

    except Exception as exc:
        error = str(exc)

    # Slot/JOB finalisieren
    with JOB_LOCK:
        if error == "cancelled":
            slot_status.status = "cancelled"
            slot_status.message = "cancelled"
        elif error:
            slot_status.status = "failed"
            slot_status.message = error
        else:
            slot_status.status = "done"
            slot_status.message = None

        slot_status.ended_at = utcnow_iso()
        slot_status.files = sorted(files_collected)

        job = JOBS.get(run_id)
        if job:
            # Wenn letzter Slot fertig, Job terminiert + Modes-Felder zurücksetzen
            _update_job_status_locked(job)
            if job.status in ("done", "failed", "cancelled"):
                job.current_mode = None
                job.remaining_modes = []
                
    with SLOT_STATE_LOCK:
        if SLOT_RUNS.get(slot) == run_id:
            del SLOT_RUNS[slot]


def _run_one_slot(
    run_id: str,
    run_dir: pathlib.Path,
    slot: str,
    req: JobRequest,
    slot_status: SlotStatus,
    storage: RunStorageInfo,
):
    """Ein Slot/Device abarbeiten - blockierend im Thread."""
    ctrl = DEVICES[slot]
    slot_segment = _sanitize_path_segment(slot, "slot")
    mode_segment = _sanitize_path_segment(req.mode, "mode")
    slot_dir = run_dir / "Wells" / slot_segment / mode_segment
    slot_dir.mkdir(parents=True, exist_ok=True)
    filename_base = f"{storage.filename_prefix}_{slot_segment}_{mode_segment}"
    filename = f"{filename_base}.csv"

    cancel_event = CANCEL_FLAGS.setdefault(run_id, threading.Event())

    if cancel_event.is_set():
        with JOB_LOCK:
            slot_status.status = "cancelled"
            if not slot_status.started_at:
                slot_status.started_at = utcnow_iso()
            slot_status.ended_at = utcnow_iso()
            slot_status.message = "cancelled"
            slot_status.files = []
            _update_job_status_locked(JOBS.get(run_id))
        with SLOT_STATE_LOCK:
            if SLOT_RUNS.get(slot) == run_id:
                del SLOT_RUNS[slot]
        return

    with JOB_LOCK:
        slot_status.status = "running"
        slot_status.started_at = utcnow_iso()
        slot_status.message = None
        _update_job_status_locked(JOBS.get(run_id))

    files: List[str] = []
    error: Optional[Exception] = None
    cancelled = False
    measurement_error: Optional[Exception] = None

    def _measurement_runner():
        nonlocal measurement_error
        try:
            ctrl.apply_measurement(
                mode=req.mode,
                params=req.params,
                tia_gain=req.tia_gain,
                sampling_interval=req.sampling_interval,
                filename=filename,
                folder=str(slot_dir),
            )
        except Exception as exc:
            measurement_error = exc

    runner_thread = threading.Thread(
        target=_measurement_runner,
        name=f"{run_id}-{slot}-measurement",
        daemon=True,
    )
    runner_thread.start()

    abort_requested = False
    while runner_thread.is_alive():
        runner_thread.join(timeout=0.2)
        if cancel_event.is_set():
            cancelled = True
            if not abort_requested:
                _request_controller_abort(ctrl)
                abort_requested = True

    runner_thread.join()
    if cancel_event.is_set():
        cancelled = True

    if cancelled:
        # treat controller exceptions as part of the cancellation flow
        measurement_error = None

    if measurement_error is not None:
        error = measurement_error
    elif not cancelled:
        csv_path = slot_dir / filename
        if req.make_plot:
            png_path = csv_path.with_suffix('.png')
            if req.mode.upper() == "CV":
                plot_cv_cycles(str(csv_path), figpath=str(png_path), show=False, cycles=req.params.get("cycles"))
            else:
                plot_time_series(str(csv_path), figpath=str(png_path), show=False)
        files = [
            str(p.relative_to(run_dir))
            for p in slot_dir.iterdir()
            if p.is_file()
        ]
    else:
        try:
            files = [
                str(p.relative_to(run_dir))
                for p in slot_dir.iterdir()
                if p.is_file()
            ]
        except Exception:
            files = []

    with JOB_LOCK:
        if cancelled:
            slot_status.status = "cancelled"
            slot_status.ended_at = utcnow_iso()
            slot_status.message = "cancelled"
            slot_status.files = sorted(files)
        elif error is None:
            slot_status.files = sorted(files)
            slot_status.status = "done"
            slot_status.ended_at = utcnow_iso()
            slot_status.message = None
        else:
            slot_status.status = "failed"
            slot_status.message = str(error)
            slot_status.ended_at = utcnow_iso()
            try:
                slot_status.files = sorted([str(p.relative_to(run_dir)) for p in slot_dir.iterdir() if p.is_file()])
            except Exception:
                slot_status.files = []
        _update_job_status_locked(JOBS.get(run_id))

    with SLOT_STATE_LOCK:
        if SLOT_RUNS.get(slot) == run_id:
            del SLOT_RUNS[slot]

# ---------- Endpunkte: Jobs ----------
@app.post("/jobs/status", response_model=List[JobStatus])
def jobs_bulk_status(req: JobStatusBulkRequest, x_api_key: Optional[str] = Header(None)):
    """Return snapshot data for multiple runs in a single call."""
    require_key(x_api_key)
    run_ids = [rid for rid in (req.run_ids or []) if rid]
    if not run_ids:
        raise http_error(
            status_code=400,
            code="jobs.missing_run_ids",
            message="Keine run_ids angegeben",
            hint="run_ids Feld im Request ausfuellen.",
        )
    with JOB_LOCK:
        missing = [rid for rid in run_ids if rid not in JOBS]
        if missing:
            missing_str = ", ".join(sorted(missing))
            raise http_error(
                status_code=404,
                code="jobs.run_ids_unknown",
                message=f"Unbekannte run_ids: {missing_str}",
                hint="Nur bekannte run_ids anfragen.",
            )
        snapshots = [job_snapshot(JOBS[rid]) for rid in run_ids]
    log.debug("jobs/status bulk request count=%d", len(run_ids))
    return snapshots


@app.get("/jobs", response_model=List[JobOverview])
def list_jobs(
    state: Optional[Literal["incomplete", "completed"]] = None,
    group_id: Optional[str] = None,
    x_api_key: Optional[str] = Header(None),
) -> List[JobOverview]:
    """Return a lightweight job overview list with optional filtering."""
    require_key(x_api_key)
    state_filter = state or None
    group_filter = _normalize_group_value(group_id)
    group_filter_lower = group_filter.lower() if group_filter else None

    with JOB_LOCK:
        run_ids = list(JOBS.keys())
        job_entries = [(rid, JOBS[rid].model_copy(deep=True)) for rid in run_ids]
        group_raw_map = {rid: JOB_GROUP_IDS.get(rid) for rid in run_ids}
        group_folder_map = {rid: JOB_GROUP_FOLDERS.get(rid) for rid in run_ids}

    results: List[JobOverview] = []
    for run_id, job in job_entries:
        overview_status = _job_overview_status(job)
        if state_filter == "incomplete" and overview_status not in ("queued", "running"):
            continue
        if state_filter == "completed" and overview_status not in ("done", "failed", "cancelled"):
            continue

        if group_filter_lower:
            candidate_norms = set()
            for candidate in (
                group_raw_map.get(run_id),
                group_folder_map.get(run_id),
            ):
                normalized_candidate = _normalize_group_value(candidate)
                if normalized_candidate:
                    candidate_norms.add(normalized_candidate.lower())
            normalized_folder = _normalize_group_value(_derive_group_folder(run_id))
            if normalized_folder:
                candidate_norms.add(normalized_folder.lower())
            if group_filter_lower not in candidate_norms:
                continue

        devices = [slot.slot for slot in job.slots]
        results.append(
            JobOverview(
                run_id=run_id,
                mode=job.mode,
                status=overview_status,
                started_at=job.started_at,
                ended_at=job.ended_at,
                devices=devices,
            )
        )

    results.sort(key=lambda item: ((item.started_at or ""), item.run_id), reverse=True)
    return results


@app.post("/jobs", response_model=JobStatus)
def start_job(req: JobRequest, x_api_key: Optional[str] = Header(None)):
    """Start a new job across selected slots and launch worker threads (multi-mode sequence)."""
    require_key(x_api_key)

    if not req.modes:
        raise http_error(status_code=422, code="jobs.invalid_request", message="modes must not be empty")
    for m in req.modes:
        if m not in req.params_by_mode:
            raise http_error(status_code=422, code="jobs.invalid_request", message=f"missing params for mode {m}")

    with DEVICE_SCAN_LOCK:
        if req.devices == "all":
            slots = sorted(DEVICES.keys())
        else:
            slots = [s for s in req.devices if s in DEVICES]
    if not slots:
        raise http_error(status_code=400, code="jobs.invalid_devices", message="Keine gueltigen devices angegeben", hint="Verwende Slots aus /devices oder 'all'.")

    run_id = req.run_name or datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]

    with JOB_LOCK:
        if run_id in JOBS:
            raise http_error(status_code=409, code="jobs.run_id_conflict", message="run_id bereits aktiv", hint="Andere run_id waehlen oder laufenden Job abwarten.")

    with SLOT_STATE_LOCK:
        busy = sorted(s for s in slots if s in SLOT_RUNS)
        if busy:
            raise http_error(status_code=409, code="jobs.slots_busy", message=f"Slots belegt: {', '.join(busy)}", hint="Warte bis die genannten Slots frei sind.")
        for s in slots:
            SLOT_RUNS[s] = run_id

    slot_statuses = [SlotStatus(slot=s, status="queued") for s in slots]
    started_at = utcnow_iso()

    try:
        storage_info = _build_run_storage_info(req)
        path_parts = [p for p in (storage_info.experiment, storage_info.subdir) if p]
        path_parts.append(storage_info.timestamp_dir)
        run_dir = RUNS_ROOT.joinpath(*path_parts)
        run_dir.mkdir(parents=True, exist_ok=True)
        _record_run_directory(run_id, run_dir)

        raw_group_id = _normalize_group_value(req.folder_name) or _normalize_group_value(req.subdir)
        storage_folder = storage_info.subdir

        # Für Kompatibilität: 'mode' = erster Modus, 'modes' vollständig
        first_mode = (req.modes or [""])[0]
        job = JobStatus(
            run_id=run_id,
            mode=first_mode,
            started_at=started_at,
            status="running",
            ended_at=None,
            slots=slot_statuses,
            modes=list(req.modes or []),
            current_mode=first_mode,
            remaining_modes=list(req.modes[1:] if len(req.modes) > 1 else []),
        )

        with JOB_LOCK:
            JOBS[run_id] = job
            CANCEL_FLAGS[run_id] = threading.Event()
            # Progress-Schätzung grob anhand des ersten Modus (KISS)
            record_job_meta(run_id, first_mode, dict(req.params_by_mode.get(first_mode, {}) or {}))
            if raw_group_id:
                JOB_GROUP_IDS[run_id] = raw_group_id
            else:
                JOB_GROUP_IDS.pop(run_id, None)
            if storage_folder:
                JOB_GROUP_FOLDERS[run_id] = storage_folder
            else:
                JOB_GROUP_FOLDERS.pop(run_id, None)

        log.info("Job start run_id=%s modes=%s devices=%s slots=%s", run_id, req.modes, req.devices if req.devices != "all" else "all", slots)
        log.debug("Job storage run_id=%s group_id=%s folder=%s experiment=%s", run_id, raw_group_id or "-", storage_folder or "-", storage_info.experiment)

        for slot_status in slot_statuses:
            t = threading.Thread(
                target=_run_slot_sequence,
                args=(run_id, run_dir, slot_status.slot, req, slot_status, storage_info),
                daemon=True,
            )
            t.start()
    except Exception:
        with SLOT_STATE_LOCK:
            for s in slots:
                if SLOT_RUNS.get(s) == run_id:
                    del SLOT_RUNS[s]
        with JOB_LOCK:
            JOBS.pop(run_id, None)
            JOB_GROUP_IDS.pop(run_id, None)
            JOB_GROUP_FOLDERS.pop(run_id, None)
        CANCEL_FLAGS.pop(run_id, None)
        JOB_META.pop(run_id, None)
        _forget_run_directory(run_id)
        raise

    with JOB_LOCK:
        return job_snapshot(JOBS[run_id])



@app.post("/jobs/{run_id}/cancel", status_code=202)
def cancel_job(run_id: str, x_api_key: Optional[str] = Header(None)):
    """Signal cancellation for a running or queued job."""
    require_key(x_api_key)
    with JOB_LOCK:
        job = JOBS.get(run_id)
        if not job:
            raise http_error(
                status_code=404,
                code="jobs.not_found",
                message="Unbekannte run_id",
                hint="run_id pruefen oder Liste der Jobs abrufen.",
            )

        event = CANCEL_FLAGS.get(run_id)
        if event is None:
            event = CANCEL_FLAGS[run_id] = threading.Event()

        if job.status in ("done", "failed", "cancelled"):
            return {"run_id": run_id, "status": job.status}

        event.set()
        queued_slots: List[str] = []
        for slot_status in job.slots:
            if slot_status.status == "queued":
                slot_status.status = "cancelled"
                if not slot_status.started_at:
                    slot_status.started_at = utcnow_iso()
                slot_status.ended_at = utcnow_iso()
                slot_status.message = "cancelled"
                slot_status.files = []
                queued_slots.append(slot_status.slot)

        _update_job_status_locked(job)

    if queued_slots:
        with SLOT_STATE_LOCK:
            for slot in queued_slots:
                if SLOT_RUNS.get(slot) == run_id:
                    del SLOT_RUNS[slot]

    log.info("Job cancel requested run_id=%s queued_slots=%d", run_id, len(queued_slots))
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/jobs/{run_id}", response_model=JobStatus)
def job_status(run_id: str, x_api_key: Optional[str] = Header(None)):
    """Return the latest status snapshot for a single run."""
    require_key(x_api_key)
    with JOB_LOCK:
        job = JOBS.get(run_id)
        if not job:
            raise http_error(
                status_code=404,
                code="jobs.not_found",
                message="Unbekannte run_id",
                hint="run_id pruefen oder Liste der Jobs abrufen.",
            )
        return job_snapshot(job)


@app.get("/runs/{run_id}/files")
def list_run_files(run_id: str, x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    run_dir = _resolve_run_directory(run_id)
    if not run_dir.is_dir():
        raise http_error(
            status_code=404,
            code="runs.not_found",
            message="Run nicht gefunden",
            hint="run_id pruefen oder vorhandene Runs auflisten.",
        )
    files = [
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file()
    ]
    files.sort()
    log.info("List files run_id=%s count=%d", run_id, len(files))
    return {"files": files}


@app.get("/runs/{run_id}/file")
def get_run_file(run_id: str, path: str, x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    run_dir = _resolve_run_directory(run_id)
    if not run_dir.is_dir():
        raise http_error(
            status_code=404,
            code="runs.not_found",
            message="Run nicht gefunden",
            hint="run_id pruefen oder vorhandene Runs auflisten.",
        )
    if not path:
        raise http_error(
            status_code=404,
            code="runs.file_not_found",
            message="Datei nicht gefunden",
            hint="Pfad relativ zum Run-Verzeichnis angeben.",
        )

    run_root = run_dir.resolve()
    try:
        target_path = (run_dir / path).resolve(strict=True)
    except FileNotFoundError:
        raise http_error(
            status_code=404,
            code="runs.file_not_found",
            message="Datei nicht gefunden",
            hint="Pfad relativ zum Run-Verzeichnis angeben.",
        )

    try:
        target_path.relative_to(run_root)
    except ValueError:
        raise http_error(
            status_code=404,
            code="runs.file_not_found",
            message="Datei nicht gefunden",
            hint="Pfad relativ zum Run-Verzeichnis angeben.",
        )

    if not target_path.is_file():
        raise http_error(
            status_code=404,
            code="runs.file_not_found",
            message="Datei nicht gefunden",
            hint="Pfad relativ zum Run-Verzeichnis angeben.",
        )

    rel_path = target_path.relative_to(run_root).as_posix()
    log.info("Serve file run_id=%s path=%s", run_id, rel_path)
    return FileResponse(path=target_path, filename=target_path.name)


@app.get("/runs/{run_id}/zip")
def get_run_zip(run_id: str, x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    run_dir = _resolve_run_directory(run_id)
    if not run_dir.is_dir():
        raise http_error(
            status_code=404,
            code="runs.not_found",
            message="Run nicht gefunden",
            hint="run_id pruefen oder vorhandene Runs auflisten.",
        )
    # ZIP im Speicher bauen
    buf = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in run_dir.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(run_dir))
                file_count += 1
    buf.seek(0)
    content = buf.read()
    log.info("Serve zip run_id=%s files=%d size=%d", run_id, file_count, len(content))
    return Response(content=content,
                    media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.zip"'})

# ---------- NAS Storage Requests ----------

class SMBSetupRequest(BaseModel):
    host: str
    share: str
    username: str
    password: str
    base_subdir: str = ""     # optionaler Unterordner innerhalb des Shares
    retention_days: int = 14
    domain: Optional[str] = None

@app.post("/nas/setup")
def nas_setup(req: SMBSetupRequest, x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    result = NAS.setup(
        host=req.host,
        share=req.share,
        username=req.username,
        password=req.password,
        base_subdir=req.base_subdir,
        retention_days=req.retention_days,
        domain=req.domain,
    )
    return result

@app.get("/nas/health")
def nas_health(x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    return NAS.health()

@app.post("/runs/{run_id}/upload")
def nas_upload_run(run_id: str, x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    enq = NAS.enqueue_upload(run_id)
    return {"ok": True, "enqueued": bool(enq), "run_id": run_id}

# ---------- Admin (optional) ----------
@app.post("/admin/rescan")
def rescan(x_api_key: Optional[str] = Header(None)):
    require_key(x_api_key)
    discover_devices()
    with DEVICE_SCAN_LOCK:
        return {"devices": list(DEVICES.keys())}
