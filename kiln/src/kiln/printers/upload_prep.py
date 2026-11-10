"""Getting sliced G-code into the shape the target printer accepts."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# : Slicer overrides a wrapped upload needs
BAMBU_SLICE_OVERRIDES: dict[str, str] = {
    "use_relative_e_distances": "1",
    "start_gcode": "",
    "end_gcode": "",
}


def adapter_wraps_gcode(adapter: Any) -> bool:
    """True when ``adapter`` uploads a wrapped 3MF rather than raw G-code."""
    return hasattr(adapter, "wrap_gcode_as_3mf")


def slice_overrides_for_adapter(adapter: Any) -> dict[str, str]:
    """The pre-slice overrides the target needs; empty for a raw-gcode printer."""
    return dict(BAMBU_SLICE_OVERRIDES) if adapter_wraps_gcode(adapter) else {}


def _complete_raw_gcode(
    adapter: Any, gcode_path: str, stl_paths: list[str] | None,
) -> None:
    """Give a raw-G-code printer's file its preview and its weight, in place."""
    if not gcode_path.lower().endswith((".gcode", ".gco", ".g")):
        return
    try:
        from kiln.printers.gcode_complete import (
            complete_gcode_for_printer,
            family_for_adapter,
            printer_model_for_adapter,
        )

        complete_gcode_for_printer(
            gcode_path,
            family_for_adapter(adapter),
            printer_model=printer_model_for_adapter(adapter),
            model_path=stl_paths[0] if stl_paths else None,
        )
    except Exception:  # noqa: BLE001 — the upload door still refuses an incomplete file
        logger.warning("G-code completion failed for %s", gcode_path, exc_info=True)


def prepare_upload_for_adapter(
    adapter: Any,
    gcode_path: str,
    *,
    stl_paths: list[str] | None = None,
    hotend_temp: int | None = None,
    bed_temp: int | None = None,
    quiet_start: dict[str, Any] | None = None,
    lift_floor_mm: float | None = None,
) -> tuple[str, bool]:
    """``(upload_path, wrapped)`` — the file to upload for this adapter."""
    if not adapter_wraps_gcode(adapter) or not gcode_path.lower().endswith(".gcode"):
        if quiet_start is not None:
            msg = "the quiet start needs a printer whose files Kiln wraps; this one starts raw G-code"
            raise ValueError(msg)
        _complete_raw_gcode(adapter, gcode_path, stl_paths)
        return gcode_path, False
    kwargs: dict[str, Any] = {}
    if stl_paths:
        kwargs["stl_paths"] = stl_paths
    if hotend_temp is not None:
        kwargs["hotend_temp"] = int(hotend_temp)
    if bed_temp is not None:
        kwargs["bed_temp"] = int(bed_temp)
    if quiet_start is not None:
        kwargs["quiet_start"] = quiet_start
    if lift_floor_mm is not None:
        kwargs["lift_floor_mm"] = float(lift_floor_mm)
    try:
        wrapped = adapter.wrap_gcode_as_3mf(gcode_path, **kwargs)
    except Exception:  # noqa: BLE001 — never lose the upload to the wrap
        if quiet_start is not None or lift_floor_mm is not None:
            raise
        logger.warning("Bambu 3MF wrapping failed, uploading raw gcode", exc_info=True)
        return gcode_path, False
    logger.info("Wrapped gcode as Bambu 3MF: %s", wrapped)
    return wrapped, True
