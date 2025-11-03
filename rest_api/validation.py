from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    """Machine-readable description of a single validation finding."""

    field: str = Field(..., description="Name of the validated parameter field")
    code: str = Field(..., description="Stable error or warning code")
    message: str = Field(..., description="Human-readable explanation of the issue")


class ValidationResult(BaseModel):
    """Structured validation response used by the GUI validation endpoint."""

    ok: bool = Field(..., description="Flag indicating validation success")
    errors: List[ValidationIssue] = Field(
        default_factory=list, description="Blocking validation errors"
    )
    warnings: List[ValidationIssue] = Field(
        default_factory=list, description="Non-blocking validation hints"
    )


class UnsupportedModeError(RuntimeError):
    """Raised when no validator exists for the requested mode."""


def _is_empty(value: Any) -> bool:
    """Return True when a value counts as empty for validation purposes."""

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _require_fields(
    payload: Dict[str, Any],
    fields: Iterable[str],
    *,
    errors: List[ValidationIssue],
) -> None:
    """Append missing_field errors for each required key."""

    for name in fields:
        if _is_empty(payload.get(name)):
            errors.append(
                ValidationIssue(
                    field=name,
                    code="missing_field",
                    message="Field is required.",
                )
            )


def _warn_not_implemented(
    warnings: List[ValidationIssue],
    *,
    field: str = "*",
    message: str = "Validation rules are not yet implemented for this mode.",
) -> None:
    """Attach a placeholder warning indicating incomplete implementation."""

    warnings.append(
        ValidationIssue(
            field=field,
            code="not_implemented",
            message=message,
        )
    )


