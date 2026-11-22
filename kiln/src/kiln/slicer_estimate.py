"""The slicer's own print-time estimate, kept on a design version's record.

A version that has a sliced file (G-code, or a 3MF carrying a sliced plate)
keeps what that file says the print will take, under
``provenance[RECORD_KEY]``: ``{"seconds": int}`` plus ``"slicer"`` when the
file names the slicer that wrote it, and ``"plates"`` when a 3MF holds
several, whose seconds are then every plate's together.  The figure is read by the one reader of
a slicer's time (:func:`kiln.gcode.slicer_print_time`) from the same lines
every reader of a file's own figures sees
(:func:`kiln.file_metadata.sliced_gcode_lines`).  A file that carries a print
on from a layer states no time of its own
(:func:`kiln.gcode.continues_a_print`), so none is kept for it.

The figure is the file printed as written, at the speed it was sliced at;
the sentences say so.

It is one version's own fact: a version made from another never carries its
parent's (``kiln.design_recipe.VERSION_STORY_KEYS``).  A file that states no
time leaves the key off, and the sentences below say nothing about it.
"""

from __future__ import annotations

from typing import Any

#: Where a version's record keeps the slicer's estimate.
RECORD_KEY = "slicer_estimate"

#: A change smaller than this is said as no change.
_SAME_WITHIN_SECONDS = 60


def read_estimate(sliced_path: str | None) -> dict[str, Any] | None:
    """``{"seconds", "slicer"?, "plates"?}`` as the sliced file at
    *sliced_path* states them, or ``None`` for a mesh, an unreadable file or
    one stating no time.

    A sliced 3MF of several plates keeps the time to print every plate, the
    sum of each plate's own time, with ``"plates"`` saying how many; when
    any plate states no time, none is kept.  This is the version's figure,
    never one plate's time at a printer (that is
    :func:`kiln.learned_print_time.slicer_seconds_for`).  Never raises."""
    if not sliced_path:
        return None
    try:
        from kiln.file_metadata import every_plate_gcode_lines
        from kiln.gcode import slicer_made_by, slicer_print_time

        plates = every_plate_gcode_lines(sliced_path)
        if not plates:
            return None
        total = 0.0
        for lines in plates:
            printed = slicer_print_time("\n".join(lines)) if lines else None
            if printed is None or printed.seconds <= 0:
                return None
            total += printed.seconds
        estimate: dict[str, Any] = {"seconds": int(total)}
        if len(plates) > 1:
            estimate["plates"] = len(plates)
        made_by = slicer_made_by("\n".join(plates[0]))
        if made_by is not None and made_by.label:
            estimate["slicer"] = made_by.label
        return estimate
    except Exception:  # noqa: BLE001 -- a file Kiln cannot read states no time
        return None


def keep_estimate(provenance: dict[str, Any] | None, sliced_path: str | None) -> dict[str, Any] | None:
    """*provenance* (a copy) holding the estimate *sliced_path* states, and
    no other: a value already there, or handed in by a caller, is replaced by
    Kiln's reading or dropped when the file states none.  ``None`` when
    nothing is left."""
    kept = {k: v for k, v in (provenance or {}).items() if k != RECORD_KEY}
    estimate = read_estimate(sliced_path)
    if estimate is not None:
        kept[RECORD_KEY] = estimate
    return kept or None


def estimate_on(provenance: Any) -> dict[str, Any] | None:
    """The estimate a version's *provenance* keeps, or ``None``."""
    if not isinstance(provenance, dict):
        return None
    estimate = provenance.get(RECORD_KEY)
    if not isinstance(estimate, dict):
        return None
    seconds = estimate.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return estimate


def _duration(seconds: float) -> str:
    from kiln.generation.validation import _human_duration

    return _human_duration(float(seconds))


def estimate_sentence(provenance: Any) -> str | None:
    """``"About 3h 1m at the sliced speed, the slicer's estimate."`` for a version's
    *provenance*, or ``None`` when it keeps no estimate."""
    estimate = estimate_on(provenance)
    if estimate is None:
        return None
    plates = estimate.get("plates")
    if isinstance(plates, int) and not isinstance(plates, bool) and plates > 1:
        return (
            f"About {_duration(estimate['seconds'])} for all {plates} plates at the sliced speed, "
            "the slicer's estimate."
        )
    return f"About {_duration(estimate['seconds'])} at the sliced speed, the slicer's estimate."


def estimate_change(provenance_a: Any, provenance_b: Any) -> str | None:
    """How the slicer's estimate moved from version A to version B, in one
    sentence, or ``None`` unless both versions keep one."""
    a, b = estimate_on(provenance_a), estimate_on(provenance_b)
    if a is None or b is None:
        return None
    before, after = float(a["seconds"]), float(b["seconds"])
    delta = after - before
    if abs(delta) < _SAME_WITHIN_SECONDS:
        sentence = f"At the sliced speed, the slicer's estimate stayed at about {_duration(after)}."
    else:
        longer = "longer" if delta > 0 else "shorter"
        sentence = (
            f"At the sliced speed, the slicer's estimate went from about {_duration(before)} to about "
            f"{_duration(after)}, {_duration(abs(delta))} {longer}."
        )
    slicer_a, slicer_b = a.get("slicer"), b.get("slicer")
    if slicer_a and slicer_b and slicer_a != slicer_b:
        sentence += (
            " The two were sliced with different slicers or slicer versions, so part of the change "
            "may be the slicer's."
        )
    return sentence


__all__ = [
    "RECORD_KEY",
    "estimate_change",
    "estimate_on",
    "estimate_sentence",
    "keep_estimate",
    "read_estimate",
]
