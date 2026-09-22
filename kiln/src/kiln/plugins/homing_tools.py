"""Homing tools plugin — the one verb every printer screen has.

``home_axes`` is what the Home button does, reachable from an agent, the
command line (``kiln home``), and a recovery flow, through one adapter
template (:meth:`~kiln.printers.base.PrinterAdapter.home_axes`) so the
refusal it carries — not while printing, not while paused with the head
over the part — runs the same way on every door.

The answer says how the homing was sent, which axes its commands
addressed, and where the head was left, every time.  A model with a
vendor-cited sequence runs that; a backend without one sends the generic
home and says it cannot see the path the firmware takes; a backend that
cannot home the way Kiln trusts refuses and names the printer's own
screen.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.  The tool body is a module-level function so
``kiln home`` runs the very same code the MCP tool runs.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

#: Same ceiling, same reason, as the filament door: the host bounds the
#: tool call, so a watch that outlasts it loses its answer.
_WAIT_BUDGET_S = float(os.environ.get("KILN_FILAMENT_WAIT_BUDGET_S", "40") or 0)

_RATE_LIMITS: dict[str, tuple[int, int]] = {"home_axes": (5000, 6), "park_head": (5000, 6)}


def run_home(*, axes: str = "XYZ", printer_name: str | None = None, action: str = "home", **kwargs: Any) -> dict[str, Any]:
    """The one door every surface calls -- for both verbs."""
    import kiln.server as _srv
    from kiln.hotend_safety import MOLTEN_FILAMENT_WARNING
    from kiln.printers.base import HomingUnsupported, ModelDeclarationRequired, PlateClearRequired, PrinterError
    from kiln.registry import PrinterNotFoundError

    tool_name = "park_head" if action == "park" else "home_axes"
    if block := _srv._emergency_latch_error(
        tool_name, _srv._resolve_effective_printer_name(printer_name)
    ):
        return block
    try:
        try:
            adapter, target_name = _srv._resolve_control_target(printer_name)
        except PrinterNotFoundError:
            return _srv._unknown_printer_error(printer_name, "home")
        if block := _srv._emergency_latch_error(tool_name, target_name):
            return block
        options = dict(kwargs)
        if _WAIT_BUDGET_S > 0:
            options["wait_ceiling_seconds"] = _WAIT_BUDGET_S
        result = adapter.park_head(**options) if action == "park" else adapter.home_axes(axes=axes, **options)
        payload: dict[str, Any] = {
            "success": result.success,
            "printer_name": target_name,
            **result.to_dict(),
        }
        if result.heats_nozzle_to_c is not None:
            payload["safety"] = MOLTEN_FILAMENT_WARNING
        if not result.success:
            code = f"{action.upper()}_FAULT" if result.error_code else f"{action.upper()}_FAILED"
            return _srv._error_dict(result.message, code=code, extra={action: payload})
        return payload
    except PlateClearRequired as exc:
        return _srv._error_dict(
            str(exc), code="PLATE_CLEAR_REQUIRED",
            extra={"outcome": "failed", "snapshot_path": exc.snapshot_path, "plate_clear_required": True},
        )
    except ModelDeclarationRequired as exc:
        return _srv._error_dict(
            str(exc), code="PRINTER_MODEL_REQUIRED",
            extra={"outcome": "failed", "printer_model_required": True},
        )
    except HomingUnsupported as exc:
        # Why there was no plan (offline, signed out, unanswered, refused)
        # rides beside the sentence, never inside it.
        why = getattr(exc, "why_fields", None)
        return _srv._error_dict(
            str(exc), code="UNSUPPORTED",
            extra={"outcome": "failed", **(why if isinstance(why, dict) else {})},
        )
    except (PrinterError, RuntimeError) as exc:
        return _srv._error_dict(f"Failed to {action}: {exc}", extra={"outcome": "failed"})
    except Exception as exc:
        _logger.exception("Unexpected error in %s", tool_name)
        return _srv._error_dict(f"Unexpected error in {tool_name}: {exc}", code="INTERNAL_ERROR")


def _gated(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Auth → rate limit → confirmation, in server.py's order.

    ``plan_only`` sends nothing, so it skips the rate limit and the
    confirmation -- but not auth: it still reads the printer's state and
    hands back the vendor sequence, which is a read and is gated as one.
    """
    import kiln.server as _srv

    if args.get("plan_only"):
        return _srv._check_auth("read")
    if err := _srv._check_auth("temperature"):
        return err
    if err := _srv._check_rate_limit(tool_name):
        return err
    return _srv._check_confirmation(tool_name, args)


