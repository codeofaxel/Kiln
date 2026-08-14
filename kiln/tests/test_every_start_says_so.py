"""A start reaches the machine it names, and is checked against it.

The stop side learned to aim first: ``cancel_print``, ``pause_print`` and
``resume_print`` take a ``printer_name`` and resolve it once through
``_resolve_control_target``.  The start side did not, so ``upload_file`` and
``start_print`` could only ever address the default connection — which bounds
every multi-printer workflow at the last step, the one that puts plastic on a
bed.  A user with two machines could stop either and start only one.

Aiming a start is more than swapping the adapter, because the gates that
decide whether a start is *allowed* were themselves bound to the default
printer:

  * the pre-flight verdict — is this machine idle, connected, within its
    temperature ceilings — ran against the default printer;
  * the bed-fit gate and the G-code dialect scan read the default printer's
    declared model, so an aimed upload would have been certified against a
    bed the file was never going to touch (the check exists because of a
    nozzle driven into a purge tool: see ``upload_file``'s Incident #0 note);
  * the nozzle-wear consult keyed off whichever name the registry listed
    first, which answers for the wrong hotend;
  * the heater watchdog — one process-wide instance around the default
    adapter — would have been told a print started when a sibling started
    one, and a watchdog that believes it is busy stops cooling an idle,
    hot machine;
  * the confirmation token issued by ``upload_file`` remembered only the
    file, so a confirmed upload aimed at one printer would have landed on
    another.

Each of those is a wrong answer that looks exactly like a right one.  These
tests pin the aimed path and, just as hard, pin that naming nothing still
means the default printer.
"""

from __future__ import annotations

import inspect

import pytest

from kiln import server
from kiln.printers.base import (
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.registry import PrinterRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_server_state(monkeypatch):
    """A fresh registry and watchdog table per test, and no rate limiting.

    The per-tool limiter is per tool NAME, not per printer, so starting a
    second machine moments after the first is refused by a throttle that has
    nothing to say about which machine was addressed.
    """
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(server, "_pending_uploads", {})
    # The preview gate is a user-consent gate, not a targeting one.
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")


class _FakePrinter:
    """A printer that records what it was told to take.

    Deliberately not a MagicMock: these tests turn on *which object* was
    handed the file, and a mock that answers every attribute makes "the
    wrong machine printed it" hard to see.
    """

    name = "fake"

    def __init__(self, host: str, state: PrinterStatus = PrinterStatus.IDLE) -> None:
        self.host = host
        self.serial = ""          # fingerprint falls back to host — distinct per printer
        self._state = state
        self.uploaded: list[str] = []
        self.started: list[str] = []
        self.tool_temps: list[float] = []
        self.bed_temps: list[float] = []
        self.gcode: list[str] = []

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(
            state=self._state,
            connected=True,
            tool_temp_actual=25.0,
            tool_temp_target=0.0,
            bed_temp_actual=25.0,
            bed_temp_target=0.0,
        )

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1024)]

    def upload_file(self, file_path: str) -> UploadResult:
        self.uploaded.append(file_path)
        return UploadResult(success=True, file_name="part.gcode", message="uploaded")

    def start_print(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        self._state = PrinterStatus.PRINTING
        return PrintResult(success=True, message="started")

    def set_tool_temp(self, value: float) -> bool:
        self.tool_temps.append(value)
        return True

    def set_bed_temp(self, value: float) -> bool:
        self.bed_temps.append(value)
        return True

    def send_gcode(self, commands) -> None:
        self.gcode.extend(commands)


def _two_printers(monkeypatch, **kwargs) -> tuple[_FakePrinter, _FakePrinter]:
    """The bench this is about: ``garage`` is the default, ``workshop`` is the
    machine the start-side verbs could not reach."""
    garage = _FakePrinter("192.0.2.10", **kwargs)
    workshop = _FakePrinter("192.0.2.11", **kwargs)
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    return garage, workshop


@pytest.fixture
def gcode(tmp_path):
    """A file that passes the size, extension and bed-fit gates."""
    path = tmp_path / "part.gcode"
    path.write_text("G28\nG1 X10 Y10 Z0.2 E1\nM104 S200\n")
    return str(path)


def _start(*, skip_preflight=True, **kwargs) -> dict:
    """``start_print`` with pre-flight stood down unless a test is about it."""
    import os
    from unittest.mock import patch

    env = {"KILN_SKIP_PREFLIGHT": "1"} if skip_preflight else {}
    with patch.dict(os.environ, env):
        return server.start_print(**kwargs)


# ---------------------------------------------------------------------------
# The gap itself: the start-side verbs can name a machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb", ["upload_file", "start_print", "preflight_check", "set_temperature"],
)
def test_every_start_side_verb_can_name_a_printer(verb):
    """The signature IS the capability — this is what was missing.

    ``preflight_check`` is in this list because ``start_print`` delegates its
    whole safety verdict to it: an aimed start whose gate reads another
    machine is a start that was never really checked.
    """
    tool = getattr(server, verb)
    sig = inspect.signature(getattr(tool, "fn", tool))
    assert "printer_name" in sig.parameters
    # Defaulting to None is what keeps every existing caller working.
    assert sig.parameters["printer_name"].default is None


