"""The nozzle size a check assumes, and where that figure came from.

A check that depends on the nozzle -- the thinnest wall that prints, the
shallowest relief that reads -- needs one size.  This is the one place that
picks it, so every check picks the same way and can say what it picked:

1. the size the caller stated;
2. the nozzle on record for the printer, when one was recorded
   (:func:`kiln._pro_nozzle_bridge.consult_recorded_nozzle`; the record is
   kiln-pro's, https://kiln3d.com);
3. the printer's own setting, when the machine reports one
   (:func:`kiln.printer_nozzle_reading.observe_printer_nozzle`);
4. the model's stock size, from its bundled slicer profile;
5. :data:`DEFAULT_MM`, said as a default.

Every answer names its rung.  A printer nobody named reaches rung 5 -- unless
the caller asks for the only printer Kiln knows of (*or_only_printer*): one
registered machine, or one nozzle on record for the account, is then the
printer, and several is no answer.  A rung that could not be asked is
skipped, and the answer says so when it was the record.  Sends nothing to
the printer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The size assumed when nothing says otherwise.
DEFAULT_MM = 0.4

#: How long a printer's own setting is remembered, read or not: a design
#: check is asked many times in a row, and a printer that is switched off
#: must not cost each of them the read's full deadline.
_SETTING_MEMO_S = 60.0
_setting_memo: dict[str, tuple[float, float | None]] = {}

_SOURCE_WORDS = {
    "stated": "the size it was given",
    "record": "the nozzle on record for {printer}",
    "printer_setting": "{printer}'s own nozzle setting",
    "stock": "the stock size for {printer}",
    "profile": "the slicer profile's own size",
    "default": "Kiln's default",
}


@dataclass(frozen=True)
class AssumedNozzle:
    """One answer.

    :param diameter_mm: The size the check runs with.
    :param source: ``"stated"``, ``"record"``, ``"printer_setting"``,
        ``"stock"``, ``"profile"`` (a slicer profile's own size, for a slice)
        or ``"default"``.
    :param printer_id: The printer the answer is about, when one was named.
    :param record_unreachable: The record could not be asked (offline,
        signed out), so a lower rung answered.
    :param inferred_printer: No printer was named and exactly one is known.
    """

    diameter_mm: float
    source: str
    printer_id: str | None = None
    record_unreachable: bool = False
    #: Nobody named the printer; it is the only one Kiln knows of.
    inferred_printer: bool = False

    def sentence(self, verb: str = "Checked") -> str:
        where = _SOURCE_WORDS.get(self.source, self.source).format(printer=self.printer_id or "the printer")
        if self.inferred_printer:
            where += ", the only printer Kiln knows of"
        said = f"{verb} for a {self.diameter_mm:g} mm nozzle: {where}."
        if self.source == "default":
            said += " Name the printer, or pass the nozzle size, if yours differs."
        if self.record_unreachable:
            said += " Kiln could not reach its record of your nozzle."
        return said

    def to_dict(self) -> dict[str, Any]:
        return {
            "diameter_mm": self.diameter_mm,
            "source": self.source,
            "printer_id": self.printer_id,
            "note": self.sentence(),
        }


def _valid(size: Any) -> float | None:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= 5 else None


def _printer_setting_mm(printer_id: str) -> float | None:
    """The printer's own setting when it can be read and is fresh, memoized
    for :data:`_SETTING_MEMO_S` whether or not it could be."""
    now = time.monotonic()
    memo = _setting_memo.get(printer_id)
    if memo is not None and now - memo[0] < _SETTING_MEMO_S:
        return memo[1]
    size: float | None = None
    try:
        from kiln.printer_nozzle_reading import observe_printer_nozzle

        observed = observe_printer_nozzle(printer_id)
        if observed is not None:
            age, budget = observed.get("state_age_seconds"), observed.get("stale_after_seconds")
            stale = isinstance(age, (int, float)) and isinstance(budget, (int, float)) and age > budget
            size = None if stale else _valid(observed.get("nozzle_diameter_mm"))
    except Exception:  # noqa: BLE001 -- a read that fails is a rung that did not answer
        logger.debug("assumed nozzle: printer read failed", exc_info=True)
    _setting_memo[printer_id] = (now, size)
    return size


def _key(name: str | None) -> str:
    return (name or "").lower().replace("-", "_").strip()


def _profile_id_of(printer_id: str) -> str | None:
    """The bundled slicer profile *printer_id* slices with -- by its own name
    or by its model -- or ``None``.  The "default" profile is never an
    answer: it is the fallback for a printer nobody identified."""
    try:
        from kiln.slicer_profiles import list_slicer_profiles

        known = set(list_slicer_profiles()) - {"default"}
        if _key(printer_id) in known:
            return _key(printer_id)
        model = _model_key_of(printer_id)
        return model if model in known else None
    except Exception:  # noqa: BLE001
        logger.debug("assumed nozzle: profile lookup failed", exc_info=True)
    return None


def _model_key_of(printer_name: str) -> str | None:
    """The model the saved printer *printer_name* is set up as, as a key, or
    ``None`` when it has none.  A catalogue model or one set up on this
    machine alike: it is what a slice for that printer is asked for by."""
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for

        return _key(resolve_printer_model_for(printer_name)) or None
    except Exception:  # noqa: BLE001
        logger.debug("assumed nozzle: model lookup failed", exc_info=True)
    return None


def stock_setting(printer_id: str, key: str) -> str | None:
    """The bundled slicer profile's *key* for the model *printer_id* slices as, or ``None``."""
    profile = _profile_id_of(printer_id)
    if profile is None:
        return None
    try:
        from kiln.slicer_profiles import get_slicer_profile

        raw = str(get_slicer_profile(profile).settings.get(key, "")).replace(";", ",").split(",")[0].strip()
        return raw or None
    except Exception:  # noqa: BLE001
        logger.debug("assumed nozzle: stock lookup failed", exc_info=True)
    return None


def _stock_mm(printer_id: str) -> float | None:
    """The model's stock size from its bundled slicer profile, or ``None``."""
    return _valid(stock_setting(printer_id, "nozzle_diameter"))


def _registered_machines() -> list[str]:
    """The printers registered here, one name per machine."""
    try:
        from kiln.registry import get_printer_registry

        return [str(name) for name in get_printer_registry().list_machines()]
    except Exception:  # noqa: BLE001 -- no registry is no machine
        return []


def assumed_nozzle(
    printer_id: str | None = None,
    *,
    stated: float | None = None,
    or_only_printer: bool = False,
) -> AssumedNozzle:
    """The nozzle size a check should run with for *printer_id*.

    *stated* is the size the caller passed, and wins.  With no printer named
    and *or_only_printer* set, the one printer Kiln knows of stands in: the
    one registered machine, else the one nozzle on record for the account.
    Never raises; with nothing to go on the answer is :data:`DEFAULT_MM`,
    named as a default.
    """
    said = _valid(stated)
    pid = (printer_id or "").strip() or None
    if said is not None:
        return AssumedNozzle(said, "stated", pid)
    inferred = False
    if pid is None and or_only_printer:
        machines = _registered_machines()
        if len(machines) == 1:
            pid, inferred = machines[0], True
        elif not machines:
            # No machine registered here (a hosted call, or a Kiln that was
            # never pointed at a printer): the account's one recorded nozzle
            # answers.  With several machines registered nothing does --
            # a record on one of them is not a reason to pick it.
            try:
                from kiln import _pro_nozzle_bridge

                only = _pro_nozzle_bridge.consult_only_recorded_nozzle()
                size = _valid(only.get("diameter_mm"))
                if only.get("printer_id") and size is not None:
                    return AssumedNozzle(size, "record", str(only["printer_id"]), inferred_printer=True)
                if not only.get("answered", True):
                    return AssumedNozzle(DEFAULT_MM, "default", record_unreachable=True)
            except Exception:  # noqa: BLE001 -- the record is one rung, never the check
                logger.debug("assumed nozzle: only-record lookup failed", exc_info=True)
    if pid is None:
        return AssumedNozzle(DEFAULT_MM, "default")

    record_unreachable = False
    try:
        from kiln import _pro_nozzle_bridge

        recorded = _pro_nozzle_bridge.consult_recorded_nozzle(pid)
        on_record = _valid(recorded.get("diameter_mm"))
        record_unreachable = not recorded.get("answered", True)
        if on_record is not None:
            return AssumedNozzle(on_record, "record", pid, inferred_printer=inferred)
    except Exception:  # noqa: BLE001 -- the record is one rung, never the check
        logger.debug("assumed nozzle: record lookup failed", exc_info=True)

    setting = _printer_setting_mm(pid)
    if setting is not None:
        return AssumedNozzle(setting, "printer_setting", pid, record_unreachable, inferred)
    stock = _stock_mm(pid)
    if stock is not None:
        return AssumedNozzle(stock, "stock", pid, record_unreachable, inferred)
    return AssumedNozzle(DEFAULT_MM, "default", pid, record_unreachable, inferred)


def nozzle_for_profile(profile_id: str, printer_name: str | None = None) -> AssumedNozzle:
    """The nozzle a slice for the model *profile_id* is for.

    A slicing door names a model, and not always a machine.  The machine is
    *printer_name* when the door has one; else the one registered machine
    set up as this model; else nobody, and the model's own name is asked (a
    nozzle recorded under it, else its stock size).  A named machine counts
    only when it is this model or its model is unknown.  Several machines
    of one model are never picked between.  The generic profile belongs to
    no model, so it is the only printer Kiln knows of, as for any check
    that names none.  Never raises.
    """
    named = (printer_name or "").strip()
    key = _key(profile_id)
    # A named machine set up as ANOTHER model is not the machine this slice
    # is for: the door asked for a different model on purpose.
    if named and (_key(named) == key or _model_key_of(named) in (None, key)):
        return assumed_nozzle(named)
    if not key or key == "default":
        return assumed_nozzle(None, or_only_printer=True)
    machines = _registered_machines()
    sharing = [name for name in machines if _key(name) == key or _model_key_of(name) == key]
    if len(sharing) == 1:
        answer = assumed_nozzle(sharing[0])
        return AssumedNozzle(
            answer.diameter_mm, answer.source, answer.printer_id, answer.record_unreachable,
            inferred_printer=len(machines) == 1,
        )
    return assumed_nozzle(key)


__all__ = ["DEFAULT_MM", "AssumedNozzle", "assumed_nozzle", "nozzle_for_profile", "stock_setting"]