def home_axes(
    axes: str = "XYZ",
    wait_seconds: float | None = None,
    step: int | None = None,
    plan_only: bool = False,
    plate_clear: bool = False,
    printer_name: str | None = None,
) -> dict[str, Any]:
    """Home the print head — what the Home button on the printer's screen does.

    Refused while a print is running, and while one is paused (the head is
    over the part and homing travels).

    Where the connected model's own start-sequence homing is served one plan at a time through Kiln's hosted service for a printer paired to your Kiln sign-in, free, and kept on your machine so it works offline — raise first, home X, Z found the way
    the maker finds it, park off the plate — it runs that, one described
    motion at a time.  A bare ``G28`` from an unknown height is exactly the
    move that family's own sequences avoid, so a Bambu model whose sequence
    is not served here is refused rather than guessed.  Other backends send
    the firmware's own ``G28`` and say so (``sequence_source:
    "firmware_home_routine"``) -- but only after the catalogue's motion
    record for the declared ``printer_model`` has answered how that routine
    finds Z: on a machine whose Z home presses a probe or the nozzle onto
    the plate (most of them) the tool asks for ``plate_clear`` first, on
    the few whose Z home cannot touch the plate it runs unasked, and with
    no ``printer_model`` declared, or one the catalogue does not know, it
    refuses and says which key to set (code ``PRINTER_MODEL_REQUIRED``).
    Homing X and Y alone never needs the record.  The plan's first step
    says what the person will see -- what descends and where, in the
    vendor's words -- and whether the routine travels sideways before Z is
    known; on a Klipper or USB Marlin machine the unit's own config or
    reports settle the cells the maker left blank, and the plan says so.

    Branch on ``outcome``: ``confirmed`` (the printer was seen at home),
    ``accepted`` (sent, not refused, not read back — the honest answer on
    every backend Kiln cannot read a homed flag from), ``failed``.
    ``homed_axes`` names the axes the sequence's own homing commands
    addressed; ``resting_position`` says where the head was left.

    **Run it in steps the first time on any machine with a person beside
    it.**  ``plan_only=True`` returns the sequence as steps -- what the
    person will see and how each motion knows where to stop -- and sends
    nothing.  ``step=N`` sends only step N, reports what it left armed
    (heater, soft endstops), and describes step N+1 for the next call.  A
    single script cannot be paused between motions, however well it was
    announced; step mode can.

    Args:
        axes: Any of ``X``, ``Y``, ``Z``.  Default all three.
        wait_seconds: How long to watch for a fault code afterwards on
            backends that raise one (default 10).
        step: Send only this step of the sequence (1-based).
        plan_only: Describe the steps; send nothing.
        plate_clear: A PERSON's statement that the plate is empty.  Required
            on every call that homes Z on a model whose Z home presses the
            nozzle onto the plate (the A1 mini), whatever the plate record
            says; without it the tool refuses with
            ``PLATE_CLEAR_REQUIRED`` and, where the printer has a camera,
            ``snapshot_path`` -- look, then call again with it set.  Never
            set it on a person's behalf.
        printer_name: Which printer.  Omit for the default one.
    """
    args = {"axes": axes, "wait_seconds": wait_seconds, "step": step,
            "plan_only": plan_only, "plate_clear": plate_clear, "printer_name": printer_name}
    if gate := _gated("home_axes", args):
        return gate
    kwargs: dict[str, Any] = {}
    if wait_seconds is not None:
        kwargs["wait_seconds"] = wait_seconds
    if step is not None:
        kwargs["step"] = int(step)
    if plan_only:
        kwargs["plan_only"] = True
    if plate_clear:
        kwargs["plate_clear"] = True
    return run_home(axes=axes, printer_name=printer_name, **kwargs)


