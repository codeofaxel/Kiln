"""A tool marked read-only reaches nothing that changes the person's things.

``test_every_tool_is_annotated.py`` proves each tool CARRIES a label; this
proves a read-only label is TRUE.  A host runs a read-only tool without
asking, so one that reaches a printer command, a saved-record write, a spent
allowance, a stored secret, an event Kiln acts on, a place the caller names,
or another tool that changes things runs that change unasked.  The walk and
its rules live in ``tests/_read_only_reach.py``, shared with kiln-pro's twin
so both repos judge alike.

A finding is fixed by relabelling the tool (or moving the change out of it),
never by widening the rules.  ``DECIDED`` is for a hit that really changes
nothing the person owns, with the reason.  A key is ``"<name>: <what>"``,
where ``<name>`` is the tool or any function on the hit's chain (its full
``module.function`` name where two share a short one), so deciding a writer
once covers every tool that reaches it.  A key no hit matches is stale
and fails, so the list cannot outlive what it excuses.
"""

from __future__ import annotations

import threading

import kiln.server as server
from kiln.events import Event, EventBus, EventType
from tests import _read_only_reach as scan

#: "<tool or function on the chain>: <what>" -> why it changes nothing the
#: person owns.  Public Kiln's writers are decided beside the rules
#: (``_read_only_reach.DECIDED``) so kiln-pro's twin reads the same decision.
DECIDED: dict[str, str] = scan.DECIDED


def _decided_by(hit: scan.Reach) -> str | None:
    return scan.decided_by(hit, DECIDED)


def _findings() -> list[scan.Reach]:
    server._ensure_internal_tool_plugins_registered()
    registry = dict(server.mcp._tool_manager._tools)
    read_only, changing = scan.read_only_tools(registry)
    found: list[scan.Reach] = []
    for name, fn in sorted(read_only.items()):
        if (getattr(fn, "__module__", "") or "").startswith("kiln_pro"):
            continue  # kiln-pro's own tests judge kiln-pro's tools
        # Public code only, as every plain install and public CI has it; what
        # kiln-pro adds to a public tool is judged by kiln-pro's twin.
        found.extend(scan.reach(name, fn, changing, public_only=True))
    return found


def test_no_read_only_tool_reaches_a_change() -> None:
    found = _findings()
    undecided = sorted({str(hit) for hit in found if _decided_by(hit) is None})
    assert not undecided, (
        "read-only tools that reach a change (relabel them, or move the change out):\n  "
        + "\n  ".join(undecided)
    )
    stale = sorted(set(DECIDED) - {_decided_by(hit) for hit in found})
    assert not stale, f"DECIDED rows the walk no longer finds: {stale}"


# --- Each rule can fail ------------------------------------------------------


def _kiln(*fns):
    for fn in fns:
        fn.__module__ = "kiln.fake"
    return fns[-1]


def _whats(tool) -> list[str]:
    return [hit.what for hit in scan.reach(tool.__name__, tool, {})]


def test_the_walk_sees_a_printer_command_two_calls_down() -> None:
    """A helper that starts a print is found through a helper."""

    class _Adapter:
        def start_print(self, path: str) -> None: ...

    adapter = _Adapter()

    def _go(path: str) -> None:
        adapter.start_print(path)

    def _middle(path: str) -> None:
        _go(path)

    def looks_harmless(path: str) -> dict:
        _middle(path)
        return {}

    _kiln(_go, _middle, looks_harmless)
    assert "printer command start_print" in _whats(looks_harmless)


def test_reads_of_a_printer_stay_read_only() -> None:
    commands = scan.printer_commands()
    for read in ("get_state", "get_job", "get_snapshot", "list_files", "async_get_state"):
        assert read not in commands
    for command in ("start_print", "cancel_print", "send_gcode", "set_tool_temp", "home_axes"):
        assert command in commands


def test_commands_only_some_brands_have_are_commands() -> None:
    """``set_light`` exists on the Bambu adapter alone, ``skip_objects`` on
    five; the base class names neither."""
    commands = scan.printer_commands()
    for command in ("set_light", "set_fan", "skip_objects", "set_speed_profile"):
        assert command in commands, command


def test_an_allowance_spent_behind_a_leading_underscore_is_seen() -> None:
    def _consume_return(machine: str) -> bool:
        return True

    def status(machine: str) -> dict:
        _consume_return(machine)
        return {}

    _kiln(_consume_return, status)
    assert "spends an allowance (_consume_return)" in _whats(status)


def test_a_plain_store_writer_call_is_seen() -> None:
    def _write_store(data: dict) -> None: ...

    def status() -> dict:
        _write_store({"engaged": "garage"})
        return {}

    _kiln(_write_store, status)
    assert "store write _write_store" in _whats(status)


class _Engine:
    def judge(self, readings: dict) -> None:
        self._announce(readings)

    def _announce(self, readings: dict) -> None:
        _bus.publish(Event(type=EventType.PRINT_FAILED, data=readings))


