"""Public-Kiln → kiln-pro bridge for one fact: does this printer MODEL
leave its maker with a camera of its own?

Kiln's catalogue knows the answer for the models it lists, as one of the
four words in :mod:`kiln.camera_words`.  The catalogue is served, never
shipped: this module asks for the word about ONE model -- the one a person
is turning always allow on for -- and keeps the answer on this computer,
so the next ask needs no network and an offline computer still knows what
it was told.

What the word is for.  Before a print starts with nobody asked, Kiln looks
at the bed through the printer's camera (:mod:`kiln.consent_windows`).
Whether a printer HAS a camera decides what a failed look means, and
printer software that can serve a camera says so whether or not one is
plugged in.  The word settles it for a known model without asking the
person something Kiln already knows.

Free on every tier, and a miss is never an error: with no answer --
offline, signed out, the service silent -- :func:`catalogue_word` returns
what this computer was last told, or ``None``, and the caller falls back
to what it can see and, last, to asking the person.  :func:`why_unanswered`
says which of the four served causes it was (:mod:`kiln.served_answer`),
so a signed-out person can be told that signing in would have answered.

With kiln-pro installed here the word is read locally and nothing is asked.
"""

from __future__ import annotations

import importlib
import logging
import time
from typing import Any

from kiln.camera_words import UNKNOWN, WORDS

logger = logging.getLogger(__name__)

#: The hosted door for the word, asked when kiln-pro is not installed here.
WIRE_TOOL = "camera_fitment"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.device_intelligence.camera_fitment"
#: Turning always allow on is a person at a terminal; they wait this long.
_CONSULT_TIMEOUT_S: float = 4.0
#: After the service fails to answer, how long it is left alone.  The same
#: backoff the nozzle and blade bridges use.
SERVICE_BACKOFF_S: float = 300.0
_service_down_until: float = 0.0
_service_down_miss: Any = None
#: Why the last ask for each model had no answer (a
#: :class:`kiln.served_answer.Miss`), cleared by an answer.
_last_miss: dict[str, Any] = {}

#: How long a word kept on this computer is used without asking again.  A
#: settled word changes only when the catalogue is corrected; ``unknown``
#: is the one a correction is most likely to replace, so it is asked about
#: again sooner.
FRESH_FOR_S: float = 7 * 24 * 3600.0
UNKNOWN_FRESH_FOR_S: float = 24 * 3600.0

__all__ = [
    "FRESH_FOR_S",
    "LOCAL_MODULE",
    "UNKNOWN_FRESH_FOR_S",
    "WIRE_TOOL",
    "catalogue_word",
    "kept_word",
    "why_unanswered",
]


def _model_key(model: object) -> str:
    """The model as it is sent and kept: the catalogue's own id for what
    the person declared, found the way every other door finds it (the id
    itself with a vendor prefix tolerated, then the shared hint table), so
    ``creality_k1`` asks about ``k1``.  A spelling the catalogue does not
    list is sent as written, lowercased -- the answer to that is
    ``unknown``, which is true.  ``""`` for no model."""
    raw = str(model or "").strip().lower() if isinstance(model, str) else ""
    if not raw:
        return ""
    try:
        from kiln.printers.bed_fit import _load_printer_intelligence, _printer_id_candidates

        catalogue = _load_printer_intelligence() or {}
        for candidate in _printer_id_candidates(raw):
            if candidate in catalogue and not candidate.startswith("_"):
                return candidate
        from kiln.printer_profile_ids import map_printer_hint_to_profile_id

        mapped = map_printer_hint_to_profile_id(raw)
        if mapped and mapped in catalogue:
            return mapped
    except Exception:  # noqa: BLE001 -- an unreadable catalogue resolves nothing
        logger.debug("declared model not resolved to a catalogue id", exc_info=True)
    return raw


def kept_word(model: object) -> str | None:
    """The word this computer was last told for *model*, however old, or
    ``None``.  Never asks anything; what a print start reads."""
    key = _model_key(model)
    if not key:
        return None
    try:
        from kiln import plate_state

        kept = plate_state.catalogue_word_on_record(key)
    except Exception:  # noqa: BLE001 -- an unreadable record knows nothing
        return None
    return kept[0] if kept else None


def catalogue_word(model: object) -> str | None:
    """The catalogue's word for *model*: from kiln-pro when it is installed
    here; else the word kept on this computer while it is fresh; else the
    served answer, which is then kept; else the kept word however old.
    ``None`` when nothing has ever answered (and :func:`why_unanswered`
    says why).  Never raises."""
    key = _model_key(model)
    if not key:
        return None
    try:
        camera_word = importlib.import_module(LOCAL_MODULE).camera_word
    except (ImportError, AttributeError):
        pass
    else:
        _last_miss.pop(key, None)
        try:
            word = camera_word(key)
        except Exception:  # noqa: BLE001 -- a local read that fails knows nothing
            logger.debug("local camera word unreadable", exc_info=True)
            return kept_word(key)
        return word if word in WORDS else UNKNOWN
    kept = None
    try:
        from kiln import plate_state

        kept = plate_state.catalogue_word_on_record(key)
    except Exception:  # noqa: BLE001
        kept = None
    if kept is not None:
        word, age_s = kept
        if age_s <= (UNKNOWN_FRESH_FOR_S if word == UNKNOWN else FRESH_FOR_S):
            _last_miss.pop(key, None)
            return word
    served = _served_word(key)
    if served is not None:
        try:
            from kiln import plate_state

            plate_state.keep_catalogue_word(key, served)
        except Exception:  # noqa: BLE001 -- not kept costs one more ask later
            logger.debug("camera word not kept", exc_info=True)
        return served
    return kept[0] if kept is not None else None


def _served_word(key: str) -> str | None:
    """The hosted word, or ``None`` with why in :data:`_last_miss`."""
    global _service_down_until, _service_down_miss
    from kiln.served_answer import Miss, classify_answer, classify_transport_error

    if time.monotonic() < _service_down_until:
        if _service_down_miss is not None:
            _last_miss[key] = _service_down_miss
        return None
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        _last_miss[key] = Miss("unanswered", detail="the served door could not be opened on this install")
        return None
    try:
        answer = _pro_api_call(WIRE_TOOL, _timeout=_CONSULT_TIMEOUT_S, printer_id=key)
    except Exception as exc:  # noqa: BLE001 -- the network is a degrade, never a refusal to go on
        logger.debug("camera word not served", exc_info=True)
        miss = classify_transport_error(exc)
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
        _last_miss[key] = miss
        return None
    word = answer.get("word") if isinstance(answer, dict) and answer.get("success") else None
    if word in WORDS:
        _last_miss.pop(key, None)
        return str(word)
    miss = classify_answer(answer) or Miss("unanswered", detail="an answer with no word in it")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until = time.monotonic() + SERVICE_BACKOFF_S
        _service_down_miss = miss
    _last_miss[key] = miss
    return None


def why_unanswered(model: object) -> str:
    """Which served cause left the last ask for *model* without an answer:
    ``offline`` / ``signed_out`` / ``unanswered`` / ``refused``, or ``""``
    when it was answered (or never asked)."""
    miss = _last_miss.get(_model_key(model))
    return str(getattr(miss, "cause", "") or "") if miss is not None else ""


def _reset_for_tests() -> None:
    global _service_down_until, _service_down_miss
    _service_down_until = 0.0
    _service_down_miss = None
    _last_miss.clear()
