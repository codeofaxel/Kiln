"""Every print Kiln starts gets a print watchdog, and the watchdog leaves with it.

Both halves were broken.

Attaching: the watchdog was spawned by the ``start_print`` tool and by nothing
else, while the scheduler, the pipelines, ``slice_and_print``,
``generate_and_print``, ``start_monitored_print``, ``retry_print_with_fix`` and
``download_and_upload`` all started prints with no watchdog behind them.  It
now attaches inside ``PrinterAdapter.start_print``, the template every one of
those doors passes through, and only when the printer accepted the job.

Leaving: nothing retired a watchdog when its print finished -- no code in kiln
publishes the PRINT_COMPLETED event the old teardown waited for -- so a
watchdog outlived its print and kept polling the machine, ready to police the NEXT print on it, one started at the
printer's own touchscreen included.  It is now retired on the ending edge,
which both status doors announce: the polled ``get_state`` wrap, and Bambu's
push callback, which writes the shared table first.

Only the test about the thread itself runs a watchdog thread.  Everywhere else
``PrintWatchdog.start`` is stood down, so a watchdog is ATTACHED (it sits in
``server._print_watchdogs``) without polling a fake printer after the test.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
from typing import Any
from unittest import mock

import pytest

from kiln import server
from kiln.print_watchdog import PrintWatchdog
from kiln.printers import base
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.registry import PrinterRegistry

#: The package under test -- resolved from the imported module, so a probe
#: never reads a different checkout than the one pytest imported.
_KILN = pathlib.Path(server.__file__).resolve().parent


# ---------------------------------------------------------------------------
# The bench
# ---------------------------------------------------------------------------


class _Bench(PrinterAdapter):
    """A real adapter that talks to nothing.

    Real, because the start template and the status wrap are inherited from
    ``PrinterAdapter``; a MagicMock or a duck-typed fake skips both and so
    proves nothing about either.
    """

    def __init__(self, host: str = "192.0.2.10", *, accepts: bool = True) -> None:
        self.host = host
        self.serial = ""  # the machine fingerprint falls back to the host
        self._accepts = accepts
        self._status = PrinterStatus.IDLE
        self.started: list[str] = []

    @property
    def name(self) -> str:
        return "bench"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(
            connected=True,
            state=self._status,
            tool_temp_actual=25.0,
            tool_temp_target=0.0,
            bed_temp_actual=25.0,
            bed_temp_target=0.0,
        )

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self.started[-1] if self.started else None)

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1024)]

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(
            success=True, file_name=os.path.basename(file_path), message="uploaded"
        )

    def delete_file(self, file_name: str) -> bool:
        return True

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        if not self._accepts:
            return PrintResult(success=False, message="The printer refused the job.")
        self.started.append(file_name)
        self._status = PrinterStatus.PRINTING
        return PrintResult(success=True, message="started")

    def finish(self) -> None:
        self._status = PrinterStatus.IDLE

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="cancelled")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="paused")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="resumed")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="stopped")

    def send_gcode(self, commands: Any) -> bool:
        return True

    def set_tool_temp(self, celsius: float, tool: int = 0) -> bool:
        return True

    def set_bed_temp(self, celsius: float) -> bool:
        return True

    def _load_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError

    def _unload_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError

    def _purge_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError


class _NullHeaterWatchdog:
    @staticmethod
    def notify_print_started() -> None:
        pass

    @staticmethod
    def notify_print_ended() -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """A process that has attached nothing yet, and polls nothing.

    ``raising=False`` on the names this change introduces, so the file still
    collects against the code before it and each test fails on its own claim.
    """
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", (), raising=False)
    monkeypatch.setattr(base, "_PRINT_ENDED_HOOKS", (), raising=False)
    monkeypatch.setattr(server, "_print_lifecycle_hooks_installed", False, raising=False)
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_adapter", None)
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(server, "_get_heater_watchdog", lambda: _NullHeaterWatchdog)
    # Attached, never polling.
    monkeypatch.setattr(PrintWatchdog, "start", lambda self: None)
    # The upload safety check reads the configured printer model; it is its
    # own subject, and a bench file never needs it.
    monkeypatch.setattr(base, "_preflight_upload_or_raise", lambda adapter, path: None)
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setenv("KILN_SKIP_PREFLIGHT", "1")


def _on_the_server(**printers: PrinterAdapter) -> None:
    """Register *printers* the way a running server holds them.

    Through ``_get_registry``, the door every printer tool resolves a printer
    through -- which is also where a serving process attaches its hooks.
    """
    registry = server._get_registry()
    for name, adapter in printers.items():
        registry.register(name, adapter)


def _tool(fn: Any) -> Any:
    return getattr(fn, "fn", fn)


def _plugin_tools(module: Any) -> dict[str, Any]:
    """The tool functions a plugin registers, called directly."""
    tools: dict[str, Any] = {}

    class _Registrar:
        def tool(self, *args: Any, **kwargs: Any):
            def decorator(fn):
                tools[kwargs.get("name") or fn.__name__] = fn
                return fn

            return decorator

        def __getattr__(self, name: str):
            return lambda *a, **k: (lambda fn: fn)

    module.plugin.register(_Registrar())
    return tools


def _ready(printer_name: str | None = None, **kwargs: Any) -> dict:
    return {"ready": True, "summary": "ready"}


def _sliced(output_path: pathlib.Path):
    from kiln.slicer import SliceResult

    return lambda path, **kwargs: SliceResult(
        success=True, output_path=str(output_path), slicer="bench", message="sliced"
    )


@pytest.fixture
def model_files(tmp_path):
    stl = tmp_path / "part.stl"
    stl.write_text("solid part\nendsolid part\n")
    gcode = tmp_path / "part.gcode"
    gcode.write_text("G28\nG1 X10 Y10 Z0.2 E1\n")
    return stl, gcode


def _record_starts() -> list[tuple[Any, str]]:
    calls: list[tuple[Any, str]] = []

    def hook(adapter: Any, file_name: str) -> None:
        calls.append((adapter, file_name))

    base.register_print_started_hook(hook)
    return calls


# ---------------------------------------------------------------------------
# The start template announces a start -- and only a real one
# ---------------------------------------------------------------------------


def test_a_start_the_printer_accepts_calls_each_started_hook_once():
    calls: list[tuple[Any, str]] = []

    def hook(adapter: Any, file_name: str) -> None:
        calls.append((adapter, file_name))

    base.register_print_started_hook(hook)
    base.register_print_started_hook(hook)  # registering twice is registering once
    printer = _Bench()

    assert printer.start_print("part.gcode").success is True
    assert calls == [(printer, "part.gcode")]


def test_a_start_the_printer_refuses_calls_no_hook():
    calls = _record_starts()
    printer = _Bench(accepts=False)

    assert printer.start_print("part.gcode").success is False
    assert calls == []


def test_a_start_the_pre_print_gate_blocks_calls_no_hook(monkeypatch):
    from kiln.printers import print_gate

    monkeypatch.setattr(
        print_gate,
        "run_adapter_gate",
        lambda adapter, file_name, kwargs: {"blocked": True, "reason": "It cannot fit."},
    )
    calls = _record_starts()
    printer = _Bench()

    assert printer.start_print("part.gcode").success is False
    assert printer.started == []
    assert calls == []


def test_a_resume_file_continues_a_print_and_calls_no_hook():
    """A mid-print swap continues the print already running; it is not a new one."""
    calls = _record_starts()
    printer = _Bench()

    assert printer.start_print("transformed_resume_ab12.3mf").success is True
    assert calls == []


def test_a_hook_that_raises_neither_fails_the_print_nor_silences_the_next_hook():
    def broken(adapter: Any, file_name: str) -> None:
        raise RuntimeError("a broken hook")

    base.register_print_started_hook(broken)
    calls = _record_starts()
    printer = _Bench()

    result = printer.start_print("part.gcode")

    assert result.success is True
    assert printer.started == ["part.gcode"]
    assert calls == [(printer, "part.gcode")]


# ---------------------------------------------------------------------------
# An ending is announced once, by whichever door saw it
# ---------------------------------------------------------------------------


def test_a_print_seen_ending_through_its_status_announces_one_ending():
    ended: list[str] = []
    base.register_print_ended_hook(ended.append)
    printer = _Bench()
    PrinterRegistry().register("garage", printer)  # the name its lifecycle is filed under

    printer.start_print("part.gcode")
    printer.get_state()  # printing
    printer.finish()
    printer.get_state()  # printing -> idle: the ending
    printer.get_state()  # idle -> idle: not a second ending

    assert ended == ["garage"]


def test_an_ending_with_no_job_name_is_still_an_ending():
    """The outcome record needs a job name; retiring a watchdog does not."""
    ended: list[str] = []
    base.register_print_ended_hook(ended.append)
    printer = _Bench()
    PrinterRegistry().register("garage", printer)
    printer._status = PrinterStatus.PRINTING  # a job the printer names nothing for

    printer.get_state()
    printer.finish()
    printer.get_state()

    assert ended == ["garage"]


def test_a_bambu_ending_seen_on_its_push_channel_retires_its_watchdog():
    """The door the polled wrap cannot see.

    A connected Bambu's MQTT callback writes the shared previous-state table
    as each frame lands, so the polled wrap reading behind it finds no edge at
    all.  An ending announced only by the wrap would leave this watchdog
    polling the machine after its print finished.
    """
    from kiln.printers.bambu import BambuAdapter

    a1 = BambuAdapter(
        host="192.0.2.50", access_code="12345678", serial="01S00C000000001", timeout=2
    )
    a1._mqtt_connected.set()
    a1._connected = True
    a1._mqtt_client = mock.MagicMock()
    a1._confirm_window_s = 0.0
    _on_the_server(a1=a1)
    server._spawn_print_watchdog(a1, "part.3mf")
    watchdog = server._print_watchdogs["a1"]
    ended: list[str] = []
    base.register_print_ended_hook(ended.append)

    def push(**fields: Any) -> None:
        msg = mock.MagicMock()
        msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
        a1._on_message(a1._mqtt_client, None, msg)

    job = {"subtask_name": "part", "gcode_file": "part.3mf"}
    push(gcode_state="RUNNING", **job)
    push(gcode_state="RUNNING", **job)
    a1.get_state()  # the polled door reads between frames, as the watchdog does
    push(gcode_state="FINISH", **job)
    a1.get_state()  # and after the ending, finding it already announced

    assert "a1" not in server._print_watchdogs
    assert watchdog._stop_event.is_set()
    assert ended == ["a1"]


def test_stop_called_from_the_watchdogs_own_thread_signals_instead_of_raising():
    """The watchdog's own poll is usually the read that sees its print end."""
    errors: list[BaseException] = []
    asked = threading.Event()

    class _EndsDuringItsOwnPoll:
        def get_state(self) -> PrinterState:
            try:
                watchdog.stop()
            except BaseException as exc:  # noqa: BLE001 — the claim is that nothing is raised
                errors.append(exc)
            finally:
                asked.set()
            return PrinterState(connected=True, state=PrinterStatus.IDLE)

        def get_job(self) -> None:
            return None

        def emergency_stop(self) -> PrintResult:
            return PrintResult(success=True, message="stopped")

    watchdog = PrintWatchdog(_EndsDuringItsOwnPoll(), poll_interval_sec=0.1)
    thread = threading.Thread(target=watchdog._run_loop, daemon=True)
    watchdog._thread = thread  # what start() does, with start() stood down here
    thread.start()

    assert asked.wait(5.0)
    thread.join(5.0)
    assert errors == []
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# By construction: one builder, one way in, one template
# ---------------------------------------------------------------------------