def test_start_print_takes_its_printer_last(monkeypatch):
    """Positional callers keep their meaning.

    ``file_name`` has to stay first, so ``printer_name`` goes last rather
    than second — inserting it after the file would silently swallow a
    positional ``use_ams`` and aim the print at "auto".
    """
    sig = inspect.signature(getattr(server.start_print, "fn", server.start_print))
    assert list(sig.parameters)[-1] == "printer_name"


def test_upload_reaches_the_printer_it_names(monkeypatch, gcode):
    garage, workshop = _two_printers(monkeypatch)

    out = server.upload_file(gcode, printer_name="workshop")

    assert out.get("success") is True
    assert (workshop.uploaded, garage.uploaded) == ([gcode], [])
    # The answer names the machine: start_print has to be aimed at the
    # same one, and "uploaded" alone does not say where.
    assert out["printer_name"] == "workshop"


def test_start_reaches_the_printer_it_names(monkeypatch):
    garage, workshop = _two_printers(monkeypatch)

    out = _start(file_name="part.gcode", printer_name="workshop")

    assert out.get("success") is not False
    assert (workshop.started, garage.started) == (["part.gcode"], [])
    assert out["printer_name"] == "workshop"


@pytest.mark.parametrize("verb", ["upload_file", "start_print"])
def test_an_unaimed_start_still_names_the_default(monkeypatch, gcode, verb):
    """A single-printer bench gets the name too — it is the same question."""
    garage, workshop = _two_printers(monkeypatch)

    out = (
        server.upload_file(gcode)
        if verb == "upload_file"
        else _start(file_name="part.gcode")
    )

    assert out["printer_name"] == "garage"


@pytest.mark.parametrize("verb", ["upload_file", "start_print"])
def test_naming_no_printer_still_means_the_default(monkeypatch, gcode, verb):
    """Every existing caller passes no name and must keep the old behaviour."""
    garage, workshop = _two_printers(monkeypatch)

    if verb == "upload_file":
        server.upload_file(gcode)
        assert (garage.uploaded, workshop.uploaded) == ([gcode], [])
    else:
        _start(file_name="part.gcode")
        assert (garage.started, workshop.started) == (["part.gcode"], [])


@pytest.mark.parametrize("verb", ["upload_file", "start_print"])
def test_a_name_kiln_does_not_know_prints_nothing(monkeypatch, gcode, verb):
    """Never silently redirect to the default: that prints on the wrong bed.

    An agent that mistypes a printer name must be told, not quietly obeyed
    against another machine.
    """
    garage, workshop = _two_printers(monkeypatch)

    if verb == "upload_file":
        out = server.upload_file(gcode, printer_name="workshopp")
    else:
        out = _start(file_name="part.gcode", printer_name="workshopp")

    assert out.get("success") is False
    assert out["error"]["code"] == "PRINTER_NOT_FOUND"
    assert "workshopp" in out["error"]["message"]
    assert (garage.uploaded, garage.started) == ([], [])
    assert (workshop.uploaded, workshop.started) == ([], [])


# ---------------------------------------------------------------------------
# The gates that decide whether a start is allowed follow the same machine
# ---------------------------------------------------------------------------