_Engine.__module__ = _Engine.judge.__module__ = _Engine._announce.__module__ = "kiln.fake"
_bus = EventBus()


def _get_engine() -> _Engine:
    return _Engine()


def test_a_method_on_an_object_and_on_self_is_followed_to_an_event() -> None:
    """The incident shape: tool -> getter's typed object -> method -> self
    method -> a print-failed announcement, which retires a watchdog."""

    def detect(readings: dict) -> dict:
        engine = _get_engine()
        engine.judge(readings)
        return {}

    _kiln(_get_engine, detect)
    assert any("announces PRINT_FAILED" in what for what in _whats(detect))


def test_only_an_event_kiln_acts_on_is_a_change() -> None:
    """Print-failed retires a watchdog; an event only the wildcard listeners
    hear is saved and sent to webhooks, which tells and changes nothing; an
    event whose type the walk cannot read counts as acted on."""
    import ast

    notice = next(k.name for k in EventType if k.name not in scan.acted_on_events())

    def announces(source: str) -> list[str]:
        return [w for w in scan._effects(ast.parse(source), frozenset()) if "announces" in w]

    assert announces("bus.publish(Event(type=EventType.PRINT_FAILED))")
    assert not announces(f"bus.publish(Event(type=EventType.{notice}))")
    assert announces("bus.publish(event)")


def test_a_function_handed_to_a_thread_is_followed() -> None:
    def _cool_down() -> None:
        _write_store({"hotend": 0})

    def _write_store(data: dict) -> None: ...

    def status() -> dict:
        threading.Thread(target=_cool_down).start()
        return {}

    _kiln(_cool_down, _write_store, status)
    assert "store write _write_store" in _whats(status)


def test_a_place_the_caller_names_is_a_write() -> None:
    def preview(texture: str, output_path: str = "") -> dict:
        return {}

    _kiln(preview)
    assert "writes where the caller says (output_path)" in _whats(preview)


def test_the_walk_goes_past_four_calls() -> None:
    def _write_store(data: dict) -> None: ...

    def _f5() -> None:
        _write_store({})

    def _f4() -> None:
        _f5()

    def _f3() -> None:
        _f4()

    def _f2() -> None:
        _f3()

    def _f1() -> None:
        _f2()

    def status() -> dict:
        _f1()
        return {}

    _kiln(_write_store, _f5, _f4, _f3, _f2, _f1, status)
    assert "store write _write_store" in _whats(status)


def _paid(*fns):
    for fn in fns:
        fn.__module__ = "kiln_pro"
    return fns[-1]


def test_the_public_check_never_walks_into_paid_code() -> None:
    """Public code that calls into kiln-pro is judged where kiln-pro is: on a
    machine with it installed the full walk sees the write, the public walk
    never does (exactly what public CI, with no kiln-pro, sees), and
    ``added_by_pro`` hands that one finding to kiln-pro's twin."""

    def _write_store(data: dict) -> None: ...

    def _paid_helper() -> None:
        _write_store({"x": 1})

    def status() -> dict:
        _paid_helper()
        return {}

    _paid(_write_store, _paid_helper)
    _kiln(status)
    full = [hit.what for hit in scan.reach("status", status, {})]
    assert "store write _write_store" in full
    assert scan.reach("status", status, {}, public_only=True) == []
    assert [hit.what for hit in scan.added_by_pro("status", status, {})] == ["store write _write_store"]


def test_a_public_write_reached_through_paid_code_stays_the_public_checks() -> None:
    """A public writer met first through kiln-pro is still walked as public,
    so the public check judges it and the twin is handed only the paid path."""

    def _write_store(data: dict) -> None: ...

    def _public_writer() -> None:
        _write_store({"x": 1})

    def _paid_helper() -> None:
        _public_writer()

    def status() -> dict:
        _paid_helper()
        _public_writer()
        return {}

    _kiln(_write_store, _public_writer, status)
    _paid(_paid_helper)
    public = scan.reach("status", status, {}, public_only=True)
    assert [hit.via for hit in public] == ["status -> _public_writer"]
    paid = scan.added_by_pro("status", status, {})
    assert [hit.via for hit in paid] == ["status -> _paid_helper -> _public_writer"]


def test_an_event_only_paid_code_acts_on_is_not_a_public_finding(monkeypatch) -> None:
    from types import SimpleNamespace

    def _public_handler(event) -> None: ...

    def _paid_handler(event) -> None: ...

    _kiln(_public_handler)
    _paid(_paid_handler)
    bus = SimpleNamespace(_handlers={
        EventType.PRINT_FAILED: [(_public_handler, None)],
        EventType.PRINT_PAUSED: [(_paid_handler, None)],
    })
    monkeypatch.setattr(server, "_get_event_bus", lambda: bus)
    scan.acted_on_events.cache_clear()
    try:
        assert scan.acted_on_events(True) == {"PRINT_FAILED"}
        assert scan.acted_on_events(False) == {"PRINT_FAILED", "PRINT_PAUSED"}
    finally:
        scan.acted_on_events.cache_clear()