def _nodes_with_function(tree: ast.AST):
    """Every node, with the name of the function it sits in (None at module level)."""
    pending: list[tuple[ast.AST, str | None]] = [(tree, None)]
    while pending:
        node, function = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        yield node, function
        pending.extend((child, function) for child in ast.iter_child_nodes(node))


def test_one_place_builds_a_print_watchdog_and_only_the_started_hook_reaches_it():
    """"Attached to every print Kiln starts" holds by construction, not by care.

    If a second door built a watchdog, or called the spawner directly, the
    promise would be back to depending on each door remembering to.
    """
    built: list[tuple[str, str | None]] = []
    reached: list[tuple[str, str | None]] = []
    registered = False
    for path in sorted(_KILN.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "PrintWatchdog" not in text and "_spawn_print_watchdog" not in text:
            continue
        rel = path.relative_to(_KILN).as_posix()
        for node, function in _nodes_with_function(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Call):
                callee = node.func
                called = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
                if called == "PrintWatchdog":
                    built.append((rel, function))
                if called == "register_print_started_hook" and any(
                    isinstance(arg, ast.Name) and arg.id == "_spawn_print_watchdog"
                    for arg in node.args
                ):
                    registered = True
            if (isinstance(node, ast.Name) and node.id == "_spawn_print_watchdog") or (
                isinstance(node, ast.Attribute) and node.attr == "_spawn_print_watchdog"
            ):
                reached.append((rel, function))

    assert built == [("server.py", "_spawn_print_watchdog")]
    assert reached == [("server.py", "_install_print_lifecycle_hooks")]
    assert registered


def test_no_printer_adapter_replaces_the_start_template():
    """An adapter that overrode start_print would start prints no hook hears of.

    Walks the classes, not the text, so an override by assignment or through
    an intermediate base is found too.
    """
    import importlib
    import pkgutil

    import kiln.printers as printers_package

    for module in pkgutil.iter_modules(printers_package.__path__, "kiln.printers."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            continue  # an optional dependency absent; that adapter cannot run here either

    seen: list[type] = []

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.append(sub)
                walk(sub)

    walk(PrinterAdapter)
    shipped = [cls for cls in seen if cls.__module__.startswith("kiln.printers.")]

    assert len(shipped) >= 8, [cls.__qualname__ for cls in shipped]
    assert [cls.__qualname__ for cls in shipped if "start_print" in vars(cls)] == []


# ---------------------------------------------------------------------------
# Attaching is a serving process's decision, taken at first use
# ---------------------------------------------------------------------------


def test_importing_the_server_attaches_nothing_until_a_printer_is_resolved(tmp_path):
    """Import alone must not put a watchdog behind every successful start.

    A fresh interpreter, pinned to the tree under test: an adapter test that
    merely imports the server would otherwise have a polling thread behind
    each start it makes, reaching whatever host that test named after its
    network mock had closed.
    """
    probe = textwrap.dedent(
        """
        import kiln.printers.base as base
        import kiln.server as server

        def count():
            return (
                len(getattr(base, "_PRINT_STARTED_HOOKS", ())),
                len(getattr(base, "_PRINT_ENDED_HOOKS", ())),
            )

        print("SERVER", server.__file__)
        print("AFTER_IMPORT", *count())
        server._get_registry()
        print("AFTER_RESOLVE", *count())
        """
    )
    import site

    env = {
        **os.environ,
        # A scratch home keeps the probe away from a real ~/.kiln; the user
        # site directory is derived from HOME, so name it outright or the
        # packages installed there vanish with it.
        "HOME": str(tmp_path),
        "PYTHONUSERBASE": site.getuserbase(),
        "PYTHONPATH": os.pathsep.join(
            [str(_KILN.parent), *filter(None, [os.environ.get("PYTHONPATH")])]
        ),
    }
    done = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    lines = {
        line.split(" ", 1)[0]: line.split(" ", 1)[1]
        for line in done.stdout.splitlines()
        if line.startswith(("SERVER ", "AFTER_IMPORT ", "AFTER_RESOLVE "))
    }

    assert pathlib.Path(lines["SERVER"]).resolve() == pathlib.Path(server.__file__).resolve()
    assert lines["AFTER_IMPORT"] == "0 0"
    assert lines["AFTER_RESOLVE"] == "1 1"


def test_a_printer_the_server_does_not_resolve_gets_no_watchdog():
    """Only a printer the server can name gets one.

    An unregistered adapter is filed under its backend family, which every
    other unregistered machine of that brand shares, so one machine's start
    or ending would reach another's watchdog.
    """
    server._get_registry()  # this process serves tools, so its hooks are attached
    stray = _Bench("192.0.2.20")
    assert stray.start_print("part.gcode").success is True
    assert server._print_watchdogs == {}

    served = _Bench("192.0.2.11")
    _on_the_server(workshop=served)
    assert served.start_print("part.gcode").success is True
    assert list(server._print_watchdogs) == ["workshop"]


# ---------------------------------------------------------------------------
# Every door that starts a print attaches a watchdog
# ---------------------------------------------------------------------------


def test_the_start_print_tool_files_the_watchdog_under_the_machine_it_started():
    """A watchdog filed under the default name would be torn down by the
    default printer's next start, leaving a live print unwatched."""
    garage, workshop = _Bench("192.0.2.10"), _Bench("192.0.2.11")
    _on_the_server(garage=garage, workshop=workshop)

    out = _tool(server.start_print)(file_name="part.gcode", printer_name="workshop")

    assert out["success"] is True, out
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_a_start_the_printer_refused_attaches_no_watchdog():
    """A watchdog on a machine that is not printing polices the next print on it."""
    workshop = _Bench(accepts=False)
    _on_the_server(workshop=workshop)

    out = _tool(server.start_print)(file_name="part.gcode", printer_name="workshop")

    assert out["success"] is False, out
    assert server._print_watchdogs == {}


def test_download_and_upload_attaches_a_watchdog_to_the_print_it_starts(
    monkeypatch, model_files
):
    _stl, gcode = model_files
    workshop = _Bench()
    _on_the_server(workshop=workshop)

    class _Marketplace:
        name = "myminifactory"
        display_name = "MyMiniFactory"
        supports_download = True

        def download_file(self, file_id: str, dest_dir: str) -> str:
            return str(gcode)

    class _Marketplaces:
        count = 1

        def get(self, name: str) -> _Marketplace:
            return _Marketplace()

    monkeypatch.setattr(server, "_marketplace_registry", _Marketplaces())
    monkeypatch.setattr(server, "_AUTO_PRINT_MARKETPLACE", True)
    monkeypatch.setattr(server, "preflight_check", _ready)

    out = _tool(server.download_and_upload)(
        file_id="7", source="myminifactory", printer_name="workshop"
    )

    assert out["success"] is True, out
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_generate_and_print_attaches_a_watchdog_to_the_print_it_starts(
    monkeypatch, model_files
):
    from kiln.generation.base import GenerationJob, GenerationResult, GenerationStatus
    from kiln.plugins import generation_ai_tools

    stl, gcode = model_files
    workshop = _Bench()
    _on_the_server(workshop=workshop)

    class _Provider:
        display_name = "bench provider"

        def generate(self, prompt: str, format: str = "stl", style: str | None = None):
            return GenerationJob(
                id="job-1", provider="bench", prompt=prompt, status=GenerationStatus.SUCCEEDED
            )

        def download_result(self, job_id: str) -> GenerationResult:
            return GenerationResult(
                job_id=job_id,
                provider="bench",
                local_path=str(stl),
                format="stl",
                file_size_bytes=stl.stat().st_size,
                prompt="a part",
            )

    monkeypatch.setattr(server, "_get_generation_provider", lambda provider="meshy": _Provider())
    monkeypatch.setattr(
        "kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline",
        lambda path, **kwargs: {"ready_to_print": True, "validated_path": str(stl)},
    )
    monkeypatch.setattr("kiln.printers.bed_fit.resolve_build_volume", lambda printer: None)
    monkeypatch.setattr(server, "_resolve_slice_profile_context", lambda **kwargs: (None, None))
    monkeypatch.setattr("kiln.slicer.slice_file", _sliced(gcode))
    monkeypatch.setattr(server, "_AUTO_PRINT_GENERATED", True)
    monkeypatch.setattr(server, "preflight_check", _ready)

    out = _plugin_tools(generation_ai_tools)["generate_and_print"](
        prompt="a part", provider="bench", printer_name="workshop"
    )

    assert out["success"] is True, out
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_start_monitored_print_attaches_a_watchdog_to_the_print_it_starts(monkeypatch):
    """Attached by the start itself, so it holds even when the door fails after it.

    This door currently does: once the print has started it calls
    ``FirstLayerMonitor.start``, which that class does not define, and reports
    an internal error for a print that is running.  That is its own defect and
    is deliberately not stubbed away here -- the running print has its
    watchdog regardless, which is the point of attaching at the start.
    """
    from kiln.plugins import monitoring_tools

    workshop = _Bench()
    _on_the_server(workshop=workshop)
    monkeypatch.setattr(server, "_first_layer_monitors", {})
    monkeypatch.setattr(server, "preflight_check", _ready)

    _plugin_tools(monitoring_tools)["start_monitored_print"](
        file_name="part.gcode", printer_name="workshop"
    )

    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_slice_and_print_attaches_a_watchdog_to_the_print_it_starts(
    monkeypatch, model_files
):
    from kiln.plugins import slicer_tools

    stl, gcode = model_files
    workshop = _Bench()
    _on_the_server(workshop=workshop)
    monkeypatch.setattr(server, "_resolve_slice_profile_context", lambda **kwargs: (None, None))
    monkeypatch.setattr(
        slicer_tools,
        "_apply_bed_fit_gate",
        lambda path, printer_id, auto_center, material_id=None: (path, None, {}),
    )
    monkeypatch.setattr(
        "kiln.slicer_profiles.start_gcode_override_from_printer",
        lambda adapter, printer_id, overrides: (None, "declined: bench"),
    )
    monkeypatch.setattr("kiln.slicer.slice_file", _sliced(gcode))
    monkeypatch.setattr(server, "preflight_check", _ready)

    out = _plugin_tools(slicer_tools)["slice_and_print"](
        input_path=str(stl), printer_name="workshop", material="PLA", skip_validation=True
    )

    assert out["success"] is True, out
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_retry_print_with_fix_attaches_a_watchdog_to_the_print_it_starts(
    monkeypatch, model_files
):
    from kiln.plugins import smart_print_tools

    stl, gcode = model_files
    workshop = _Bench()
    _on_the_server(workshop=workshop)
    monkeypatch.setattr("kiln.slicer.slice_file", _sliced(gcode))
    monkeypatch.setattr("kiln.slicer_profiles.resolve_slicer_profile", lambda *a, **k: None)
    monkeypatch.setattr(
        "kiln.printability.analyze_printability",
        mock.Mock(side_effect=RuntimeError("not the subject")),
    )
    monkeypatch.setattr(server, "_map_printer_hint_to_profile_id", lambda *a, **k: None)
    monkeypatch.setattr(server, "_PRINTER_MODEL", None)
    monkeypatch.setattr(server, "preflight_check", _ready)

    out = _plugin_tools(smart_print_tools)["retry_print_with_fix"](
        model_path=str(stl),
        printer_name="workshop",
        skip_diagnosis=True,
        skip_validation=True,
    )

    assert out["success"] is True, out
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_the_scheduler_attaches_a_watchdog_to_the_job_it_dispatches(tmp_path):
    """Its own tests hand it MagicMock adapters, whose start_print never
    reaches the template -- so this one uses a real adapter."""
    from kiln.events import EventBus
    from kiln.queue import PrintQueue
    from kiln.scheduler import JobScheduler

    workshop = _Bench()
    _on_the_server(workshop=workshop)
    queue = PrintQueue(db_path=str(tmp_path / "queue.db"))
    queue.submit(file_name="part.gcode", printer_name="workshop")
    scheduler = JobScheduler(queue, server._get_registry(), EventBus())

    dispatched = scheduler.tick()["dispatched"]

    assert [job["printer_name"] for job in dispatched] == ["workshop"]
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


def test_the_quick_print_pipeline_attaches_a_watchdog_to_the_print_it_starts(
    monkeypatch, model_files
):
    from kiln.pipelines import quick_print

    stl, gcode = model_files
    workshop = _Bench()
    _on_the_server(workshop=workshop)
    monkeypatch.setattr("kiln.slicer.slice_file", _sliced(gcode))

    result = quick_print(model_path=str(stl), printer_name="workshop", skip_validation=True)

    assert result.success is True, result.message
    assert workshop.started == ["part.gcode"]
    assert list(server._print_watchdogs) == ["workshop"]


# ---------------------------------------------------------------------------
# A watchdog does not outlive its print
# ---------------------------------------------------------------------------


def test_a_print_that_ends_retires_its_watchdog_and_no_other_machines():
    garage, workshop = _Bench("192.0.2.10"), _Bench("192.0.2.11")
    _on_the_server(garage=garage, workshop=workshop)
    server._spawn_print_watchdog(garage, "a.gcode")
    server._spawn_print_watchdog(workshop, "b.gcode")
    garage_watchdog = server._print_watchdogs["garage"]
    workshop_watchdog = server._print_watchdogs["workshop"]
    garage._status = PrinterStatus.PRINTING
    workshop._status = PrinterStatus.PRINTING
    garage.get_state()
    workshop.get_state()

    garage.finish()
    garage.get_state()  # the garage print is seen ending

    assert list(server._print_watchdogs) == ["workshop"]
    assert garage_watchdog._stop_event.is_set()
    assert not workshop_watchdog._stop_event.is_set()
