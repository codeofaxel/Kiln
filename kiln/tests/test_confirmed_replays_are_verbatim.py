"""A confirmed action executes exactly the call the user was shown.

``KILN_CONFIRM_MODE`` works by interception: the destructive tool stores its
arguments and returns a token, and ``confirm_action`` replays the tool with
``fn(**stored_args)``.  Anything a tool leaves out of the stored dict silently
reverts to its default on the replay — the action performed is not the action
that was confirmed.

That was not hypothetical.  Measured before the fix:

  * ``clear_emergency_stop`` stored two of its three parameters and dropped
    the REQUIRED ``acknowledgement_note`` — every confirmed replay died on a
    TypeError, so with confirm mode on the latch could not be cleared through
    the tool at all.  Confirm mode is exactly the deployment that cares most
    about the e-stop lifecycle, and it was the one where half of it was
    unusable.
  * ``emergency_stop`` stored ``{}`` — a stop the user confirmed for ONE
    printer replayed as ``printer_name=None``: stop ALL printers.
  * ``cancel_print`` stored only ``printer_name`` — a
    ``preserve_temperatures=True`` cancel (staged mid-print swap, bed must
    stay hot) replayed as a plain cooling cancel.
  * ``start_print`` stored only ``file_name`` — plate number, AMS mapping and
    calibration switches all reverted to defaults on the confirmed start.

The AST test is the engine-level pin: every ``_check_confirmation`` call site
in server.py must store a dict whose keys are exactly the enclosing tool's
parameters, so the next parameter added to a gated tool (this round it was
``printer_name``) cannot be silently dropped from the replay again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiln import server
from kiln.registry import PrinterRegistry

_SERVER_PY = Path(server.__file__).resolve()


# ---------------------------------------------------------------------------
# Engine pin: stored args == tool signature, for every gated tool, forever
# ---------------------------------------------------------------------------


def _confirmation_sites() -> list[tuple[str, set[str], set[str]]]:
    """Yield ``(tool_name, stored_keys, function_params)`` per gate site."""
    tree = ast.parse(_SERVER_PY.read_text(encoding="utf-8"))
    sites: list[tuple[str, set[str], set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in node.args.args + node.args.kwonlyargs} - {"self"}
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_check_confirmation"
                and len(call.args) >= 2
            ):
                tool_name = (
                    call.args[0].value
                    if isinstance(call.args[0], ast.Constant)
                    else node.name
                )
                dict_node = call.args[1]
                assert isinstance(dict_node, ast.Dict), (
                    f"{tool_name}: _check_confirmation args must be a dict "
                    "literal so this test can read the stored keys"
                )
                keys = {
                    k.value for k in dict_node.keys if isinstance(k, ast.Constant)
                }
                sites.append((tool_name, keys, params))
    return sites


def test_the_gate_sites_were_all_found():
    """The AST walk found the gates.  If this number drops to zero the
    completeness test below vacuously passes, which is its own bug."""
    assert len(_confirmation_sites()) >= 7


@pytest.mark.parametrize(
    ("tool_name", "stored", "params"),
    _confirmation_sites(),
    ids=[s[0] for s in _confirmation_sites()],
)
def test_every_confirmation_stores_the_complete_call(tool_name, stored, params):
    """Stored args must be EXACTLY the tool's parameters.

    A missing key replays as the default (the confirmed action mutates); an
    extra key replays as an unexpected kwarg (the confirmed action crashes).
    """
    missing = params - stored
    extra = stored - params
    assert not missing, (
        f"{tool_name} does not store {sorted(missing)} for its confirmation "
        "replay — confirm_action would execute it with default values the "
        "user never saw."
    )
    assert not extra, (
        f"{tool_name} stores {sorted(extra)} which are not parameters — "
        "confirm_action's replay would die on an unexpected keyword."
    )


# ---------------------------------------------------------------------------
# Live replays — the mechanism, exercised end to end
# ---------------------------------------------------------------------------


class _FakePrinter:
    name = "fake"

    def __init__(self, host: str) -> None:
        self.host = host
        self.serial = ""
        self.cancelled = 0
        self.tool_temps: list[float] = []
        self.bed_temps: list[float] = []

    def get_state(self):
        from kiln.printers.base import PrinterState, PrinterStatus

        return PrinterState(
            state=PrinterStatus.PRINTING,
            connected=True,
            tool_temp_actual=220.0,
            tool_temp_target=220.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )

    def cancel_print(self):
        from kiln.printers.base import PrintResult

        self.cancelled += 1
        return PrintResult(success=True, message="cancelled")

    def set_tool_temp(self, value: float) -> None:
        self.tool_temps.append(value)

    def set_bed_temp(self, value: float) -> None:
        self.bed_temps.append(value)


@pytest.fixture()
def _confirm_mode_bench(monkeypatch):
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_pause_keepalive", server._PauseKeepAlive())
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_pending_confirmations", {})
    monkeypatch.setattr(server, "_CONFIRM_MODE", True)
    garage = _FakePrinter("192.0.2.10")
    workshop = _FakePrinter("192.0.2.11")
    server._get_registry().register("garage", garage)
    server._get_registry().register("workshop", workshop)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    return garage, workshop


def test_confirmed_cancel_is_the_cancel_that_was_requested(_confirm_mode_bench):
    """Named printer AND preserve_temperatures both survive the round trip."""
    garage, workshop = _confirm_mode_bench

    challenge = server.cancel_print(
        printer_name="workshop", preserve_temperatures=True
    )
    assert challenge.get("confirmation_required") is True
    assert workshop.cancelled == 0  # nothing happened yet

    out = server.confirm_action(challenge["token"])

    assert out["success"] is True
    assert (workshop.cancelled, garage.cancelled) == (1, 0)
    # The preserved targets prove preserve_temperatures reached the replay.
    assert out["preserved_temperatures"] == {"tool_target": 220.0, "bed_target": 60.0}
    assert workshop.tool_temps == [220.0]
    assert garage.tool_temps == []


def test_confirmed_emergency_stop_stays_scoped_to_its_printer(
    _confirm_mode_bench, monkeypatch
):
    """A stop confirmed for one machine must not replay as stop-ALL."""
    import kiln.emergency as emergency

    stops: list[str | None] = []

    class _Coord:
        def emergency_stop(self, printer_id, **kw):
            stops.append(printer_id)
            return SimpleRecord()

        def emergency_stop_all(self, **kw):
            stops.append(None)
            return [SimpleRecord()]

    class SimpleRecord:
        def to_dict(self):
            return {"success": True}

    monkeypatch.setattr(emergency, "get_emergency_coordinator", lambda: _Coord())

    challenge = server.emergency_stop(printer_name="workshop", reason="user_request")
    assert challenge.get("confirmation_required") is True

    out = server.confirm_action(challenge["token"])

    assert out["success"] is True
    assert stops == ["workshop"]


def test_confirmed_clear_emergency_stop_actually_executes(
    _confirm_mode_bench, monkeypatch
):
    """The replay must reach the coordinator, not die on a TypeError.

    Before the fix the stored args were missing the required
    ``acknowledgement_note`` and every confirmed clear failed with
    INTERNAL_ERROR — confirm mode made the latch permanent.
    """
    import kiln.emergency as emergency

    cleared: list[tuple[str, str]] = []

    class _Coord:
        def clear_stop_with_ack(self, printer_id, *, acknowledged_by, ack_note):
            cleared.append((printer_id, ack_note))
            return {"success": True, "cleared": True, "status": {"latched": False}}

    monkeypatch.setattr(emergency, "get_emergency_coordinator", lambda: _Coord())

    challenge = server.clear_emergency_stop(
        printer_name="workshop", acknowledgement_note="hazard cleared, bed cool"
    )
    assert challenge.get("confirmation_required") is True

    out = server.confirm_action(challenge["token"])

    assert out.get("success") is not False, out
    assert cleared == [("workshop", "hazard cleared, bed cool")]
