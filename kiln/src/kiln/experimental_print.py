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
  sliced says it from the request's own words (:func:`asked_facts`); each
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


# ---------------------------------------------------------------------------
# The facts each row is said from, and the one wording of each
# ---------------------------------------------------------------------------
#
# A row an approval shows is never taken as text from whoever asks: it is
# said here, from facts that are checked first.  Every door says it from the
# same facts in the same words: this computer's approval, its terminal, and
# the account's card (whose server says it with these functions).

#: A filament type name a fact may carry: one short name, nothing else.
_NAMED_FILAMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +._-]{0,39}$")
#: A nozzle material a fact may carry, in the nozzle record's own words.
_NOZZLE_WORD = re.compile(r"^[a-z][a-z_]{0,23}$")
#: The most filaments, nozzles or sizes one fact may list, and the highest
#: nozzle position it may name.
_MOST_LISTED = 8
_HIGHEST_POSITION = 16
#: The nozzle sizes a fact may state: thousandths of a millimetre, within
#: these bounds.
_SIZE_RANGE_MM = (0.05, 3.0)
#: The filament an approval names when the facts name none.
_SOME_ABRASIVE = "Abrasive filament"
#: The nozzle an approval names when the facts name none.
_SOME_SOFT_NOZZLE = "a soft nozzle"


def _joined(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _nozzle_word(material: Any) -> str:
    return str(material or "").strip().lower().replace("-", "_").replace(" ", "_")


def _nozzle_said(nozzle: Mapping[str, Any]) -> str:
    made = str(nozzle.get("material") or "").replace("_", " ").strip()
    position = nozzle.get("position")
    if isinstance(position, int) and not isinstance(position, bool):
        return f"nozzle {position} ({made})" if made else f"nozzle {position}"
    return f"the {made} nozzle" if made else "the nozzle on record"


def checked_print_anyway(facts: Any) -> dict[str, Any] | None:
    """*facts* for the print-anyway row, checked: ``{"filaments": [type
    names], "nozzles": [{"position", "material"}]}``, each filament an
    abrasive one, each nozzle's material one that is not wear-resistant and
    its position a whole number or ``None``; either list may be empty when
    it is not known yet.  ``None`` for anything else."""
    from kiln.material_safety import is_abrasive_filament, is_wear_resistant_nozzle

    if not isinstance(facts, Mapping) or set(facts) - {"filaments", "nozzles"}:
        return None
    filaments, nozzles = facts.get("filaments", []), facts.get("nozzles", [])
    if not isinstance(filaments, list) or not isinstance(nozzles, list):
        return None
    if len(filaments) > _MOST_LISTED or len(nozzles) > _MOST_LISTED:
        return None
    for name in filaments:
        if not isinstance(name, str) or not _NAMED_FILAMENT.match(name) or not is_abrasive_filament(name):
            return None
    kept_nozzles = []
    for nozzle in nozzles:
        if not isinstance(nozzle, Mapping) or set(nozzle) - {"position", "material"}:
            return None
        position, material = nozzle.get("position"), nozzle.get("material")
        if position is not None and (
            not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= _HIGHEST_POSITION
        ):
            return None
        if not isinstance(material, str) or not _NOZZLE_WORD.match(material) or is_wear_resistant_nozzle(material):
            return None
        kept_nozzles.append({"position": position, "material": material})
    return {"filaments": sorted(set(filaments)), "nozzles": kept_nozzles}


def print_anyway_row(facts: Any) -> str | None:
    """The one wording of the print-anyway row, from *facts*
    (:func:`checked_print_anyway`): ``PLA-CF through the brass nozzle, which
    it wears out quickly.``, or ``Abrasive filament through a soft nozzle,
    which it wears out quickly.`` when neither is known yet.  ``None`` for
    facts that do not check."""
    checked = checked_print_anyway(facts)
    if checked is None:
        return None
    named, nozzles = checked["filaments"], checked["nozzles"]
    what = _joined(named) if named else _SOME_ABRASIVE
    where = _joined([_nozzle_said(n) for n in nozzles]) if nozzles else _SOME_SOFT_NOZZLE
    who = "they wear" if len(named) > 1 else "it wears"
    return f"{what} through {where}, which {who} out quickly."


#: The facts every row falls back to when the facts it has do not check:
#: the word still holds the print, and is said in its plain form.
_PLAIN_FACTS: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "soft_nozzle_confirmed": MappingProxyType({"filaments": (), "nozzles": ()}),
    "mixed_nozzle_experiment": MappingProxyType({"sizes": ()}),
})