def test_preflight_judges_the_printer_it_names(monkeypatch):
    """The verdict is about one printer, and it has to be the right one."""
    garage, workshop = _two_printers(monkeypatch)
    workshop._state = PrinterStatus.PRINTING

    assert server.preflight_check()["ready"] is True
    assert server.preflight_check(printer_name="workshop")["ready"] is False


def test_a_busy_named_printer_refuses_the_start(monkeypatch):
    """The regression this closes: an aimed start whose gate read the default.

    ``garage`` is idle and ``workshop`` is mid-print.  With the pre-flight
    gate bound to the default printer, starting on ``workshop`` would have
    been waved through by ``garage``'s readiness — onto a machine already
    printing.
    """
    garage, workshop = _two_printers(monkeypatch)
    workshop._state = PrinterStatus.PRINTING

    out = _start(skip_preflight=False, file_name="part.gcode", printer_name="workshop")

    assert out["success"] is False
    assert out["error"]["code"] == "PREFLIGHT_FAILED"
    assert workshop.started == []


def test_a_second_machine_is_not_measured_against_the_defaults_bed():
    """Bed-fit and temperature ceilings read the named machine's model.

    A legacy single-printer config states one ``printer_model`` at the top
    level.  Lending it to a second machine is the wrong-bed answer the
    bed-fit gate exists to prevent, so a machine with no declared model
    resolves to nothing (the gates then soft-pass and say so) rather than
    inheriting the default's.
    """
    from kiln import printer_model_resolver as pmr

    config = {
        "active_printer": "garage",
        "printer_model": "bambu_a1",
        "printers": {
            "garage": {"type": "bambu", "printer_model": "bambu_a1"},
            "workshop": {"type": "bambu", "printer_model": "bambu_p1s"},
            "mystery": {"type": "bambu"},
        },
    }
    pmr.invalidate_cache()
    original = pmr._load_yaml_config
    pmr._load_yaml_config = lambda: config
    try:
        assert pmr.resolve_printer_model_for("workshop") == "bambu_p1s"
        assert pmr.resolve_printer_model_for("garage") == "bambu_a1"
        # No name at all still answers for the active printer.
        assert pmr.resolve_printer_model_for(None) == "bambu_a1"
        # Declared nowhere: skipped, never borrowed from the top level.
        assert pmr.resolve_printer_model_for("mystery") is None
    finally:
        pmr._load_yaml_config = original
        pmr.invalidate_cache()


def test_the_nozzle_consult_asks_about_the_printer_that_will_print(monkeypatch):
    """Nozzle wear is a property of one hotend.

    The consult keyed off the registry's first name, which is the default
    printer on any bench where it was registered first — so an aimed start
    would have been cleared (or refused) on a sibling's wear record.
    """
    garage, workshop = _two_printers(monkeypatch)
    monkeypatch.setattr(
        workshop,
        "list_files",
        lambda: [
            PrinterFile(
                name="part.gcode", path="part.gcode", filament_used_mm=5000.0,
            )
        ],
    )
    asked: list[str] = []

    from kiln import _pro_nozzle_bridge

    monkeypatch.setattr(_pro_nozzle_bridge, "available", lambda: True)
    monkeypatch.setattr(
        _pro_nozzle_bridge,
        "consult_capacity",
        lambda *, printer_id, planned_grams, filament_material: (
            asked.append(printer_id) or None
        ),
    )

    _start(file_name="part.gcode", printer_name="workshop")

    assert asked == ["workshop"]


def test_a_latch_on_the_named_printer_refuses_the_start(monkeypatch):
    """A latched machine cannot be started, and only that machine is stopped."""
    garage, workshop = _two_printers(monkeypatch)
    monkeypatch.setattr(
        server,
        "_emergency_latch_error",
        lambda tool, name: (
            {"success": False, "error": {"code": "EMERGENCY_LATCHED", "message": name}}
            if name == "workshop"
            else None
        ),
    )

    blocked = _start(file_name="part.gcode", printer_name="workshop")
    assert blocked["success"] is False
    assert blocked["error"]["code"] == "EMERGENCY_LATCHED"
    assert workshop.started == []

    assert _start(file_name="part.gcode").get("success") is not False
    assert garage.started == ["part.gcode"]