def park_head(
    wait_seconds: float | None = None,
    step: int | None = None,
    plan_only: bool = False,
    plate_clear: bool = False,
    printer_name: str | None = None,
) -> dict[str, Any]:
    """Move the print head somewhere safe, away from the plate, and leave it there.

    The retreat, as distinct from ``home_axes`` (the measurement).  A park
    NEVER homes Z and never heats: it raises the head the vendor's way,
    homes X (an endstop -- nothing is touched), and travels to the model's
    own off-plate spot -- on a Bambu, the position the machine itself
    flushes at, served one plan at a time through Kiln's hosted service for a printer paired to your Kiln sign-in, free, and kept on your machine so it works offline.  On
    Marlin, Klipper and RepRapFirmware the firmware's own home position IS
    the park, chosen by whoever configured the machine -- and the catalogue's
    motion record for the declared ``printer_model`` decides how much of it
    is sent: on a machine whose Z home presses a probe or the nozzle onto
    the plate (most of them), park homes X and Y only and leaves Z alone;
    on the few whose Z home cannot touch the plate, the full home.  With no
    ``printer_model`` declared, or one the catalogue does not know, park
    refuses and says which key to set (code PRINTER_MODEL_REQUIRED).  A
    Bambu model whose spot is not served here refuses by name rather than
    guessing a coordinate, and says to use the screen's jog controls, Z up
    first -- never its Home button over a part.

    Use this first on any machine you are nervous about: it is the safer
    of the two verbs by construction, because nothing descends.  Same
    ``plan_only`` / ``step`` options as ``home_axes`` -- run it in steps
    the first time with a person beside the machine.

    Args:
        wait_seconds: How long to watch for a fault code afterwards on
            backends that raise one (default 10).
        step: Send only this step of the sequence (1-based).
        plan_only: Describe the steps; send nothing.
        plate_clear: A PERSON's statement that the plate is empty.  Lets a
            park proceed over a plate the record says holds a part, and lets
            the full home (Z included) stand in for the park on a machine
            whose Z home touches the plate.
        printer_name: Which printer.  Omit for the default one.
    """
    args = {"wait_seconds": wait_seconds, "step": step, "plan_only": plan_only,
            "plate_clear": plate_clear, "printer_name": printer_name}
    if gate := _gated("park_head", args):
        return gate
    kwargs: dict[str, Any] = {}
    if wait_seconds is not None:
        kwargs["wait_seconds"] = wait_seconds
    if step is not None:
        kwargs["step"] = int(step)
    if plan_only:
        kwargs["plan_only"] = True
    if plate_clear:
        kwargs["plate_clear"] = True
    return run_home(printer_name=printer_name, action="park", **kwargs)


def run_plate(*, printer_name: str | None = None, action: str = "status", note: str = "") -> dict[str, Any]:
    """The plate record's one door, for the tool and the CLI alike.

    ``status`` reads; ``clear`` writes a person's word (``source: "human"``).
    Neither talks to the printer: the record is what Kiln knows, and a
    person's statement is a fact about the plate, not a round trip.
    """
    import kiln.server as _srv
    from kiln.plate_state import mark_clear, plate_occupancy
    from kiln.registry import PrinterNotFoundError

    try:
        try:
            adapter, target_name = _srv._resolve_control_target(printer_name)
        except PrinterNotFoundError:
            return _srv._unknown_printer_error(printer_name, "plate")
        if action == "clear":
            mark_clear(adapter, "human", note=note)
        state = plate_occupancy(adapter)
        if action == "clear" and not state.clear:
            return _srv._error_dict(
                "The plate could not be recorded as clear: " + (state.note or "the record did not take."),
                code="PLATE_RECORD_FAILED", extra={"plate": state.to_dict()},
            )
        from kiln.plate_state import camera_of

        return {
            "success": True, "printer_name": target_name, "plate": state.to_dict(),
            # Whether this machine could settle an unknown plate by itself.
            # A refusal that has a camera to offer should never tell someone
            # to walk to the printer.
            "camera": camera_of(adapter),
        }
    except Exception as exc:
        _logger.exception("Unexpected error in plate %s", action)
        return _srv._error_dict(f"Unexpected error reading the plate record: {exc}", code="INTERNAL_ERROR")


def plate_status(printer_name: str | None = None) -> dict[str, Any]:
    """What Kiln knows is on the build plate -- read-only, no printer round trip.

    Kiln records the plate at the moments it can be sure of: a print Kiln
    started puts a part there (``occupied``, with the file and, where Kiln
    could read it, the part's height); a print seen ending leaves it there;
    a PERSON says it is empty (``plate_clear=true`` on ``home_axes``, or
    ``kiln plate clear`` at the command line); and a LOOK through the
    machine's camera settles it either way (``look_at_plate``).  Anything
    else -- no record, a torn record, a print started at the printer's own
    screen -- reads as ``unknown``, and the answer then says whether a
    camera could settle it.

    Why it matters: ``home_axes`` and ``park_head`` start with the vendor's
    raise and then cross the head's row a few millimetres up -- and a
    collision there raises no fault.  When the record names a part taller
    than that raise, both refuse and say so; a ``clear`` record is what
    lets them stop asking about the row.  A Z home that presses the nozzle
    onto the plate (the A1 mini) asks on every call regardless -- the
    record cannot see a print started from the printer's own screen, and
    that press is the one motion a stale record must never answer for.

    There is deliberately no tool that marks the plate clear from nothing:
    that is a statement someone has to make, at the machine, at the command
    line, or by looking through the camera and saying what they see.

    Args:
        printer_name: Which printer.  Omit for the default one.

    Returns ``plate`` with ``status`` (``unknown`` / ``occupied`` /
    ``clear``), ``source``, ``since``, ``job`` (``file``, ``footprint_mm``,
    ``max_z_mm``, ``printer_id``), ``from_camera`` and ``looked_by`` when a
    look wrote it, and a one-line ``description``; plus ``camera``, whether
    this machine has one that could settle an unknown plate.
    """
    import kiln.server as _srv

    if err := _srv._check_auth("read"):
        return err
    return run_plate(printer_name=printer_name, action="status")


