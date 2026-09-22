"""Say a nozzle's wear milestone once, when it is crossed -- never per print.

The pre-print capacity verdict (:mod:`kiln._pro_nozzle_bridge`) answers
before every print, and that is right: it is one cheap question with a
backoff, and asking every time is what makes a crossing detectable.  What
was wrong was REPEATING the answer: a nozzle that is approaching the end
of its life was announced on every start, which trains a person to stop
reading.  The pattern to follow is a phone's battery health -- a figure you
can look up any time (the pre-flight), one notice when a threshold is
crossed (here), and an interruption only when it affects the print in
front of you (the refusal at the budget, unchanged).

The milestones are the rungs the verdict already has -- ``approaching``,
``exceeded_p50`` (half of nozzles like it have worn out), ``exceeded_p90``
(the budget) -- never a percentage invented here, so this side can never
disagree with the words the verdict came with.  One notice per rung, per
nozzle: the record keeps the highest rung said for each printer, and a
notice fires only when the verdict climbs above it.

"Per nozzle" needs an identity.  A swap resets the ladder: the verdict
carries the nozzle's material and how many grams had gone through it
before this print, so a drop in grams or a change of material is a
different nozzle, and the next climb is announced again.  Nothing here
asks a server; the record is a small file under ``KILN_HOME``.

On the hosted multi-tenant server one ``KILN_HOME`` serves every customer,
and a record keyed by a caller-chosen printer name would be one tenant's
memory read as another's -- so there nothing is remembered, and every
crossing is said (today's behaviour).  A per-tenant memory belongs to the
server that keeps the nozzle record; that is the follow-up, not a reason
to share a file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The rungs, lowest first.  A verdict status outside this ladder (an
#: unknown baseline, an unknown nozzle, bad input) is not a rung and is
#: never remembered.
RUNGS: tuple[str, ...] = ("safe", "approaching", "exceeded_p50", "exceeded_p90")
#: The rungs a person hears about.
NOTICED: frozenset[str] = frozenset({"approaching", "exceeded_p50", "exceeded_p90"})

#: What each crossing means, in the person's words, and what to do.  The
#: verdict's own narrative rides between them.
_WORDS: dict[str, tuple[str, str]] = {
    "approaching": (
        "Your nozzle is approaching the end of its life on this kind of filament.",
        "Order a spare now, before it's urgent.",
    ),
    "exceeded_p50": (
        "Your nozzle has outlasted half of the nozzles like it on this kind of filament.",
        "Plan the swap: a fresh nozzle before the next long print on abrasive filament.",
    ),
    "exceeded_p90": (
        "Your nozzle is past the point almost every nozzle like it had worn out by.",
        "Replace it before the next print.",
    ),
}

_FILE = "nozzle_milestones.json"
_lock = threading.Lock()


def _rank(rung: str | None) -> int:
    return RUNGS.index(rung) if rung in RUNGS else -1


def _hosted() -> bool:
    """Whether this process is the shared multi-tenant deploy.

    Fails CLOSED: a predicate that cannot be read is treated as hosted, so
    the answer to "may this machine remember something keyed by a name the
    caller picked?" is no whenever it is unknown.  The cost of being wrong
    that way is noise -- a local install says every crossing instead of
    the first -- and the cost of being wrong the other way is one tenant's
    memory read as another's.
    """
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- unknown is hosted; see above
        return True


def _path() -> Path:
    home = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    return home / _FILE


def _read_all() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_all(data: dict[str, dict[str, Any]]) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("nozzle milestone record not written", exc_info=True)


def last_rung(printer_id: str) -> str | None:
    """The highest rung said for *printer_id*'s current nozzle, or ``None``."""
    if not isinstance(printer_id, str) or not printer_id.strip() or _hosted():
        return None
    rung = (_read_all().get(printer_id) or {}).get("rung")
    return rung if rung in RUNGS else None


def is_flagged(printer_id: str) -> bool:
    """True when this nozzle has already been named as approaching or worse --
    the one case where a start that could not check it should say so, since
    the print in hand might be the one that crosses the next rung."""
    return last_rung(printer_id) in NOTICED


def forget(printer_id: str) -> None:
    """Drop the record (a swap recorded by hand).  Never raises."""
    if not printer_id or _hosted():
        return
    with _lock:
        data = _read_all()
        if data.pop(printer_id, None) is not None:
            _write_all(data)


def notice_for(printer_id: str, verdict: dict[str, Any] | None) -> dict[str, Any] | None:
    """The notice a door carries for *verdict*, or ``None`` when there is
    nothing new to say.

    A notice fires the first time this nozzle's verdict climbs to a rung
    a person hears about; the same rung on the next print says nothing;
    a higher rung is a new notice; a swap (fewer grams through than last
    recorded, or another material) starts the ladder again.  On the hosted
    server nothing is remembered and every crossing is said.  Never
    raises: a notice never touches a print.
    """
    if not isinstance(printer_id, str) or not printer_id.strip() or not isinstance(verdict, dict):
        return None  # a memory is keyed by a real printer name, never by a stringified object
    rung = str(verdict.get("status") or "")
    if rung not in RUNGS:
        return None
    grams = _float(verdict.get("nozzle_grams_through_before"))
    material = str(verdict.get("nozzle_material") or "").strip().lower()
    say = rung in NOTICED
    if _hosted():
        return _notice(rung, verdict) if say else None
    try:
        with _lock:
            data = _read_all()
            record = data.get(printer_id) or {}
            same_nozzle = _same_nozzle(record, grams=grams, material=material)
            said = _rank(record.get("rung")) if same_nozzle else -1
            climbed = _rank(rung) > said
            if climbed or not same_nozzle or _rank(rung) < said:
                data[printer_id] = {
                    "rung": rung,
                    "grams": grams,
                    "material": material,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                _write_all(data)
    except Exception:  # noqa: BLE001 -- the record is a courtesy; the verdict stands without it
        logger.debug("nozzle milestone record failed", exc_info=True)
        return _notice(rung, verdict) if say else None
    return _notice(rung, verdict) if (say and climbed) else None


def _same_nozzle(record: dict[str, Any], *, grams: float | None, material: str) -> bool:
    if not record:
        return True
    old_material = str(record.get("material") or "")
    if material and old_material and material != old_material:
        return False
    old_grams = _float(record.get("grams"))
    # Fewer grams through than last time is a fresh nozzle.
    return not (grams is not None and old_grams is not None and grams + 0.5 < old_grams)


def _notice(rung: str, verdict: dict[str, Any]) -> dict[str, Any]:
    headline, next_step = _WORDS[rung]
    narrative = str(verdict.get("narrative") or "").strip()
    parts = [headline]
    if narrative:
        parts.append(narrative if narrative.endswith((".", "!", "?")) else f"{narrative}.")
    parts.append(next_step)
    out: dict[str, Any] = {
        "status": rung,
        "crossed": True,
        "line": " ".join(parts),
        "narrative": narrative,
        "percent_used": verdict.get("percent_used"),
    }
    if verdict.get("upgrade_hint"):
        out["upgrade_hint"] = verdict["upgrade_hint"]
    return out


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["NOTICED", "RUNGS", "forget", "is_flagged", "last_rung", "notice_for"]
