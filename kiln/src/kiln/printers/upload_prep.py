"""Getting sliced G-code into the shape the target printer accepts.

Bambu machines do not run raw PrusaSlicer G-code: the file has to be
sliced with relative extrusion and empty start/end blocks, then wrapped in
a 3MF carrying BambuStudio's own start/end sequences (``wrap_gcode_as_3mf``
on the adapter).  Every other adapter uploads the G-code as-is.

Three doors send a sliced file to a printer — ``slice_and_print``,
``reslice_and_print`` and ``quick_print`` — and each carried its own copy of
this decision, or none: ``quick_print`` uploaded raw ``.gcode`` to a P1S and
started a job the firmware ignored (2026-08-23, seen in our own install
telemetry).  One helper, every door.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Slicer overrides a wrapped upload needs.  ``wrap_gcode_as_3mf`` documents
#: them: relative E, and no slicer start/end block because the 3MF supplies
#: BambuStudio's.
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
    """Give a raw-G-code printer's file its preview and its weight, in place.

    The other half of what a wrap does for a Bambu.  ``slice_and_print``,
    ``reslice_and_print`` and ``quick_print`` all come through this helper,
    so the file each of them sends arrives complete rather than being
    refused at the upload door one step later.  Best-effort: a picture that
    cannot be drawn costs the picture, never the print.  See
    :mod:`kiln.printers.gcode_complete`.
    """
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
    """``(upload_path, wrapped)`` — the file to upload for this adapter.

    Wraps ``gcode_path`` into a 3MF when the adapter needs one.  A wrap
    failure logs and falls back to the raw file so an upload is never lost
    to a thumbnail problem; the caller's post-wrap safety verification still
    runs on whatever comes back.  With a *quiet_start* plan or a
    *lift_floor_mm* (a part is still on the plate) there is no fallback: a
    raw file, or one wrapped without the plan, would carry the vendor's
    start onto the occupied plate, so the wrap error is raised instead.
    """
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
