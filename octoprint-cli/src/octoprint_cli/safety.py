"""Pre-flight checks and safety validation for autonomous 3D printing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from octoprint_cli.output import format_bytes, format_time

# Recognised G-code file extensions.
_GCODE_EXTENSIONS = {".gcode", ".gco", ".g"}

# Size thresholds (bytes).
_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
_LARGE_FILE_THRESHOLD = 500 * 1024 * 1024  # 500 MB


# ------------------------------------------------------------------
# Individual checks
# ------------------------------------------------------------------


def check_printer_ready(client: Any) -> dict[str, Any]:
    """Verify the printer is connected, operational, idle, and error-free.

    Args:
        client: An :class:`~octoprint_cli.client.OctoPrintClient` instance.

    Returns:
        A dict with keys ``ready`` (bool), ``checks`` (list of individual
        check results), and ``errors`` (list of error messages).
    """
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    # -- Check 1: printer connected --
    conn_result = client.get_connection()
    if conn_result["success"]:
        conn_state = conn_result["data"].get("current", {}).get("state", "")
        is_connected = conn_state.lower() not in ("closed", "closed_with_error", "")
        msg = f"Connection state: {conn_state}" if conn_state else "No connection state reported"
        checks.append({"name": "printer_connected", "passed": is_connected, "message": msg})
        if not is_connected:
            errors.append(f"Printer is not connected (state: {conn_state})")
    else:
        err_msg = conn_result["error"]["message"]
        checks.append({"name": "printer_connected", "passed": False, "message": err_msg})
        errors.append(f"Failed to query connection: {err_msg}")

    # -- Check 2 & 3 & 4: operational, not printing, no errors --
    printer_result = client.get_printer_state()
    if printer_result["success"]:
        state_data = printer_result["data"].get("state", {})
        flags = state_data.get("flags", {})
        state_text = state_data.get("text", "Unknown")

        # Operational
        is_operational = bool(flags.get("operational", False))
        checks.append(
            {
                "name": "printer_operational",
                "passed": is_operational,
                "message": f"Printer state: {state_text}",
            }
        )
        if not is_operational:
            errors.append(f"Printer is not operational (state: {state_text})")

        # Not currently printing
        is_printing = bool(flags.get("printing", False)) or bool(flags.get("pausing", False))
        not_printing = not is_printing
        printing_msg = "No active print job" if not_printing else f"Printer is currently printing (state: {state_text})"
        checks.append(
            {
                "name": "not_printing",
                "passed": not_printing,
                "message": printing_msg,
            }
        )
        if not not_printing:
            errors.append(f"Printer is busy (state: {state_text})")

        # No errors
        has_error = bool(flags.get("error", False)) or bool(flags.get("closedOrError", False))
        no_error = not has_error
        error_msg = "No printer errors detected" if no_error else f"Printer reports an error (state: {state_text})"
        checks.append(
            {
                "name": "no_errors",
                "passed": no_error,
                "message": error_msg,
            }
        )
        if not no_error:
            errors.append(f"Printer error detected (state: {state_text})")
    else:
        err_msg = printer_result["error"]["message"]
        for name in ("printer_operational", "not_printing", "no_errors"):
            checks.append({"name": name, "passed": False, "message": err_msg})
        errors.append(f"Failed to query printer state: {err_msg}")

    ready = all(c["passed"] for c in checks)
    return {"ready": ready, "checks": checks, "errors": errors}


def check_temperatures(
    client: Any,
    max_tool_temp: float = 260,
    max_bed_temp: float = 110,
) -> dict[str, Any]:
    """Check that current and target temperatures are within safe limits.

    Args:
        client: An :class:`~octoprint_cli.client.OctoPrintClient` instance.
        max_tool_temp: Maximum acceptable tool (hotend) temperature in Celsius.
        max_bed_temp: Maximum acceptable bed temperature in Celsius.

    Returns:
        A dict with keys ``safe`` (bool), ``tool`` and ``bed`` temperature
        info, and ``warnings`` (list of strings).
    """
    warnings: list[str] = []

    tool_info: dict[str, float | None] = {"actual": 0.0, "target": 0.0}
    bed_info: dict[str, float | None] = {"actual": 0.0, "target": 0.0}

    printer_result = client.get_printer_state()
    if not printer_result["success"]:
        err_msg = printer_result["error"]["message"]
        warnings.append(f"Could not read temperatures: {err_msg}")
        return {
            "safe": False,
            "tool": tool_info,
            "bed": bed_info,
            "warnings": warnings,
        }

    temperature_data = printer_result["data"].get("temperature", {})

    # Tool temperature (tool0 is the primary extruder).
    tool0 = temperature_data.get("tool0", {})
    tool_actual = tool0.get("actual", 0.0) or 0.0
    tool_target = tool0.get("target", 0.0) or 0.0
    tool_info = {"actual": float(tool_actual), "target": float(tool_target)}

    # Bed temperature.
    bed = temperature_data.get("bed", {})
    bed_actual = bed.get("actual", 0.0) or 0.0
    bed_target = bed.get("target", 0.0) or 0.0
    bed_info = {"actual": float(bed_actual), "target": float(bed_target)}

    safe = True

    # Check tool temperature limits.
    if tool_actual > max_tool_temp:
        warnings.append(f"Tool temperature ({tool_actual:.1f}C) exceeds safe maximum ({max_tool_temp:.0f}C)")
        safe = False
    if tool_target > max_tool_temp:
        warnings.append(f"Tool target temperature ({tool_target:.1f}C) exceeds safe maximum ({max_tool_temp:.0f}C)")
        safe = False

    # Check bed temperature limits.
    if bed_actual > max_bed_temp:
        warnings.append(f"Bed temperature ({bed_actual:.1f}C) exceeds safe maximum ({max_bed_temp:.0f}C)")
        safe = False
    if bed_target > max_bed_temp:
        warnings.append(f"Bed target temperature ({bed_target:.1f}C) exceeds safe maximum ({max_bed_temp:.0f}C)")
        safe = False

    # Warn if tool is heated but bed isn't (or vice versa).
    if tool_target > 0 and bed_target == 0:
        warnings.append("Tool temperature is set but bed temperature is not. Most prints require both.")
    if bed_target > 0 and tool_target == 0:
        warnings.append("Bed temperature is set but tool temperature is not. Most prints require both.")

    return {
        "safe": safe,
        "tool": tool_info,
        "bed": bed_info,
        "warnings": warnings,
    }


def validate_file(file_path: str) -> dict[str, Any]:
    """Validate a local G-code file before upload.

    Checks that the file exists, is readable, has a recognised extension,
    has a non-zero size, and is within the 2 GB limit.

    Args:
        file_path: Path to the local file on disk.

    Returns:
        A dict with keys ``valid`` (bool), ``errors``, ``warnings``, and
        ``info`` (size and extension metadata).
    """
    errors: list[str] = []
    warnings: list[str] = []
    info: dict[str, Any] = {"size_bytes": 0, "size_human": "0 B", "extension": ""}

    path = Path(file_path)

    # Check existence.
    if not path.exists():
        errors.append(f"File not found: {file_path}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    # Check it is a file (not a directory).
    if not path.is_file():
        errors.append(f"Path is not a regular file: {file_path}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    # Check readability.
    try:
        with path.open("rb") as fh:
            fh.read(1)
    except PermissionError:
        errors.append(f"File is not readable (permission denied): {file_path}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}
    except OSError as exc:
        errors.append(f"Cannot read file: {exc}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    # Extension check.
    ext = path.suffix.lower()
    info["extension"] = ext
    if ext not in _GCODE_EXTENSIONS:
        errors.append(f"Unsupported file extension '{ext}'. Expected one of: {', '.join(sorted(_GCODE_EXTENSIONS))}")

    # Size checks.
    try:
        size = path.stat().st_size
    except OSError as exc:
        errors.append(f"Could not determine file size: {exc}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    info["size_bytes"] = size
    info["size_human"] = format_bytes(size)

    if size == 0:
        errors.append("File is empty (0 bytes)")
    elif size >= _MAX_FILE_SIZE:
        errors.append(
            f"File is too large ({format_bytes(size)}). Maximum allowed size is {format_bytes(_MAX_FILE_SIZE)}."
        )
    elif size >= _LARGE_FILE_THRESHOLD:
        warnings.append(f"File is very large ({format_bytes(size)}). Upload and slicing analysis may take a while.")

    valid = len(errors) == 0
    return {"valid": valid, "errors": errors, "warnings": warnings, "info": info}


def estimate_resources(
    client: Any,
    file_path_on_server: str,
    location: str = "local",
) -> dict[str, Any]:
    """Retrieve estimated print time and filament usage from OctoPrint.

    OctoPrint performs file analysis asynchronously after upload, so this
    information may not be available immediately.

    Args:
        client: An :class:`~octoprint_cli.client.OctoPrintClient` instance.
        file_path_on_server: The file path as known to OctoPrint.
        location: Storage location (``"local"`` or ``"sdcard"``).

    Returns:
        A dict with ``available`` (bool), ``estimated_print_time``,
        ``estimated_print_time_seconds``, and ``filament`` usage info.
    """
    default_result: dict[str, Any] = {
        "estimated_print_time": "Unknown",
        "estimated_print_time_seconds": None,
        "filament": {"length_mm": None, "volume_cm3": None},
        "available": False,
    }

    result = client.get_file_info(location, file_path_on_server)
    if not result["success"]:
        err_msg = result["error"]["message"]
        default_result["estimated_print_time"] = f"Error: {err_msg}"
        return default_result

    data = result["data"]
    analysis = data.get("gcodeAnalysis")
    if analysis is None:
        default_result["estimated_print_time"] = "File not yet analyzed by OctoPrint"
        return default_result

    # Print time.
    est_seconds = analysis.get("estimatedPrintTime")
    print_time_str: str
    if est_seconds is not None:
        est_seconds_int = int(est_seconds)
        print_time_str = format_time(est_seconds_int)
    else:
        est_seconds_int = None  # type: ignore[assignment]
        print_time_str = "Unknown"

    # Filament usage -- OctoPrint stores per-tool, we aggregate tool0.
    filament_data = analysis.get("filament", {})
    tool0_filament = filament_data.get("tool0", {})
    length_mm = tool0_filament.get("length")
    volume_cm3 = tool0_filament.get("volume")

    return {
        "estimated_print_time": print_time_str,
        "estimated_print_time_seconds": est_seconds_int,
        "filament": {
            "length_mm": float(length_mm) if length_mm is not None else None,
            "volume_cm3": float(volume_cm3) if volume_cm3 is not None else None,
        },
        "available": True,
    }


# ------------------------------------------------------------------
# Cancellation check
# ------------------------------------------------------------------


def check_can_cancel(client: Any) -> dict[str, Any]:
    """Determine whether there is an active job that can be cancelled.

    Args:
        client: An :class:`~octoprint_cli.client.OctoPrintClient` instance.

    Returns:
        A dict with ``can_cancel`` (bool), ``current_state`` (str), and
        ``message`` (str).
    """
    printer_result = client.get_printer_state()
    if not printer_result["success"]:
        err_msg = printer_result["error"]["message"]
        return {
            "can_cancel": False,
            "current_state": "unknown",
            "message": f"Could not query printer state: {err_msg}",
        }

    state_data = printer_result["data"].get("state", {})
    flags = state_data.get("flags", {})
    state_text = state_data.get("text", "Unknown")

    is_printing = bool(flags.get("printing", False))
    is_paused = bool(flags.get("paused", False)) or bool(flags.get("pausing", False))
    can_cancel = is_printing or is_paused

    if can_cancel:
        message = f"Active job detected (state: {state_text}). Cancellation is possible."
    else:
        message = f"No active job to cancel (state: {state_text})."

    return {
        "can_cancel": can_cancel,
        "current_state": state_text,
        "message": message,
    }


# ------------------------------------------------------------------
# Combined pre-flight
# ------------------------------------------------------------------


def preflight_check(
    client: Any,
    file_path: str | None = None,
    file_on_server: str | None = None,
    location: str = "local",
) -> dict[str, Any]:
    """Run all safety checks and return a combined report.

    Args:
        client: An :class:`~octoprint_cli.client.OctoPrintClient` instance.
        file_path: Optional path to a local G-code file to validate.
        file_on_server: Optional path (on OctoPrint) to retrieve resource
            estimates for.
        location: Storage location for *file_on_server*.

    Returns:
        A combined dict with ``ready`` (bool), individual check results,
        and a human-readable ``summary`` string.
    """
    printer = check_printer_ready(client)
    temperatures = check_temperatures(client)

    file_result: dict[str, Any] | None = None
    if file_path is not None:
        file_result = validate_file(file_path)

    resources_result: dict[str, Any] | None = None
    if file_on_server is not None:
        resources_result = estimate_resources(client, file_on_server, location)

    # Determine overall readiness.
    ready = printer["ready"] and temperatures["safe"]
    if file_result is not None and not file_result["valid"]:
        ready = False

    # Build a one-line summary.
    issues: list[str] = []
    if not printer["ready"]:
        issues.append("printer not ready")
    if not temperatures["safe"]:
        issues.append("temperature warnings")
    if file_result is not None and not file_result["valid"]:
        issues.append("file validation failed")

    if ready:
        summary = "All pre-flight checks passed. Ready to print."
    else:
        summary = "Pre-flight checks failed: " + "; ".join(issues) + "."

    result: dict[str, Any] = {
        "ready": ready,
        "printer": printer,
        "temperatures": temperatures,
        "summary": summary,
    }

    if file_result is not None:
        result["file"] = file_result
    if resources_result is not None:
        result["resources"] = resources_result

    return result