def look_at_plate(
    printer_name: str | None = None,
    seen: str | None = None,
) -> dict[str, Any]:
    """Look at the build plate through the machine's camera, and record what is there.

    Two steps, one tool.  Called WITHOUT ``seen`` it fetches a frame and
    hands it back for you to look at: ``image_b64`` is the picture, and
    nothing is recorded.  Look at it, then call again with ``seen="clear"``
    (nothing on the plate) or ``seen="occupied"`` (something is), and that
    answer becomes the plate record with you named as the one who looked.

    Kiln ships no vision model and judges nothing here.  The eyes are
    yours; this tool is the camera and the pen.  Say what you actually see:
    a plate you are not sure about is ``occupied``, because the cost of a
    wrong "clear" is a print driven into a part and the cost of a wrong
    "occupied" is one question.

    Works on any printer with a camera Kiln can read -- the machine's own,
    or one registered against it with ``camera_snapshot_url``, which works
    on every printer type including those with no camera of their own.
    A machine with neither says so and stays ``unknown``.

    Args:
        printer_name: Which printer.  Omit for the default one.
        seen: Omit to fetch the picture.  ``"clear"`` or ``"occupied"`` to
            record what you saw in the picture you were just given.

    Returns the ``plate`` record and a ``look`` block saying whether a
    frame was available and which camera it came from.
    """
    import kiln.server as _srv
    from kiln import plate_state
    from kiln.registry import PrinterNotFoundError

    if err := _srv._check_auth("control"):
        return err
    try:
        try:
            adapter, target_name = _srv._resolve_control_target(printer_name)
        except PrinterNotFoundError:
            return _srv._unknown_printer_error(printer_name, "plate")

        if seen is None:
            found = plate_state.look(adapter)
            if not found.available:
                return _srv._error_dict(
                    f"Kiln could not get a picture of {target_name}'s plate: {found.why}.",
                    code="PLATE_LOOK_UNAVAILABLE",
                    extra={"look": found.to_dict(), "plate": plate_state.read(adapter).to_dict()},
                )
            return {
                "success": True,
                "printer_name": target_name,
                "look": found.to_dict(),
                "image_b64": found.image_b64,
                "media_type": found.media_type,
                "plate": plate_state.read(adapter).to_dict(),
                "next": (
                    "Look at the picture, then call look_at_plate again with seen=\"clear\" if the plate is "
                    "empty or seen=\"occupied\" if anything is on it. If you cannot tell, say occupied."
                ),
            }

        if seen not in ("clear", "occupied"):
            return _srv._error_dict(
                'seen must be "clear" (nothing on the plate) or "occupied" (something is on it).',
                code="INVALID_INPUT",
            )
        # An agent is calling this tool, so an agent is what did the looking.
        # A person's own statement has its own doors (`kiln plate clear`,
        # plate_clear=true on home_axes) and is recorded as theirs.
        status = plate_state.mark_from_camera(adapter, seen=seen, judged_by="agent")
        state = plate_state.read(adapter)
        if status is None or state.status != seen:
            return _srv._error_dict(
                "What you saw could not be recorded against this printer: "
                + (state.note or "the record did not take."),
                code="PLATE_RECORD_FAILED", extra={"plate": state.to_dict()},
            )
        return {"success": True, "printer_name": target_name, "plate": state.to_dict()}
    except Exception as exc:
        _logger.exception("Unexpected error in look_at_plate")
        return _srv._error_dict(f"Unexpected error looking at the plate: {exc}", code="INTERNAL_ERROR")


class _HomingToolsPlugin:
    """Home the head.

    Tools:
        - home_axes
        - park_head
        - plate_status
        - look_at_plate
    """

    @property
    def name(self) -> str:
        return "homing_tools"

    @property
    def description(self) -> str:
        return "Home the print head, or park it somewhere safe away from the plate"

    def register(self, mcp: Any) -> None:
        import kiln.server as _srv

        for tool_name, limits in _RATE_LIMITS.items():
            _srv._TOOL_RATE_LIMITS.setdefault(tool_name, limits)
        mcp.tool()(home_axes)
        mcp.tool()(park_head)
        mcp.tool()(plate_status)
        mcp.tool()(look_at_plate)


plugin = _HomingToolsPlugin()