def _coerce_float(
    field: str,
    payload: Dict[str, Any],
    errors: List[ValidationIssue],
    *,
    positive: bool = False,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Optional[float]:
    """Convert a payload value to float while collecting validation issues."""

    raw = payload.get(field, None)
    if _is_empty(raw):
        errors.append(
            ValidationIssue(field=field, code="missing_field", message="Field is required.")
        )
        return None

    try:
        number = float(raw)
    except Exception:
        errors.append(
            ValidationIssue(
                field=field,
                code="not_a_number",
                message="Value must be numeric.",
            )
        )
        return None

    if positive and number <= 0:
        errors.append(
            ValidationIssue(
                field=field,
                code="must_be_positive",
                message="Value must be greater than zero.",
            )
        )

    if minimum is not None and number < minimum:
        errors.append(
            ValidationIssue(
                field=field,
                code="min_value",
                message=f"Value must be at least {minimum}.",
            )
        )

    if maximum is not None and number > maximum:
        errors.append(
            ValidationIssue(
                field=field,
                code="max_value",
                message=f"Value must be at most {maximum}.",
            )
        )

    return number


def _coerce_int(
    field: str,
    payload: Dict[str, Any],
    errors: List[ValidationIssue],
    *,
    positive: bool = False,
) -> Optional[int]:
    """Convert a payload value to int while collecting validation issues."""

    raw = payload.get(field, None)
    if _is_empty(raw):
        errors.append(
            ValidationIssue(field=field, code="missing_field", message="Field is required.")
        )
        return None

    try:
        number = int(float(raw))
    except Exception:
        errors.append(
            ValidationIssue(
                field=field,
                code="not_an_integer",
                message="Value must be an integer.",
            )
        )
        return None

    if positive and number <= 0:
        errors.append(
            ValidationIssue(
                field=field,
                code="must_be_positive",
                message="Value must be greater than zero.",
            )
        )

    return number


def _validate_cv_params(payload: Dict[str, Any]) -> ValidationResult:
    """Validate CV parameters against lab safety limits and simple heuristics."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    voltage_bounds = (-10.0, 10.0)

    start = _coerce_float(
        "start",
        payload,
        errors,
        minimum=voltage_bounds[0],
        maximum=voltage_bounds[1],
    )
    vertex1 = _coerce_float(
        "vertex1",
        payload,
        errors,
        minimum=voltage_bounds[0],
        maximum=voltage_bounds[1],
    )
    vertex2 = _coerce_float(
        "vertex2",
        payload,
        errors,
        minimum=voltage_bounds[0],
        maximum=voltage_bounds[1],
    )
    end = _coerce_float(
        "end",
        payload,
        errors,
        minimum=voltage_bounds[0],
        maximum=voltage_bounds[1],
    )
    scan_rate = _coerce_float("scan_rate", payload, errors, positive=True)
    cycles = _coerce_int("cycles", payload, errors, positive=True)

    if (
        start is not None
        and vertex1 is not None
        and vertex2 is not None
        and end is not None
        and start == vertex1 == vertex2 == end
    ):
        errors.append(
            ValidationIssue(
                field="end",
                code="zero_sweep",
                message="Potential sweep must span at least one vertex.",
            )
        )

    if scan_rate is not None and scan_rate > 5.0:
        warnings.append(
            ValidationIssue(
                field="scan_rate",
                code="high_value",
                message="Scan rate exceeds 5 V/s; verify hardware capability.",
            )
        )

    if cycles is not None and cycles > 50:
        warnings.append(
            ValidationIssue(
                field="cycles",
                code="high_value",
                message="Cycle count above 50 may lead to long experiment times.",
            )
        )

    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _validate_dc_params(payload: Dict[str, Any]) -> ValidationResult:
    """Placeholder validation for DC mode (chronoamperometry / chrono)."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    required = ("duration_s", "voltage_v")
    _require_fields(payload, required, errors=errors)
    _warn_not_implemented(
        warnings,
        message="DC validation is not yet implemented; values were not checked.",
    )
    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _validate_ac_params(payload: Dict[str, Any]) -> ValidationResult:
    """Placeholder validation for AC mode (chrono AC pulses)."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    required = ("duration_s", "frequency_hz", "voltage_v")
    _require_fields(payload, required, errors=errors)
    _warn_not_implemented(
        warnings,
        message="AC validation is not yet implemented; values were not checked.",
    )
    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _validate_lsv_params(payload: Dict[str, Any]) -> ValidationResult:
    """Placeholder validation for LSV mode (linear sweep voltammetry)."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    required = ("start", "end", "scan_rate")
    _require_fields(payload, required, errors=errors)
    _warn_not_implemented(
        warnings,
        message="LSV validation is not yet implemented; values were not checked.",
    )
    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _validate_eis_params(payload: Dict[str, Any]) -> ValidationResult:
    """Placeholder validation for EIS mode (electrochemical impedance spectroscopy)."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    required = ("freq_start_hz", "freq_end_hz", "points", "spacing")
    _require_fields(payload, required, errors=errors)
    _warn_not_implemented(
        warnings,
        message="EIS validation is not yet implemented; values were not checked.",
    )
    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _validate_cdl_params(payload: Dict[str, Any]) -> ValidationResult:
    """Placeholder validation for CDL mode (capacitance measurement)."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    required = ("vertex_a_v", "vertex_b_v", "cycles")
    _require_fields(payload, required, errors=errors)
    _warn_not_implemented(
        warnings,
        message="CDL validation is not yet implemented; values were not checked.",
    )
    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)

def _validate_ca_params(payload: Dict[str, Any]) -> ValidationResult:
    """Placeholder validation for CDL mode (capacitance measurement)."""

    errors: List[ValidationIssue] = []
    warnings: List[ValidationIssue] = []
    required = ("duration", "potential")
    _require_fields(payload, required, errors=errors)
    _warn_not_implemented(
        warnings,
        message="CDL validation is not yet implemented; values were not checked.",
    )
    ok = not errors
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


_MODE_VALIDATORS: Dict[str, Callable[[Dict[str, Any]], ValidationResult]] = {
    "CV": _validate_cv_params,
    "DC": _validate_dc_params,
    "AC": _validate_ac_params,
    "LSV": _validate_lsv_params,
    "EIS": _validate_eis_params,
    "CDL": _validate_cdl_params,
    "CA": _validate_ca_params,
}


def validate_mode_payload(mode: str, params: Dict[str, Any]) -> ValidationResult:
    """Run the configured validator for a mode or raise UnsupportedModeError."""

    mode_key = (mode or "").upper()
    validator = _MODE_VALIDATORS.get(mode_key)
    if not validator:
        raise UnsupportedModeError(f"Unsupported mode '{mode}'.")

    return validator(dict(params or {}))
