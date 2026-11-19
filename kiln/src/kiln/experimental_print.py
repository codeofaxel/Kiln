"""A print that is an experiment, read from the file that prints.

One kind today: a file sliced for nozzles of different sizes at once
(:func:`kiln.slicer_profiles.mixed_size_experiment`).  The file states it
itself -- the nozzle sizes it was sliced for -- so every door that starts a
print reads the same answer whichever door made the file.

Three things follow, and each has its one reader here:

* the person is asked about THIS print, every time: a yes that covers
  prints in general (a standing window, always allow) does not start it
  (:func:`needs_its_own_yes`);
* the approval they are shown says it is experimental (:func:`approval_line`);
* the print's record carries the word (:func:`recorded_word`), and when it
  ends the person is asked how it came out (:mod:`kiln.ending_question`).

Reads a file; sends nothing anywhere.  Never raises.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from kiln.slicer_profiles import MIXED_SIZES_EXPERIMENT

logger = logging.getLogger(__name__)

#: The kinds of experiment a print's record may carry.
KINDS: frozenset[str] = frozenset({MIXED_SIZES_EXPERIMENT})
#: The code of a start held because the yes on record is not for this print.
NEEDS_A_YES_CODE = "EXPERIMENT_NEEDS_A_YES"
#: Two sizes closer than this are one size.
_SAME_SIZE_MM = 0.005


def mixed_sizes_of_lines(lines: Iterable[str] | None) -> tuple[float, ...] | None:
    """The nozzle sizes the sliced file whose settings *lines* are given was
    laid out for, one per nozzle, when they differ; ``None`` for one size or
    a file that states none."""
    try:
        from kiln.gcode import slicer_nozzle_diameters

        statements = slicer_nozzle_diameters("\n".join(lines)) if lines else ()
    except Exception:  # noqa: BLE001 -- lines nobody can read are no experiment
        logger.debug("experiment not read", exc_info=True)
        return None
    if not statements:
        return None
    sizes = statements[-1]
    if len(sizes) < 2 or max(sizes) - min(sizes) <= _SAME_SIZE_MM:
        return None
    return tuple(sizes)


def mixed_sizes_of_file(job_path: str | None) -> tuple[float, ...] | None:
    """:func:`mixed_sizes_of_lines` for the sliced file at *job_path*;
    ``None`` too for one that cannot be read."""
    if not job_path:
        return None
    try:
        from kiln.file_metadata import sliced_gcode_lines

        return mixed_sizes_of_lines(sliced_gcode_lines(job_path))
    except Exception:  # noqa: BLE001 -- an unreadable file is no experiment
        logger.debug("experiment not read from %s", job_path, exc_info=True)
        return None


_RECORDED = re.compile(r"^([a-z_]{3,40}):(\d\.\d{1,2}(?:,\d\.\d{1,2}){1,7})$")


def recorded_word(lines: Iterable[str] | None) -> str | None:
    """What a print's record keeps of the experiment its sent file is:
    ``mixed_nozzle_sizes:0.4,0.6``; ``None`` for any other file."""
    sizes = mixed_sizes_of_lines(lines)
    if not sizes:
        return None
    return f"{MIXED_SIZES_EXPERIMENT}:{','.join(f'{size:g}' for size in sizes)}"


def read_recorded(word: Any) -> tuple[str, tuple[float, ...]] | None:
    """``(kind, nozzle sizes)`` from a record's word (:func:`recorded_word`),
    or ``None`` for a record that carries none Kiln knows."""
    found = _RECORDED.match(word) if isinstance(word, str) else None
    if found is None or found.group(1) not in KINDS:
        return None
    return found.group(1), tuple(float(size) for size in found.group(2).split(","))


def _sizes_said(sizes: tuple[float, ...]) -> str:
    said = [f"{size:g} mm" for size in sizes]
    return f"{', '.join(said[:-1])} and {said[-1]}"


def approval_line(job_path: str | None) -> str | None:
    """What a person is told, beside the word "experimental", before an
    experimental print starts; ``None`` for any other file."""
    sizes = mixed_sizes_of_file(job_path)
    if not sizes:
        return None
    return (
        f"This print uses nozzles of different sizes at once ({_sizes_said(sizes)}), which Kiln has "
        "not proven on a printer. Watch the first layers; Kiln will ask how it came out."
    )


def needs_its_own_yes(job_path: str | None, source: str | None) -> str | None:
    """The sentence holding a start of the experimental file at *job_path*
    when the yes on record (*source*) covers prints in general and not this
    one; ``None`` when the file is no experiment or the yes is for this
    print."""
    from kiln.print_consent import SOURCE_ALWAYS, SOURCE_HOSTED_DELEGATION, SOURCE_STANDING_OPT_IN, SOURCE_WINDOW

    if source not in (SOURCE_ALWAYS, SOURCE_HOSTED_DELEGATION, SOURCE_STANDING_OPT_IN, SOURCE_WINDOW):
        return None
    line = approval_line(job_path)
    if line is None:
        return None
    return (
        f"Experimental: {line} An experimental print starts only on a yes to that print itself, and the yes on "
        "record here is one that lets prints start without asking. Nothing was started: ask the "
        "person about this print, and start it again once they have said yes to it."
    )


__all__ = [
    "KINDS",
    "NEEDS_A_YES_CODE",
    "approval_line",
    "mixed_sizes_of_file",
    "mixed_sizes_of_lines",
    "needs_its_own_yes",
    "read_recorded",
    "recorded_word",
]
