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
    "default": "Kiln's default",
}


@dataclass(frozen=True)
class AssumedNozzle:
    """One answer.

    :param diameter_mm: The size the check runs with.
    :param source: ``"stated"``, ``"record"``, ``"printer_setting"``,
        ``"stock"`` or ``"default"``.
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

    def sentence(self) -> str:
        where = _SOURCE_WORDS.get(self.source, self.source).format(printer=self.printer_id or "the printer")
        if self.inferred_printer:
            where += ", the only printer Kiln knows of"
        said = f"Checked for a {self.diameter_mm:g} mm nozzle: {where}."
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


def _stock_mm(printer_id: str) -> float | None:
    """The model's stock size from its bundled slicer profile, or ``None``.
    The profile's own "default" fallback is never taken for an answer: it
    would pass a guess off as this model's size."""
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for
        from kiln.slicer_profiles import get_slicer_profile, list_slicer_profiles

        known = set(list_slicer_profiles()) - {"default"}
        for key in (printer_id, resolve_printer_model_for(printer_id) or ""):
            key = key.lower().replace("-", "_").strip()
            if key in known:
                raw = str(get_slicer_profile(key).settings.get("nozzle_diameter", ""))
                return _valid(raw.replace(";", ",").split(",")[0])
    except Exception:  # noqa: BLE001
        logger.debug("assumed nozzle: stock lookup failed", exc_info=True)
    return None


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


__all__ = ["DEFAULT_MM", "AssumedNozzle", "assumed_nozzle"]
