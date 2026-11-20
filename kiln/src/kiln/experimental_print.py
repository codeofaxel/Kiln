"""A print that starts only on a yes to that print itself, read from the
file that prints.

Two kinds, and every door that starts a print reads the same answer
whichever door made the file:

* an experiment: a file sliced for nozzles of different sizes at once
  (:func:`kiln.slicer_profiles.mixed_size_experiment`), which the file
  states itself (the nozzle sizes it was sliced for);
* a print the owner said to print anyway: an abrasive filament through a
  nozzle that is not wear-resistant, which this computer's record of the
  slice keeps (:func:`kiln.print_lineage.note_soft_nozzle`).

What follows, each with its one reader here:

* the person is asked about THIS print, every time: a yes that covers
  prints in general (a standing window, always allow) does not start it
  (:func:`needs_its_own_yes`, :func:`asks_every_time`);
* the approval they are shown says what the print is (:func:`approval_line`,
  :func:`print_anyway_line`), and an approval asked for before the print is
  sliced says it from the request's own words (:func:`asked_rows`); each
  row stands for one owner's word (:data:`ROW_WORDS`);
* an experiment's record carries the word (:func:`recorded_word`), and when
  it ends the person is asked how it came out (:mod:`kiln.ending_question`).

Reads files and this computer's slice records; sends nothing anywhere.
Never raises.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, NamedTuple

from kiln.slicer_profiles import MIXED_SIZES_EXPERIMENT

logger = logging.getLogger(__name__)

#: The kinds of experiment a print's record may carry.
KINDS: frozenset[str] = frozenset({MIXED_SIZES_EXPERIMENT})
#: The code of a start held because the yes on record is not for this print.
NEEDS_A_YES_CODE = "EXPERIMENT_NEEDS_A_YES"
#: The code of a start the owner said to print anyway, held because the yes
#: on record is not for this print.
PRINT_ANYWAY_NEEDS_A_YES_CODE = "PRINT_ANYWAY_NEEDS_A_YES"
#: Every code a start is held with for want of a yes to that print itself.
NEEDS_A_YES_CODES = (NEEDS_A_YES_CODE, PRINT_ANYWAY_NEEDS_A_YES_CODE)
#: The rows of an approval (:func:`kiln.print_consent.what_the_print_is`)
#: that only a yes to that print itself answers, each with the slice keyword
#: of the owner's word it stands for (:func:`kiln.slicer.owner_asks`).  A yes
#: given on an approval that showed the row is that word, for that print.
ROW_WORDS: Mapping[str, str] = MappingProxyType({
    "print_anyway": "soft_nozzle_confirmed",
    "experimental": "mixed_nozzle_experiment",
})
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


#: How a request names its filament for the row an approval shows before
#: the print is sliced: one short name, nothing else.
_NAMED_FILAMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +._-]{0,39}$")
#: The filament an approval names when the request names none it knows.
_SOME_ABRASIVE = "Abrasive filament"


def _joined(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _nozzle_said(nozzle: Mapping[str, Any]) -> str:
    made = str(nozzle.get("material") or "").replace("_", " ").strip()
    position = nozzle.get("position")
    if isinstance(position, int) and not isinstance(position, bool):
        return f"nozzle {position} ({made})" if made else f"nozzle {position}"
    return f"the {made} nozzle" if made else "the nozzle on record"


def print_anyway_said(kept: Mapping[str, Any] | None) -> str | None:
    """The row an approval shows for a slice the owner said to print
    anyway, from what its record keeps (:func:`kiln.print_lineage.soft_nozzle_of`):
    ``PLA-CF through the brass nozzle, which it wears out quickly.``;
    ``None`` for a record that names no filament or no nozzle."""
    if not isinstance(kept, Mapping):
        return None
    named = sorted({
        str(f.get("material") or "").strip()
        for f in kept.get("filaments") or () if isinstance(f, Mapping)
    } - {""})
    nozzles = [n for n in kept.get("nozzles") or () if isinstance(n, Mapping)]
    if not named or not nozzles:
        return None
    who = "it wears" if len(named) == 1 else "they wear"
    return f"{_joined(named)} through {_joined([_nozzle_said(n) for n in nozzles])}, which {who} out quickly."


def print_anyway_line(job_path: str | None) -> str | None:
    """:func:`print_anyway_said` for the sliced file at *job_path*, read from
    this computer's record of that file's slice (matched by the file itself,
    never by its name); ``None`` for a file the owner said nothing about or
    one this computer keeps no record of."""
    if not job_path:
        return None
    try:
        from kiln.print_lineage import resolve

        found = resolve(os.path.basename(job_path), local_paths=[job_path], keep_foreign=False)
    except Exception:  # noqa: BLE001 -- an unread record says nothing
        logger.debug("print-anyway record not read for %s", job_path, exc_info=True)
        return None
    if found.get("matched_by") not in ("stamp", "content"):
        return None
    return print_anyway_said(found.get("soft_nozzle"))


def asked_rows(
    *, soft_nozzle_confirmed: Any = False, mixed_nozzle_experiment: Any = False, material: Any = None,
) -> dict[str, str]:
    """The rows an approval shows for a print asked for before it is sliced,
    from the request's own words: ``print_anyway`` for the owner's word to
    print an abrasive filament through a nozzle that is not wear-resistant
    (naming the filament only when the request names one that is abrasive),
    and ``experimental`` for the yes to nozzles of different sizes.  Only for
    a word given as ``True`` itself; ``{}`` for neither."""
    rows: dict[str, str] = {}
    if soft_nozzle_confirmed is True:
        named = str(material or "").strip() if isinstance(material, str) else ""
        what = _SOME_ABRASIVE
        if named and _NAMED_FILAMENT.match(named):
            try:
                from kiln.material_safety import is_abrasive_filament

                if is_abrasive_filament(named):
                    what = named
            except Exception:  # noqa: BLE001 -- unread is unnamed
                logger.debug("filament not judged", exc_info=True)
        rows["print_anyway"] = f"{what} through a soft nozzle, which it wears out quickly."
    if mixed_nozzle_experiment is True:
        rows["experimental"] = (
            "This print uses nozzles of different sizes at once, which Kiln has not proven on a printer. "
            "Watch the first layers; Kiln will ask how it came out."
        )
    return rows


def asks_every_time(rows: Mapping[str, Any] | None) -> bool:
    """Whether the rows an approval shows (:func:`kiln.print_consent.what_the_print_is`)
    are those of a print that starts only on a yes to that print itself."""
    return isinstance(rows, Mapping) and any(str(rows.get(name) or "").strip() for name in ROW_WORDS)


class OwnYesHold(NamedTuple):
    """A start held for want of a yes to that print itself."""

    code: str
    message: str


def needs_its_own_yes(job_path: str | None, source: str | None) -> OwnYesHold | None:
    """The hold on a start of the file at *job_path* -- an experiment, or a
    print the owner said to print anyway -- when the yes on record (*source*)
    covers prints in general and not this one; ``None`` when the file is
    neither or the yes is for this print."""
    from kiln.print_consent import SOURCE_ALWAYS, SOURCE_HOSTED_DELEGATION, SOURCE_STANDING_OPT_IN, SOURCE_WINDOW

    if source not in (SOURCE_ALWAYS, SOURCE_HOSTED_DELEGATION, SOURCE_STANDING_OPT_IN, SOURCE_WINDOW):
        return None
    experiment = approval_line(job_path)
    anyway = print_anyway_line(job_path)
    if experiment is None and anyway is None:
        return None
    said = []
    if experiment:
        said.append(f"Experimental: {experiment}")
    if anyway:
        said.append(f"The owner said to print this anyway: {anyway}")
    if anyway is None:
        which = "An experimental print starts"
    elif experiment is None:
        which = "A print the owner said to print anyway starts"
    else:
        which = "An experimental print, or one the owner said to print anyway, starts"
    return OwnYesHold(
        NEEDS_A_YES_CODE if experiment else PRINT_ANYWAY_NEEDS_A_YES_CODE,
        f"{' '.join(said)} {which} only on a yes to that print itself, and the yes on record here is one "
        "that lets prints start without asking. Nothing was started: ask the person about this print, "
        "and start it again once they have said yes to it.",
    )


__all__ = [
    "KINDS",
    "NEEDS_A_YES_CODE",
    "NEEDS_A_YES_CODES",
    "PRINT_ANYWAY_NEEDS_A_YES_CODE",
    "ROW_WORDS",
    "OwnYesHold",
    "approval_line",
    "asked_rows",
    "asks_every_time",
    "mixed_sizes_of_file",
    "mixed_sizes_of_lines",
    "needs_its_own_yes",
    "print_anyway_line",
    "print_anyway_said",
    "read_recorded",
    "recorded_word",
]
