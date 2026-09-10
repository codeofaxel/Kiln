"""Getting sliced G-code into the shape the target printer accepts.

Bambu machines do not run raw PrusaSlicer G-code: the file has to be
sliced with relative extrusion and empty start/end blocks, then wrapped in
a 3MF carrying BambuStudio's own start/end sequences (``wrap_gcode_as_3mf``
on the adapter).  Every other adapter uploads the G-code as-is.

Three doors send a sliced file to a printer — ``slice_and_print``,
``reslice_and_print`` and ``quick_print`` — and each carried its own copy of
this decision, or none: ``quick_print`` uploaded raw ``.gcode`` to a P1S and
started a job the firmware ignored (2026-08-23, the founder dashboard's
bambu_p1s row).  One helper, every door.
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


def prepare_upload_for_adapter(
    adapter: Any,
    gcode_path: str,
    *,
    stl_paths: list[str] | None = None,
    hotend_temp: int | None = None,
    bed_temp: int | None = None,
) -> tuple[str, bool]:
    """``(upload_path, wrapped)`` — the file to upload for this adapter.

    Wraps ``gcode_path`` into a 3MF when the adapter needs one.  A wrap
    failure logs and falls back to the raw file so an upload is never lost
    to a thumbnail problem; the caller's post-wrap safety verification still
    runs on whatever comes back.
    """
    if not adapter_wraps_gcode(adapter) or not gcode_path.lower().endswith(".gcode"):
        return gcode_path, False
    kwargs: dict[str, Any] = {}
    if stl_paths:
        kwargs["stl_paths"] = stl_paths
    if hotend_temp is not None:
        kwargs["hotend_temp"] = int(hotend_temp)
    if bed_temp is not None:
        kwargs["bed_temp"] = int(bed_temp)
    try:
        wrapped = adapter.wrap_gcode_as_3mf(gcode_path, **kwargs)
    except Exception:  # noqa: BLE001 — never lose the upload to the wrap
        logger.warning("Bambu 3MF wrapping failed, uploading raw gcode", exc_info=True)
        return gcode_path, False
    logger.info("Wrapped gcode as Bambu 3MF: %s", wrapped)
    return wrapped, True