def test_a_latch_under_either_name_refuses_the_start(monkeypatch):
    """One machine can answer to two names; a latch on it is on all of them.

    The caller's alias and the name the lifecycle files under are resolved
    by different helpers and can disagree — registering a machine again
    renames it.  Checking only the alias would let a latched printer be
    started by naming it the other way.
    """
    garage, workshop = _two_printers(monkeypatch)
    # The same machine, registered again: the lifecycle name is now "bench",
    # while a caller can still reach it as "workshop".
    server._get_registry().register("bench", workshop)
    latched: list[str] = []
    monkeypatch.setattr(
        server,
        "_emergency_latch_error",
        lambda tool, name: (
            {"success": False, "error": {"code": "EMERGENCY_LATCHED", "message": name}}
            if latched and name == latched[0]
            else None
        ),
    )

    latched.append("bench")
    out = _start(file_name="part.gcode", printer_name="workshop")

    assert out["success"] is False
    assert out["error"]["code"] == "EMERGENCY_LATCHED"
    assert workshop.started == []


# ---------------------------------------------------------------------------
# Process-wide bookkeeping is filed under the machine that started
# ---------------------------------------------------------------------------


def test_the_watchdog_is_keyed_under_the_printer_that_started(monkeypatch):
    """A watchdog filed under the default name would be torn down by the
    default printer's next start, leaving a live print unwatched."""
    garage, workshop = _two_printers(monkeypatch)
    spawned: list[tuple[object, str]] = []
    monkeypatch.setattr(
        server,
        "_spawn_print_watchdog",
        lambda adapter, file_name: spawned.append((adapter, file_name)),
    )

    _start(file_name="part.gcode", printer_name="workshop")

    assert spawned == [(workshop, "part.gcode")]


def test_the_heater_watchdog_is_not_told_about_someone_elses_print(monkeypatch):
    """It watches the default printer and nothing else.

    Told that a print started when a sibling started one, it marks itself
    busy — and its idle tick, the thing that cools a machine nobody is
    using, stops firing for a default printer sitting idle and hot.
    """
    garage, workshop = _two_printers(monkeypatch)
    notified: list[str] = []

    class _Watchdog:
        @staticmethod
        def notify_print_started() -> None:
            notified.append("started")

    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _Watchdog)

    _start(file_name="part.gcode", printer_name="workshop")
    assert notified == []

    # The default printer's own start is still its news.
    _start(file_name="part.gcode")
    assert notified == ["started"]


# ---------------------------------------------------------------------------
# The other door: a confirmed upload
# ---------------------------------------------------------------------------


def test_a_confirmed_upload_lands_on_the_printer_it_was_aimed_at(monkeypatch, gcode):
    """``upload_file_confirm`` is handed a token and nothing else.

    With the target left out of the token, confirming an aimed upload
    re-resolved the default adapter and sent the file to the wrong machine —
    after the user had been asked about, and approved, the right one.
    """
    garage, workshop = _two_printers(monkeypatch)
    monkeypatch.setattr(server, "_CONFIRM_UPLOAD", True)

    pending = server.upload_file(gcode, printer_name="workshop")
    assert pending["confirmation_required"] is True
    assert pending["printer_name"] == "workshop"
    assert (garage.uploaded, workshop.uploaded) == ([], [])

    out = server.upload_file_confirm(pending["token"])

    assert out.get("success") is True
    assert (workshop.uploaded, garage.uploaded) == ([gcode], [])


def test_an_unaimed_confirmed_upload_still_lands_on_the_default(monkeypatch, gcode):
    garage, workshop = _two_printers(monkeypatch)
    monkeypatch.setattr(server, "_CONFIRM_UPLOAD", True)

    pending = server.upload_file(gcode)
    out = server.upload_file_confirm(pending["token"])

    assert out.get("success") is True
    assert (garage.uploaded, workshop.uploaded) == ([gcode], [])


# ---------------------------------------------------------------------------
# Heat is aimed too: preheating is the step before a start
# ---------------------------------------------------------------------------


def test_heat_reaches_the_printer_it_names(monkeypatch):
    """Preheating the second machine used to heat the default one."""
    garage, workshop = _two_printers(monkeypatch)

    out = server.set_temperature(tool_temp=200.0, bed_temp=60.0, printer_name="workshop")

    assert out["success"] is True
    assert out["printer_name"] == "workshop"
    assert (workshop.tool_temps, workshop.bed_temps) == ([200.0], [60.0])
    assert (garage.tool_temps, garage.bed_temps) == ([], [])


