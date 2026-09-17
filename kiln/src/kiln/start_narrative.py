"""What a Bambu print start and a cancel LOOK like, said on every door.

Why this exists.  2026-09-16, a Bambu A1: three times in one night the head
made a move that looked like a crash, and the person at the machine cut the
power.  Every one of them was the vendor's own start sequence doing what it
always does -- the filament cutter firing (a hard run into a stop and a
clack), a hot flush pushed off the plate edge into the purge chute, a Z home
by touch on the bare steel behind the plate -- and nothing Kiln had said
warned them.  A print killed over the printer's own routine is Kiln's
failure to speak, so every door that starts or cancels a print now says what
the next minutes look like BEFORE they happen.

What lives here is the MECHANISM, and only that: one field,
``what_you_will_see``, rendered by one helper and attached by every start
door (through :func:`kiln.print_start_verdict.resolve_print_start`, which
every start door already calls) and by the cancel engine; plus the one-line
early-stage reading the monitor report carries while the start sequence is
still running.  The per-model, stage-by-stage account -- which move comes
when on which machine, read from the vendor's own start file -- is curated
knowledge and lives in kiln-pro (https://kiln3d.com), served free to any
signed-in user.  Without it, every Bambu gets the one honest generic line
below, which is enough to stop a power cut: it names the three moves that
read as crashes and says they are not.

Non-Bambu printers get nothing from this module, on purpose.  The lines are
about Bambu's start block and nothing else; a Klipper or Prusa owner shown
"a slam into a hard stop is normal" would be told something false.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BAMBU_BACKEND",
    "GENERIC_BAMBU_CANCEL",
    "GENERIC_BAMBU_START",
    "GENERIC_BAMBU_START_STAGE_LINE",
    "cancel_narrative",
    "start_narrative",
    "start_stage_line",
]

#: ``PrinterAdapter.name`` of the one backend these lines are about.
BAMBU_BACKEND = "bambu"

#: The one line every Bambu gets when the per-model reading is not in reach.
#: The three moves named are the three that were mistaken for crashes; each
#: is in the vendor's own start file for the machine it happened on.
GENERIC_BAMBU_START = (
    "Bambu's own start sequence runs first and makes several moves that look "
    "wrong and are not: a slam into a hard stop with a clack (the filament "
    "cutter), a hot flush pushed off the plate edge into the purge chute, and "
    "a Z probe by touch on the bare steel behind the plate. Let it run. "
    "Kiln's stage-by-stage reading for this model is free with a Kiln sign-in."
)

#: The cancel line.  Kept generic on purpose -- it names no side of the
#: machine -- because where the cutter sits differs by family; the located
#: version rides with the per-model reading.
GENERIC_BAMBU_CANCEL = (
    "Bambu's own cancel routine runs first: expect a hard move and a clack "
    "(the head cutting the filament), then the head lifts and parks away from "
    "the part. It will not return over the print. Let it finish before "
    "touching anything."
)

#: The monitor report's one line while the start sequence is still running
#: and the per-model reading is not in reach.
GENERIC_BAMBU_START_STAGE_LINE = (
    "Still in Bambu's own start sequence (layer 0): a slam into a hard stop, "
    "a flush off the plate edge or a probe behind the plate here is the "
    "vendor's routine, not a crash. Kiln's stage-by-stage reading for this "
    "model is free with a Kiln sign-in."
)


def _is_bambu(adapter: Any) -> bool:
    """Whether *adapter* drives a Bambu -- the backend family, never the model."""
    try:
        return str(getattr(adapter, "name", "") or "").strip().lower() == BAMBU_BACKEND
    except Exception:  # noqa: BLE001 -- an adapter that refuses its name is not a Bambu we can speak for
        return False


def _declared_model(adapter: Any) -> str:
    """The model the OWNER declared for this adapter, lower-cased, or ``""``.

    The Bambu adapter keeps the config-declared ``printer_model`` on itself
    -- the same value the 3MF wrapper keys its start sequence on -- so the
    reading and the file the printer is actually running agree by
    construction.  Never inferred from a serial or a firmware string: a
    guessed model names the wrong start file confidently.
    """
    try:
        model = getattr(adapter, "_printer_model", None) or getattr(adapter, "_safety_profile_id", None)
        return str(model or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _pro_block(model: str) -> dict[str, Any] | None:
    """kiln-pro's per-model reading for *model*, or ``None``.

    Interface contract only: public Kiln knows that kiln-pro can say, stage
    by stage, what this model's start sequence looks like and what its cancel
    does; the reading and its shape are kiln-pro's.  ``None`` without
    kiln-pro, for a model it has no row for, and on any failure -- a start
    must never fail over a courtesy line.
    """
    if not model:
        return None
    try:
        from kiln_pro.bridge import pro_features
    except ImportError:
        return None
    try:
        if not pro_features.is_available("device_intelligence"):
            return None
        block = pro_features.device_intelligence.start_stages_block(model)
    except Exception as exc:  # noqa: BLE001 -- a missing courtesy line is not a failure
        logger.debug("start-stage reading unavailable for %r: %s", model, exc)
        return None
    if not isinstance(block, dict) or not block.get("known"):
        return None
    return block


def start_narrative(adapter: Any) -> list[str] | None:
    """The ``what_you_will_see`` list for a start on *adapter*, or ``None``.

    ``None`` for every non-Bambu adapter, so the field is absent rather than
    empty on a printer these lines do not describe.  For a Bambu it is the
    per-model, stage-by-stage reading when kiln-pro can supply one, and the
    one generic line otherwise -- never nothing, because the generic line is
    the one that stops the power cut.
    """
    if not _is_bambu(adapter):
        return None
    block = _pro_block(_declared_model(adapter))
    if block:
        stages = [str(s) for s in (block.get("stages") or []) if str(s).strip()]
        if stages:
            return stages
    return [GENERIC_BAMBU_START]


def cancel_narrative(adapter: Any) -> list[str] | None:
    """The ``what_you_will_see`` list for a cancel on *adapter*, or ``None``.

    Same shape as :func:`start_narrative` so a reader meets one field with
    one meaning on both doors: absent off Bambu, the located per-model line
    when kiln-pro has it, the generic line otherwise.
    """
    if not _is_bambu(adapter):
        return None
    block = _pro_block(_declared_model(adapter))
    if block:
        cancel = str(block.get("cancel") or "").strip()
        if cancel:
            return [cancel]
    return [GENERIC_BAMBU_CANCEL]


def _early_in_start(layer: int | None, completion: float | None) -> bool:
    """Whether the readings say the start sequence may still be running.

    The layer counter is the authority when the printer reports one: the
    start block runs at layer 0 and the first layer is layer 1.  Without a
    layer count the percent stands in -- the vendor's start file itself
    advances the progress readout to a few percent before the first layer.
    Nothing known either way is not "early"; it is unknown, and no line is
    better than a wrong one.
    """
    if layer is not None:
        try:
            return int(layer) <= 0
        except (TypeError, ValueError):
            return False
    if completion is not None:
        try:
            return float(completion) < 5.0
        except (TypeError, ValueError):
            return False
    return False


def start_stage_line(
    adapter: Any,
    *,
    nozzle_target_c: float | None,
    layer: int | None,
    completion: float | None = None,
) -> str | None:
    """The monitor report's one line while a Bambu is still in its start block.

    Says which stage the nozzle target suggests when kiln-pro can read it for
    this model -- a fixed target the vendor's file sets for one stage only
    (250 C on an A1 is the AMS load and flush, 170 C the nozzle wipe) -- and
    the generic reassurance otherwise.  ``None`` off Bambu and once the print
    has left layer 0, so a normal mid-print report is untouched.
    """
    if not _is_bambu(adapter) or not _early_in_start(layer, completion):
        return None
    model = _declared_model(adapter)
    if model and nozzle_target_c is not None:
        try:
            from kiln_pro.bridge import pro_features

            if pro_features.is_available("device_intelligence"):
                reading = pro_features.device_intelligence.start_stage_reading(
                    model, nozzle_target_c=float(nozzle_target_c),
                )
                if isinstance(reading, str) and reading.strip():
                    return reading.strip()
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 -- a missing courtesy line is not a failure
            logger.debug("start-stage line unavailable for %r: %s", model, exc)
    return GENERIC_BAMBU_START_STAGE_LINE