def _plain(word: str) -> dict[str, Any]:
    return {key: list(value) for key, value in _PLAIN_FACTS[word].items()}


def print_anyway_facts(kept: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The print-anyway facts of a slice the owner said to print anyway,
    from what its record keeps (:func:`kiln.print_lineage.soft_nozzle_of`)
    when they check (:func:`checked_print_anyway`), else the plain facts.  ``None`` only when there is no record: the owner's word on
    record is never lost to facts that do not check."""
    if not isinstance(kept, Mapping):
        return None
    filaments = sorted({
        str(f.get("material") or "").strip()
        for f in kept.get("filaments") or () if isinstance(f, Mapping)
    } - {""})
    nozzles = [
        {"position": n.get("position"), "material": _nozzle_word(n.get("material"))}
        for n in kept.get("nozzles") or () if isinstance(n, Mapping)
    ]
    return checked_print_anyway({"filaments": filaments, "nozzles": nozzles}) or _plain("soft_nozzle_confirmed")


def _print_anyway_facts_of_file(job_path: str | None) -> dict[str, Any] | None:
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
    return print_anyway_facts(found.get("soft_nozzle"))


def print_anyway_line(job_path: str | None) -> str | None:
    """:func:`print_anyway_row` for the sliced file at *job_path*, read from
    this computer's record of that file's slice (matched by the file itself,
    never by its name); ``None`` for a file the owner said nothing about or
    one this computer keeps no record of."""
    facts = _print_anyway_facts_of_file(job_path)
    return print_anyway_row(facts) if facts is not None else None


def checked_experiment(facts: Any) -> dict[str, Any] | None:
    """*facts* for the experimental row, checked: ``{"sizes": [mm, ...]}``,
    two or more nozzle sizes of which at least two differ, each in
    thousandths of a millimetre within the sizes a nozzle comes in, or an
    empty list when they are not known yet.  ``None`` for anything else."""
    if not isinstance(facts, Mapping) or set(facts) - {"sizes"}:
        return None
    sizes = facts.get("sizes", [])
    if not isinstance(sizes, list) or len(sizes) == 1 or len(sizes) > _MOST_LISTED:
        return None
    low, high = _SIZE_RANGE_MM
    kept: list[float] = []
    for size in sizes:
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not low <= size <= high:
            return None
        if abs(size * 1000 - round(size * 1000)) > 1e-6:
            return None
        kept.append(round(float(size), 3))
    if kept and max(kept) - min(kept) <= _SAME_SIZE_MM:
        return None
    return {"sizes": kept}


def experimental_row(facts: Any) -> str | None:
    """The one wording of the experimental row, from *facts*
    (:func:`checked_experiment`), naming the sizes when they are known;
    ``None`` for facts that do not check."""
    checked = checked_experiment(facts)
    if checked is None:
        return None
    sizes = tuple(checked["sizes"])
    said = f" ({_sizes_said(sizes)})" if sizes else ""
    return (
        f"This print uses nozzles of different sizes at once{said}, which Kiln has not proven on a "
        "printer. Watch the first layers; Kiln will ask how it came out."
    )


def experiment_facts(job_path: str | None) -> dict[str, Any] | None:
    """The experimental facts of the sliced file at *job_path*: its sizes
    when they check (:func:`checked_experiment`), else the plain facts.
    ``None`` only for a file that states no sizes that differ: a file sliced
    for nozzles of different sizes is always an experiment."""
    sizes = mixed_sizes_of_file(job_path)
    if not sizes:
        return None
    return checked_experiment({"sizes": list(sizes)}) or _plain("mixed_nozzle_experiment")


def approval_line(job_path: str | None) -> str | None:
    """What a person is told, beside the word "experimental", before an
    experimental print starts; ``None`` for any other file."""
    facts = experiment_facts(job_path)
    return experimental_row(facts) if facts is not None else None


#: Each owner's word with the row it is said in and that row's wording.
_ROW_OF_WORD = {
    "soft_nozzle_confirmed": ("print_anyway", print_anyway_row),
    "mixed_nozzle_experiment": ("experimental", experimental_row),
}


def word_facts(word: Any, facts: Any) -> dict[str, Any] | None:
    """*facts* for the owner's word *word*, checked, or its plain facts when
    they do not check; ``None`` only for a word that is not an owner's word."""
    checker = {"soft_nozzle_confirmed": checked_print_anyway, "mixed_nozzle_experiment": checked_experiment}
    found = checker.get(word) if isinstance(word, str) else None
    if found is None:
        return None
    return found(facts) or _plain(word)


def word_row(word: Any, facts: Any) -> str | None:
    """The row for the owner's word *word*, said from *facts* in its one
    wording, or in its plain form when they do not check; ``None`` only for
    a word that is not an owner's word."""
    found = _ROW_OF_WORD.get(word) if isinstance(word, str) else None
    if found is None:
        return None
    return found[1](facts) or found[1](_plain(word))


def word_rows(owner_words: Any) -> dict[str, str]:
    """The rows for *owner_words* (``{word: facts}``), ``{row: text}``, each
    said by :func:`word_row`: an owner's word always has its row, in its
    plain form when its facts do not check.  A word that is not an owner's
    word has none."""
    if not isinstance(owner_words, Mapping):
        return {}
    rows: dict[str, str] = {}
    for word, facts in owner_words.items():
        said = word_row(word, facts)
        if said is not None:
            rows[_ROW_OF_WORD[word][0]] = said
    return rows


def file_owner_words(job_path: str | None) -> dict[str, dict[str, Any]]:
    """``{word: facts}`` for the owner's words the sliced file at *job_path*
    stands for: the word to print it anyway, when its slice record keeps
    one, and the yes to nozzles of different sizes, when it was sliced for
    them.  ``{}`` for any other file."""
    words: dict[str, dict[str, Any]] = {}
    anyway = _print_anyway_facts_of_file(job_path)
    if anyway is not None:
        words["soft_nozzle_confirmed"] = anyway
    experiment = experiment_facts(job_path)
    if experiment is not None:
        words["mixed_nozzle_experiment"] = experiment
    return words


def asked_facts(
    *, soft_nozzle_confirmed: Any = False, mixed_nozzle_experiment: Any = False, material: Any = None,
) -> dict[str, dict[str, Any]]:
    """``{word: facts}`` for a print asked for before it is sliced, from the
    request's own words: the print-anyway facts name the filament only when
    the request names one that is abrasive, and no nozzle; the experiment's
    name no sizes.  Only for a word given as ``True`` itself."""
    words: dict[str, dict[str, Any]] = {}
    if soft_nozzle_confirmed is True:
        named = material.strip() if isinstance(material, str) else ""
        facts = {"filaments": [named], "nozzles": []}
        words["soft_nozzle_confirmed"] = (
            facts if checked_print_anyway(facts) is not None else {"filaments": [], "nozzles": []}
        )
    if mixed_nozzle_experiment is True:
        words["mixed_nozzle_experiment"] = {"sizes": []}
    return words


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
    "asked_facts",
    "asks_every_time",
    "checked_experiment",
    "checked_print_anyway",
    "experiment_facts",
    "experimental_row",
    "file_owner_words",
    "print_anyway_facts",
    "print_anyway_row",
    "word_facts",
    "word_row",
    "word_rows",
    "mixed_sizes_of_file",
    "mixed_sizes_of_lines",
    "needs_its_own_yes",
    "print_anyway_line",
    "read_recorded",
    "recorded_word",
]
