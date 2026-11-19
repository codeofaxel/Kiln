"""What Kiln tells a person whose printer it slices for more than one nozzle
while that printer's maker's slicer is missing or too old on this computer.

Such a print is never refused for it: it is sliced for one nozzle, with
filament swaps (:mod:`kiln.maker_slicer`).  This module owns the words and
how often they are said, so every door says the same thing:

* **The slice** (:func:`after_slice`): every such slice says it was sliced
  for one nozzle and how many filament swaps the sliced file makes, read
  from the file.  The line naming the slicer to install or update is said
  the first time on a printer, then only on a print of
  :data:`SWAPS_WORTH_SAYING` swaps or more.  The first slice for more than
  one nozzle after a slice for one says "Using both nozzles now." once.
* **Setting up** (:func:`setup_line`): when a printer is added, or its owner
  records a second nozzle as fitted, one line, once per printer.
* **The checkup** (:func:`doctor_row`): a status row, every run.

Kiln never installs a slicer.  It names the maker's page and, on a Mac with
Homebrew, the command (:func:`kiln.maker_slicer.install_words`); an agent may
offer that command and runs it only on the person's yes.  The slicer is free,
so nothing here names a plan.

What was said is kept per printer model in ``~/.kiln/maker_slicer_notes.json``
on the person's own computer.  On Kiln's shared server nothing is read or
written, and the slicer line is never said: a server's slicer is not the
person's to install.  Never raises into a slice: a note never costs one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A print with this many filament swaps or more names the slicer that would
#: use every nozzle again, however often it was said before.
SWAPS_WORTH_SAYING = 20

#: Said once, on the first slice for more than one nozzle after a slice for one.
BOTH_NOW = "Using both nozzles now."

_lock = threading.Lock()


def store_path() -> Path:
    # Resolved at call time so a moved HOME (tests, sandboxes) is honoured.
    return Path.home() / ".kiln" / "maker_slicer_notes.json"


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- an unknown posture keeps no record
        return True


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _save(data: Mapping[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _note(printer: str, **changes: Any) -> dict[str, Any]:
    """The kept entry for *printer* before *changes*, with *changes* kept.
    Never raises; nothing is kept on the shared server."""
    if _hosted():
        return {}
    try:
        with _lock:
            data = _load()
            before = dict(data.get(printer) or {})
            if changes:
                data[printer] = {**before, **changes}
                _save(data)
            return before
    except Exception:  # noqa: BLE001 -- a note never costs what it rides on
        logger.debug("maker slicer note not kept for %s", printer, exc_info=True)
        return {}


def _nozzles_words(count: int) -> tuple[str, str, str]:
    """``("two nozzles", "both nozzles", "both")`` for *count* nozzles."""
    if count == 2:
        return "two nozzles", "both nozzles", "both"
    return f"{count} nozzles", f"all {count} nozzles", f"all {count}"


def _get_it(miss: Any) -> tuple[str, dict[str, str]]:
    """Where to get the slicer *miss* names, as words and as the fields an
    answer carries."""
    from kiln.maker_slicer import install_words

    words = install_words(miss)
    said = words["download"]
    if words.get("command"):
        said += f", or on this Mac: {words['command']}"
    return said, words


def _what_to_do(miss: Any) -> str:
    """``Install Bambu Studio (free)`` / ``Update PrusaSlicer to 2.9.4 or newer (free)``."""
    from kiln.maker_slicer import UPDATE

    if miss.need == UPDATE:
        floor = f" to {miss.floor} or newer" if miss.floor else ""
        return f"Update {miss.said}{floor} (free)"
    return f"Install {miss.said} (free)"


def _swaps_sentence(swaps: int | None) -> str:
    if swaps is None:
        return "Sliced for one nozzle."
    if swaps == 0:
        return "Sliced for one nozzle, with no filament swaps."
    return f"Sliced for one nozzle: {swaps} filament swap{'s' if swaps != 1 else ''}."


def _swaps_in(path: str | None) -> int | None:
    """The filament swaps the sliced file at *path* makes, or ``None`` when
    it cannot be read."""
    if not path:
        return None
    from kiln.gcode import tool_changes

    try:
        with open(path, errors="replace", encoding="utf-8") as fh:
            return tool_changes(fh)[0]
    except OSError:
        return None


def after_slice(result: Any, *, printer: str | None, miss: Any = None, nozzles: int = 2) -> Any:
    """*result* saying what a slice of a printer sliced for more than one
    nozzle was made for.

    With *miss* (:class:`kiln.maker_slicer.Miss`) the slice was made for one
    nozzle of the *nozzles* the printer's record would use: its message
    gains the filament swaps the sliced file makes and, when due, the line
    naming the slicer that would use them all; ``one_nozzle`` holds the
    same.  Without it, a slice for more than one nozzle (``sliced_with``)
    after a slice for one says :data:`BOTH_NOW`, once.  *printer* is the
    printer's id; ``None`` keeps nothing and says the slicer line every time.
    Never raises.
    """
    try:
        if miss is not None:
            return _said_one_nozzle(result, printer, miss, nozzles)
        several = getattr(result, "sliced_with", None) is not None
        if printer is not None and several and _note(printer).get("one_nozzle"):
            _note(printer, one_nozzle=False)
            result.message = f"{result.message} {BOTH_NOW}".strip()
    except Exception:  # noqa: BLE001 -- a note never costs a slice
        logger.debug("maker slicer note not said", exc_info=True)
    return result


def _said_one_nozzle(result: Any, printer: str | None, miss: Any, nozzles: int) -> Any:
    swaps = _swaps_in(getattr(result, "output_path", None))
    before = _note(printer, one_nozzle=True) if printer is not None else {}
    due = not before.get("told") or (swaps is not None and swaps >= SWAPS_WORTH_SAYING)
    words = [_swaps_sentence(swaps)]
    block: dict[str, Any] = {"filament_swaps": swaps}
    if due and not _hosted():
        where, fields = _get_it(miss)
        both = _nozzles_words(nozzles)[1]
        words.append(f"{_what_to_do(miss)} and Kiln uses {both} on its own: {where}.")
        block["maker_slicer"] = {"name": miss.said, "need": miss.need, **fields}
        if printer is not None:
            _note(printer, told=time.strftime("%Y-%m-%d"))
    result.one_nozzle = block
    result.message = f"{result.message} {' '.join(words)}".strip()
    return result


def _record_for(printer_model: str | None, positions: Sequence[Mapping[str, Any]] | None) -> tuple[str, str, Mapping[str, Any], int] | None:
    """``(id, name, record, nozzles)`` for a printer whose served record
    slices it for more than one nozzle, with the owner's fitted *positions*
    on it when given; ``None`` for any other printer, or when no record is
    served (signed out, offline)."""
    if not printer_model:
        return None
    from kiln.printer_intelligence import catalogue_block
    from kiln.slicer_profiles import fitted_record, nozzle_positions

    found = catalogue_block(str(printer_model), "slicer")
    if found is None:
        return None
    printer_id, block = found
    record = block.get("nozzles")
    if not isinstance(record, Mapping):
        return None
    if positions:
        record = fitted_record(dict(record), list(positions)) or record
    count = nozzle_positions(record)
    if count < 2:
        return None
    return printer_id, str(block.get("display_name") or printer_id), record, count


def setup_line(printer_model: str | None, *, positions: Sequence[Mapping[str, Any]] | None = None) -> str:
    """One line for a printer just added, or whose owner just recorded a
    second nozzle as fitted (*positions*): the printer has more than one
    nozzle and Kiln uses them all once its maker's slicer is installed (or
    updated).  ``""`` when the slicer is ready, the printer is sliced for one
    nozzle, no record is served, on Kiln's shared server, or the line was
    said for this printer before.  The one helper every such door calls.
    Never raises.
    """
    try:
        if _hosted():
            return ""
        found = _record_for(printer_model, positions)
        if found is None:
            return ""
        printer_id, name, record, count = found
        from kiln.maker_slicer import UPDATE, ready_for

        miss = ready_for(record)
        if miss is None or _note(printer_id).get("suggested"):
            return ""
        _note(printer_id, suggested=time.strftime("%Y-%m-%d"))
        has, _all, uses = _nozzles_words(count)
        when = (
            f"is updated{f' to {miss.floor} or newer' if miss.floor else ''}"
            if miss.need == UPDATE else "is installed"
        )
        where, _fields = _get_it(miss)
        return (
            f"Your {name} has {has}. Kiln uses {uses} once {miss.said} {when} (free). "
            f"Until then, multi-colour prints use one nozzle with filament swaps. Get it from {where}."
        )
    except Exception:  # noqa: BLE001 -- a printer is added whether or not this is said
        logger.debug("maker slicer setup line not said", exc_info=True)
        return ""


def after_nozzle_record(answer: Any, printer_id: Any) -> Any:
    """*answer*, the reply to a nozzle record just written for the saved
    printer *printer_id*, carrying :func:`setup_line` as ``two_nozzles``
    when the record now holds two or more fitted nozzles.  Never raises."""
    try:
        if not isinstance(answer, dict) or answer.get("success") is not True:
            return answer
        from kiln._pro_nozzle_bridge import recorded_positions_of

        positions = recorded_positions_of(answer.get("positions")) or []
        if sum(1 for p in positions if p["fitted"]) < 2:
            return answer
        from kiln.printer_model_resolver import resolve_printer_model_for

        line = setup_line(resolve_printer_model_for(str(printer_id or "")), positions=positions)
        if line:
            answer["two_nozzles"] = line
    except Exception:  # noqa: BLE001 -- the record was written whether or not this is said
        logger.debug("maker slicer line after a nozzle record skipped", exc_info=True)
    return answer


def doctor_row(printer_model: str | None, *, positions: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    """The checkup's row for a printer sliced for more than one nozzle:
    ``ready``, or what to install or update to use every nozzle; ``None``
    for any other printer, or when no record is served.  Said every run.
    Never raises."""
    try:
        found = _record_for(printer_model, positions)
        if found is None:
            return None
        _printer_id, _name, record, count = found
        from kiln.maker_slicer import UPDATE, ready_for

        label = "two_nozzles" if count == 2 else f"{count}_nozzles"
        miss = ready_for(record)
        if miss is None:
            return {"name": label, "ok": True, "detail": "ready"}
        where, fields = _get_it(miss)
        verb = "update" if miss.need == UPDATE else "install"
        floor = f" to {miss.floor} or newer" if miss.need == UPDATE and miss.floor else ""
        use = _nozzles_words(count)[2]
        row: dict[str, Any] = {
            "name": label,
            "ok": True,
            "warn": True,
            "detail": f"{verb} {miss.said}{floor} to use {use}: {where}",
        }
        if fields.get("command"):
            row["command"] = fields["command"]
        return row
    except Exception:  # noqa: BLE001 -- a checkup row is never worth failing the checkup
        logger.debug("maker slicer doctor row skipped", exc_info=True)
        return None
