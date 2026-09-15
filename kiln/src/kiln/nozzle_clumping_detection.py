"""One sentence about a printer's own nozzle-clumping-detection switch.

A printer that can feel for a blob on its nozzle does it by driving the
toolhead off the bed and probing, and it leaks a little filament each
time.  The printer's own screen says so the moment the switch is turned
on, and asks for the purge tower.  Kiln reads the switch off the machine
(:meth:`kiln.printers.base.PrinterAdapter.read_nozzle_clumping_detection`)
and every door that shows printer state -- the status read, the pre-flight
-- says the same thing through this module, so no door grows its own idea
of what an ON reading asks of the slice.

Three states, and they are said differently on purpose.  ON carries the
printer's own warning and what a user can do about it.  OFF says the probe
will not run.  UNVERIFIED -- the machine reports the channel but nobody
has confirmed what it means on this model -- is reported as unknown, with
the reason, and never as a verdict either way: a reading nobody could
verify is not "off".

The detector is a convenience the printer offers, not a fail-safe, and
nothing here presents it as one.  Which models carry the switch, where the
probe lands, and which slicing modes defeat it are per-model facts kept
with kiln-pro's printer intelligence (https://kiln3d.com); this module
states only what the printer itself says.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.printers.base import NozzleClumpingDetection

logger = logging.getLogger(__name__)

CHECK_NAME = "nozzle_clumping_detection"

#: What the printer's own screen states when the switch is turned on, and the
#: two facts Kiln adds: its slicers emit no prime tower for a single-filament
#: file (measured with PrusaSlicer 2.9.4 and OrcaSlicer 2.3.2 on the A1
#: profile, 2026-09-14), and its per-print skip is the printer's own switch:
#: measured 2026-09-15 on the A1, the ``print_option`` command it sends turns
#: the switch OFF, so the adapter reads it first and turns it back on when
#: the print ends (``BambuAdapter._skip_nozzle_detection_for_print``).
_ON_STATEMENT = (
    "Nozzle clumping detection is on for this printer, read off its own "
    "switch. The printer's own screen warns that enabling it may leave "
    "traces on the model and asks for the purge (prime) tower to be on when "
    "slicing: each probe leaks a little filament, and without a tower that "
    "ooze lands on the print. Kiln's slicers add no prime tower to a "
    "single-filament file, so for a single-colour print either accept the "
    "marks, slice with a prime tower in the printer maker's own slicer, or "
    "turn the switch off on the printer's screen. Kiln can ask the printer "
    "to skip the probe with start_print(nozzle_clog_detect=False): that "
    "turns the printer's own switch off for the print, and Kiln turns it back "
    "on when the print ends. "
    "The detector is not a fail-safe: do not leave a print unattended on its "
    "strength."
)

_OFF_STATEMENT = (
    "Nozzle clumping detection is off for this printer, read off its own "
    "switch: the probe will not run this print, and the printer will not "
    "feel for a blob on the nozzle."
)


def read_switch(adapter: Any) -> NozzleClumpingDetection | None:
    """The switch as *adapter* reports it, or ``None`` when it cannot say.

    Guards the door: a backend without the method, a read that raises, or
    anything that is not a real reading is ``None`` -- a status read must
    never fail on account of a sentence beside it.
    """
    read = getattr(adapter, "read_nozzle_clumping_detection", None)
    if not callable(read):
        return None
    try:
        reading = read()
    except Exception:  # noqa: BLE001 -- context beside the reading, never the reading
        logger.debug("nozzle clumping detection read unavailable", exc_info=True)
        return None
    return reading if isinstance(reading, NozzleClumpingDetection) else None


def statement(reading: NozzleClumpingDetection) -> str:
    """The one sentence for *reading*, in the words the three states get."""
    if reading.enabled is True:
        return _ON_STATEMENT
    if reading.enabled is False:
        return _OFF_STATEMENT
    reason = (reading.unverified_reason or "").strip().rstrip(".")
    return (
        "Whether nozzle clumping detection is on for this printer is not "
        f"verified: {reason}. Kiln reports it as unknown rather than as a "
        "verdict either way; the switch is on the printer's own screen."
    )


def status_block(reading: NozzleClumpingDetection) -> dict[str, Any]:
    """The ``nozzle_clumping_detection`` block of a status read."""
    block: dict[str, Any] = {
        "enabled": reading.enabled,
        "read_from": reading.source,
        "value_kind": "switch",
        "state_age_seconds": reading.age_seconds,
        "stale_after_seconds": reading.stale_after_seconds,
        "firmware_version": reading.firmware_version,
        "statement": statement(reading),
    }
    if reading.enabled is None:
        block["unverified_reason"] = reading.unverified_reason
    return block


def preflight_entry(reading: NozzleClumpingDetection) -> dict[str, Any]:
    """The pre-flight check for *reading*.  Always advisory: the switch is
    the user's choice, and the print is not unsafe either way -- the cost
    of an ON reading is marks on the part, which the message names."""
    return {
        "name": CHECK_NAME,
        "passed": True,
        "advisory": True,
        "enabled": reading.enabled,
        "message": statement(reading),
    }


# ---------------------------------------------------------------------------
# What the sliced FILE says
# ---------------------------------------------------------------------------

#: Config-comment keys that name a mode the probe does not run in, as the
#: slicers spell them: PrusaSlicer's ``spiral_vase`` / ``complete_objects``,
#: Orca's and Bambu Studio's ``spiral_mode`` / ``print_sequence``.
_SPIRAL_KEYS = ("spiral_vase", "spiral_mode")
_BY_OBJECT_KEYS = ("complete_objects",)
_MAX_SCAN_LINES = 400_000


def _config_value(line: str) -> tuple[str, str] | None:
    """``("key", "value")`` for a ``; key = value`` config comment."""
    body = line[1:].strip() if line.startswith(";") else ""
    if "=" not in body:
        return None
    key, _, value = body.partition("=")
    return key.strip(), value.strip()


def _truthy(value: str) -> bool:
    return value.strip().strip('"').casefold() in {"1", "true", "yes", "on"}


def _scan_gcode(text: str) -> dict[str, Any]:
    """One pass over G-code text: a prime tower feature label, the mode
    the config comments declare, and the print-region footprint."""
    from kiln.printers.bed_fit import compute_gcode_bbox  # noqa: F401 -- footprint below
    from kiln.slicer_geometry import classify_feature

    tower = False
    spiral = by_object = False
    seq_by_object = False
    for n, line in enumerate(text.splitlines()):
        if n > _MAX_SCAN_LINES:
            break
        if not line.startswith(";"):
            continue
        upper = line[:12].upper()
        if upper.startswith(";TYPE:") or upper.startswith("; FEATURE:") or upper.startswith(";FEATURE:"):
            label = line.split(":", 1)[1]
            if classify_feature(label) == "prime_tower":
                tower = True
            continue
        cfg = _config_value(line)
        if cfg is None:
            continue
        key, value = cfg
        if key in _SPIRAL_KEYS and _truthy(value):
            spiral = True
        elif key in _BY_OBJECT_KEYS and _truthy(value):
            by_object = True
        elif key == "print_sequence" and "object" in value.casefold():
            seq_by_object = True
    if spiral:
        mode = "spiral_vase"
    elif by_object or seq_by_object:
        mode = "by_object"
    else:
        mode = "normal"
    return {"prime_tower_in_file": tower, "print_mode": mode}


def _footprint_of_gcode(path: str) -> dict[str, float] | None:
    from kiln.printers.bed_fit import compute_gcode_bbox

    bbox = compute_gcode_bbox(path)
    if not bbox:
        return None
    try:
        return {k: float(bbox[k]) for k in ("x_min", "y_min", "x_max", "y_max")}
    except (KeyError, TypeError, ValueError):
        return None


def file_facts(file_path: str) -> dict[str, Any] | None:
    """What a sliced file says about the probe's costs, or ``None`` when the
    file cannot be read.

    ``prime_tower_in_file`` -- whether the slicer laid a tower (the
    feature label, whatever dialect wrote it); ``print_mode`` --
    ``"spiral_vase"`` / ``"by_object"`` / ``"normal"`` from the slicer's
    own config comments (a Bambu 3MF's plate metadata ``is_seq_print``
    counts too); ``footprint`` -- the print-region bounding box in bed
    coordinates, so a door that knows where a probe lands can say whether
    the part sits there.  A 3MF is read through the G-code it carries.
    """
    import contextlib
    import json
    import os
    import tempfile
    import zipfile

    try:
        if not os.path.isfile(file_path):
            return None
        if file_path.casefold().endswith(".3mf"):
            with zipfile.ZipFile(file_path) as zf:
                names = zf.namelist()
                gcode_members = sorted(n for n in names if n.casefold().endswith(".gcode"))
                if not gcode_members:
                    return None
                text = zf.read(gcode_members[0]).decode("utf-8", errors="replace")
                seq = False
                for n in names:
                    if n.startswith("Metadata/plate_") and n.endswith(".json"):
                        # One bad plate file is not the answer.
                        with contextlib.suppress(Exception):
                            seq = seq or bool(json.loads(zf.read(n)).get("is_seq_print"))
            facts = _scan_gcode(text)
            if seq and facts["print_mode"] == "normal":
                facts["print_mode"] = "by_object"
            with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as tmp:
                tmp.write(text)
            try:
                facts["footprint"] = _footprint_of_gcode(tmp.name)
            finally:
                os.unlink(tmp.name)
            return facts
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        facts = _scan_gcode(text)
        facts["footprint"] = _footprint_of_gcode(file_path)
        return facts
    except Exception:  # noqa: BLE001 -- a file that cannot be read says nothing
        logger.debug("nozzle clumping detection: file facts unavailable", exc_info=True)
        return None


_NO_TOWER = (
    " This file has no prime tower, so each probe's ooze will land on the print."
)


def preflight_entry_for_file(
    reading: NozzleClumpingDetection, facts: dict[str, Any] | None
) -> dict[str, Any]:
    """The pre-flight check for *reading* with what the file says beside it."""
    entry = preflight_entry(reading)
    if facts is None:
        return entry
    entry["file"] = facts
    if reading.enabled is True and facts.get("prime_tower_in_file") is False:
        entry["message"] += _NO_TOWER
    return entry


__all__ = [
    "CHECK_NAME",
    "file_facts",
    "preflight_entry",
    "preflight_entry_for_file",
    "read_switch",
    "statement",
    "status_block",
]