def test_unaimed_heat_still_means_the_default(monkeypatch):
    garage, workshop = _two_printers(monkeypatch)

    out = server.set_temperature(tool_temp=200.0)

    assert out["printer_name"] == "garage"
    assert (garage.tool_temps, workshop.tool_temps) == ([200.0], [])


def test_heating_a_name_kiln_does_not_know_heats_nothing(monkeypatch):
    garage, workshop = _two_printers(monkeypatch)

    out = server.set_temperature(tool_temp=200.0, printer_name="workshopp")

    assert out["success"] is False
    assert out["error"]["code"] == "PRINTER_NOT_FOUND"
    assert (garage.tool_temps, workshop.tool_temps) == ([], [])


def test_the_ceiling_belongs_to_the_machine_being_heated(monkeypatch):
    """A machine with no declared model is held to the unknown-printer
    ceiling, not to the default printer's — which may be the looser of the
    two, and lending it out is how an unidentified hotend gets overdriven."""
    garage, workshop = _two_printers(monkeypatch)
    seen: list[str | None] = []
    real_limits = server._get_temp_limits

    def _spy(printer_name=None):
        seen.append(printer_name)
        return real_limits(printer_name)

    monkeypatch.setattr(server, "_get_temp_limits", _spy)

    server.set_temperature(tool_temp=200.0, printer_name="workshop")

    assert seen == ["workshop"]


def test_a_latched_machine_can_still_be_cooled_but_not_heated(monkeypatch):
    """The heater-off carve-out survives aiming.

    A latched printer must never be told to heat, and must always be
    allowed to cool — that is the whole point of the latch.
    """
    garage, workshop = _two_printers(monkeypatch)
    monkeypatch.setattr(
        server,
        "_emergency_latch_error",
        lambda tool, name: (
            {"success": False, "error": {"code": "EMERGENCY_LATCHED", "message": name}}
            if name == "workshop"
            else None
        ),
    )

    heated = server.set_temperature(tool_temp=200.0, printer_name="workshop")
    assert heated["success"] is False
    assert heated["error"]["code"] == "EMERGENCY_LATCHED"
    assert workshop.tool_temps == []

    cooled = server.set_temperature(tool_temp=0.0, printer_name="workshop")
    assert cooled["success"] is True
    assert workshop.tool_temps == [0.0]


def test_the_heater_watchdog_is_not_told_about_a_siblings_heaters(monkeypatch):
    """It watches the default printer; a sibling's heaters are not its news."""
    garage, workshop = _two_printers(monkeypatch)
    notified: list[str] = []

    class _Watchdog:
        @staticmethod
        def notify_heater_set() -> None:
            notified.append("set")

    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _Watchdog)

    server.set_temperature(tool_temp=200.0, printer_name="workshop")
    assert notified == []

    server.set_temperature(tool_temp=200.0)
    assert notified == ["set"]


def test_a_confirmed_upload_refuses_if_the_default_moved(monkeypatch, gcode):
    """The user approved a machine by name, not "whatever is default now".

    An unaimed token resolves the default printer twice — once when the
    token is issued and once when it is confirmed — and a registration or
    config edit in between moves it.  Refusing beats uploading to a
    machine nobody was asked about.
    """
    garage, workshop = _two_printers(monkeypatch)
    monkeypatch.setattr(server, "_CONFIRM_UPLOAD", True)

    pending = server.upload_file(gcode)
    assert pending["printer_name"] == "garage"

    # The default moves out from under the pending token.
    monkeypatch.setattr(server, "_get_adapter", lambda: workshop)

    out = server.upload_file_confirm(pending["token"])

    assert out["success"] is False
    assert out["error"]["code"] == "PRINTER_CHANGED"
    assert (garage.uploaded, workshop.uploaded) == ([], [])


def test_a_token_issued_before_aiming_still_confirms(monkeypatch, gcode):
    """Tokens outlive a restart-free upgrade; a bare path must still work."""
    garage, workshop = _two_printers(monkeypatch)
    server._pending_uploads["legacy"] = gcode

    out = server.upload_file_confirm("legacy")

    assert out.get("success") is True
    assert garage.uploaded == [gcode]
