from __future__ import annotations

import datetime
import math
from datetime import timezone
from typing import Any, Dict, Iterable, Mapping, Optional


def utcnow_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format with trailing Z."""
    return datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(ts: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 timestamp and normalize it to a timezone-aware UTC value."""
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        parsed = datetime.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _as_float(value: Any) -> Optional[float]:
    """Cast dynamic values to float while rejecting NaN or infinity."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _as_positive_float(value: Any) -> Optional[float]:
    """Return a strictly positive float when possible, otherwise None."""
    num = _as_float(value)
    if num is None or num <= 0:
        return None
    return num


def _as_positive_int(value: Any) -> Optional[int]:
    """Return a positive integer parsed from the given value or None if invalid."""
    num = _as_positive_float(value)
    if num is None:
        return None
    integer = int(num)
    if integer <= 0:
        return None
    return integer


def estimate_planned_duration(mode: Optional[str], params: Dict[str, Any]) -> Optional[float]:
    """Estimate measurement duration in seconds based on mode-specific parameters."""
    if not mode or not params:
        return None
    mode_key = mode.upper()
    setup = 1.0

    if mode_key == "CV":
        scan_rate = _as_positive_float(params.get("scan_rate"))
        cycles = _as_positive_float(params.get("cycles"))
        start = _as_float(params.get("start"))
        vertex1 = _as_float(params.get("vertex1"))
        vertex2 = _as_float(params.get("vertex2"))
        end = _as_float(params.get("end"))
        if not (
            scan_rate
            and cycles is not None
            and start is not None
            and vertex1 is not None
            and vertex2 is not None
            and end is not None
        ):
            return None
        sweep = abs(vertex1 - start) + abs(vertex2 - vertex1) + abs(end - vertex2)
        if sweep <= 0:
            return None
        base = (sweep / scan_rate) * max(cycles, 1)
        return base + setup

    if mode_key in {"CA", "CP", "OCP"}:
        duration = _as_positive_float(params.get("duration"))
        if duration is None:
            return None
        return duration + setup

    if mode_key == "LSV":
        start = _as_float(params.get("start"))
        end = _as_float(params.get("end"))
        scan_rate = _as_positive_float(params.get("scan_rate"))
        if start is None or end is None or not scan_rate:
            return None
        base = abs(end - start) / scan_rate
        return base + setup

    if mode_key == "PSTEP":
        potentials = params.get("potentials")
        step_duration = _as_positive_float(params.get("step_duration"))
        if not isinstance(potentials, list) or not step_duration:
            return None
        steps = len(potentials)
        if steps <= 0:
            return None
        base = steps * step_duration
        return base + setup

    if mode_key == "GS":
        num_steps = _as_positive_int(params.get("num_steps"))
        step_duration = _as_positive_float(params.get("step_duration"))
        if not num_steps or not step_duration:
            return None
        base = num_steps * step_duration
        return base + setup

    if mode_key == "GCV":
        num_steps = _as_positive_int(params.get("num_steps"))
        step_duration = _as_positive_float(params.get("step_duration"))
        cycles = _as_positive_int(params.get("cycles"))
        if not num_steps or not step_duration or not cycles:
            return None
        base = num_steps * step_duration * max(cycles, 1)
        return base + setup

    if mode_key == "STEPSEQ":
        currents = params.get("currents")
        step_duration = _as_positive_float(params.get("step_duration"))
        if not isinstance(currents, list) or not step_duration:
            return None
        steps = len(currents)
        if steps <= 0:
            return None
        base = steps * step_duration
        return base + setup

    if mode_key == "DC":
        duration = _as_positive_float(params.get("duration_s"))
        if duration is None:
            return None
        return duration + setup

    if mode_key == "EIS":
        start_freq = _as_positive_float(params.get("start_freq"))
        end_freq = _as_positive_float(params.get("end_freq"))
        points_per_decade = _as_positive_float(params.get("points_per_decade"))
        cycles_per_freq = _as_positive_float(params.get("cycles_per_freq")) or 3.0
        if not start_freq or not end_freq or not points_per_decade or not cycles_per_freq:
            return None
        spacing = str(params.get("spacing") or "log").strip().lower()

        if math.isclose(start_freq, end_freq, rel_tol=1e-9):
            freqs = [start_freq]
        else:
            decades = abs(math.log10(end_freq) - math.log10(start_freq))
            points = max(int(round(decades * points_per_decade)) + 1, 2)
            if spacing == "lin":
                step = (end_freq - start_freq) / (points - 1)
                freqs = [start_freq + i * step for i in range(points)]
            else:
                log_start = math.log10(start_freq)
                log_end = math.log10(end_freq)
                step_log = (log_end - log_start) / (points - 1)
                freqs = [10 ** (log_start + i * step_log) for i in range(points)]

        total = sum(cycles_per_freq / f for f in freqs if f and f > 0)
        if total <= 0:
            return None
        return total + setup

    return None


def compute_progress(
    *,
    status: str,
    slots: Iterable[Mapping[str, Any]],
    started_at: Optional[str],
    planned_duration_s: Optional[float],
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Optional[int]]:
    """Compute overall progress percentage and remaining seconds for a run."""
    now = now or datetime.datetime.now(timezone.utc)
    status_norm = (status or "").lower()
    if status_norm in {"done", "failed"}:
        return {"progress_pct": 100, "remaining_s": 0}

    slot_list = list(slots)
    slot_progress: list[int] = []
    remaining_candidates: list[int] = []

    for slot in slot_list:
        slot_status = str(slot.get("status") or "").lower()
        if slot_status in {"done", "failed"}:
            slot_progress.append(100)
            remaining_candidates.append(0)
            continue
        if slot_status == "queued":
            slot_progress.append(0)
            continue
        if slot_status == "running":
            started = parse_iso(slot.get("started_at")) or parse_iso(started_at)
            if started and planned_duration_s and planned_duration_s > 0:
                elapsed = max((now - started).total_seconds(), 0.0)
                pct = int(round(min(1.0, elapsed / planned_duration_s) * 100))
                if pct >= 100:
                    pct = 99
                slot_progress.append(max(0, pct))
                remaining_candidates.append(max(int(math.ceil(planned_duration_s - elapsed)), 0))
            else:
                slot_progress.append(0)
            continue
        slot_progress.append(0)

    if not slot_progress:
        return {"progress_pct": 0, "remaining_s": None}

    avg_progress = int(round(sum(slot_progress) / len(slot_progress)))
    if status_norm == "running" and any(str(slot.get("status") or "").lower() == "running" for slot in slot_list):
        avg_progress = min(avg_progress, 99)

    if status_norm == "running":
        remaining_val: Optional[int] = max(remaining_candidates) if remaining_candidates else None
    else:
        remaining_val = 0 if status_norm in {"done", "failed"} else None

    return {"progress_pct": avg_progress, "remaining_s": remaining_val}
